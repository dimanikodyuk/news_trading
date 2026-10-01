"""
Paper Trading Engine: відкриття та закриття віртуальних угод.

Режим: SPOT + LONG-only (Варіант A).
- Відкриваємо тільки LONG (expected_dir = "up")
- Події з expected_dir = "down" — пропускаємо (на споті не можна шортити)
- Розмір: $10
- Комісія: 0.1% (Bybit spot taker) + slippage 0.05%
- Вихід: close свічки T+60
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from app.db import get_db
from app.config import settings
from app.bot.notifier import get_bot

logger = logging.getLogger(__name__)

# --- Параметри стратегії ---
SYMBOL = "SOLUSDT"
TRADE_SIZE_USD = 10.0
COMMISSION_PCT = 0.1        # 0.1% Bybit spot taker
SLIPPAGE_PCT = 0.05         # 0.05% slippage
HIT_RATE_THRESHOLD = 60.0   # %
MIN_EVENTS_FOR_TRUST = 1    # ТЕСТ: 1 (для продакшену — 5)
MAX_OPEN_TRADES = 3

# --- Режим торгівлі ---
# True  → SPOT + LONG-only (не можемо шортити)
# False → дозволити SHORT (для ф'ючерсів)
SPOT_LONG_ONLY = True


async def process_paper_engine() -> dict:
    """
    Головний цикл:
      1. Закриває прострочені угоди (T+60)
      2. Відкриває нові, якщо є підходящі події
    """
    stats = {"closed": 0, "opened": 0}

    async with get_db() as db:
        stats["closed"] = await _close_expired_trades(db)
        stats["opened"] = await _open_new_trades(db)
        await db.commit()

    if stats["closed"] or stats["opened"]:
        logger.info(f"Paper engine: {stats}")
    return stats


# ---------------------------------------------------------------------------
# Закриття
# ---------------------------------------------------------------------------

async def _close_expired_trades(db) -> int:
    """Закриває угоди, у яких T+60 хв уже минув."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    cur = await db.execute("""
        SELECT pt.*, e.time_utc
        FROM paper_trades pt
        JOIN events e ON e.id = pt.event_id
        WHERE pt.status = 'OPEN'
    """)
    open_trades = await cur.fetchall()

    closed = 0
    for t in open_trades:
        event_ts_ms = _iso_to_ms(t["time_utc"])
        if event_ts_ms is None:
            continue

        exit_ts = event_ts_ms + 60 * 60_000
        if now_ms < exit_ts:
            continue  # ще не час

        cur = await db.execute("""
            SELECT close FROM prices
            WHERE symbol = ? AND interval = '1' AND confirm = 1
              AND ts BETWEEN ? AND ?
            ORDER BY ABS(ts - ?) ASC LIMIT 1
        """, (t["symbol"], exit_ts - 2 * 60_000, exit_ts + 2 * 60_000, exit_ts))
        row = await cur.fetchone()
        if not row:
            logger.warning(f"[trade {t['id']}] немає свічки на T+60")
            continue

        exit_price = float(row["close"])
        await _close_trade(db, t, exit_price, exit_ts)
        closed += 1

    return closed


async def _close_trade(db, trade, exit_price: float, exit_ts: int):
    """
    Розраховує P&L з УРАХУВАННЯМ напрямку (long/short),
    оновлює баланс, шле сповіщення.

    Логіка LONG:
      - купуємо qty = size_usd / entry_price за entry_price
      - продаємо qty за exit_price
      - P&L = qty * (exit − entry) − fees

    Логіка SHORT:
      - продаємо qty = size_usd / entry_price за entry_price
      - купуємо qty назад за exit_price
      - P&L = qty * (entry − exit) − fees
    """
    entry_price = float(trade["entry_price"])
    size_usd = float(trade["size_usd"])
    direction = trade["direction"]  # 'long' | 'short'
    entry_fee = float(trade["entry_fee"])

    qty = size_usd / entry_price

    # Slippage на виході (гірша ціна для нас)
    if direction == "long":
        exit_price_eff = exit_price * (1 - SLIPPAGE_PCT / 100)
    else:  # short
        exit_price_eff = exit_price * (1 + SLIPPAGE_PCT / 100)

    # Комісія на виході (від обсягу виходу)
    exit_notional = qty * exit_price_eff
    exit_fee = exit_notional * (COMMISSION_PCT / 100)

    # P&L з урахуванням напрямку
    if direction == "long":
        gross_pnl = qty * (exit_price_eff - entry_price)
    else:  # short
        gross_pnl = qty * (entry_price - exit_price_eff)

    pnl = gross_pnl - entry_fee - exit_fee
    pnl_pct = (pnl / size_usd) * 100

    # Повертаємо на баланс: size_usd (тіло) + pnl
    # При long: ми інвестували size_usd, отримали назад size_usd + gross_pnl − fees
    # При short: ми «позичили» size_usd, повернули size_usd + gross_pnl − fees
    return_amount = size_usd + pnl

    # Оновлюємо угоду
    await db.execute("""
        UPDATE paper_trades
        SET exit_ts = ?, exit_price = ?, exit_fee = ?,
            pnl = ?, pnl_pct = ?, status = 'CLOSED',
            closed_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (exit_ts, exit_price, exit_fee, pnl, pnl_pct, trade["id"]))

    # Оновлюємо баланс
    await db.execute("""
        UPDATE paper_account
        SET balance = balance + ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
    """, (return_amount,))

    # --- Telegram ---
    emoji = "✅" if pnl > 0 else "❌"
    sign = "+" if pnl >= 0 else ""
    text = (
        f"{emoji} *Закрито {direction.upper()} {trade['symbol']}*\n"
        f"Вхід: ${entry_price:.4f} → Вихід: ${exit_price:.4f}\n"
        f"Розмір: ${size_usd:.2f}\n"
        f"*P&L: {sign}${pnl:.4f} ({sign}{pnl_pct:.3f}%)*"
    )
    await _notify(text)

    logger.info(
        f"[trade {trade['id']}] closed {direction}: "
        f"entry={entry_price:.4f} exit={exit_price_eff:.4f} "
        f"pnl={pnl:.4f} ({pnl_pct:.3f}%)"
    )


# ---------------------------------------------------------------------------
# Відкриття
# ---------------------------------------------------------------------------

async def _open_new_trades(db) -> int:
    """
    Шукає події, для яких:
      - impact розрахований (для SOL)
      - є expected_dir (не None)
      - hit rate по цьому title >= 60% (з n >= MIN_EVENTS_FOR_TRUST)
      - немає ще відкритої угоди
      - T+1 вже минув, T+60 ще не минув
      - кількість відкритих < MAX_OPEN_TRADES
      - напрямок дозволений (при SPOT_LONG_ONLY — тільки up)
    """
    cur = await db.execute("SELECT COUNT(*) AS n FROM paper_trades WHERE status='OPEN'")
    open_count = (await cur.fetchone())["n"]
    if open_count >= MAX_OPEN_TRADES:
        return 0

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    cur = await db.execute("""
        SELECT
            e.id AS event_id, e.title, e.importance, e.time_utc,
            ei.expected_dir, ei.hit, ei.price_t1, ei.price_t60
        FROM events e
        JOIN event_impact ei ON ei.event_id = e.id
        WHERE ei.symbol = 'SOLUSDT'
          AND e.importance = 'high'
          AND ei.expected_dir IS NOT NULL
          AND ei.hit IN ('HIT', 'MISS')
          AND ei.price_t1 IS NOT NULL
          AND ei.price_t60 IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM paper_trades pt
              WHERE pt.event_id = e.id AND pt.symbol = 'SOLUSDT'
          )
        ORDER BY e.time_utc DESC
        LIMIT 20
    """)
    candidates = await cur.fetchall()

    opened = 0
    for c in candidates:
        if open_count + opened >= MAX_OPEN_TRADES:
            break

        # --- Визначаємо напрямок ---
        direction = "long" if c["expected_dir"] == "up" else "short"

        # --- SPOT mode: пропускаємо SHORT ---
        if SPOT_LONG_ONLY and direction == "short":
            logger.debug(
                f"[event {c['event_id']}] пропущено: expected_dir=down "
                f"(SPOT_LONG_ONLY=True)"
            )
            continue

        # --- Перевірка hit rate для цього title ---
        cur = await db.execute("""
            SELECT
                SUM(CASE WHEN hit='HIT' THEN 1 ELSE 0 END) AS hits,
                SUM(CASE WHEN hit='MISS' THEN 1 ELSE 0 END) AS misses
            FROM event_impact
            WHERE symbol = 'SOLUSDT'
              AND event_id IN (
                SELECT id FROM events WHERE title = ?
              )
        """, (c["title"],))
        hr = await cur.fetchone()
        hits = hr["hits"] or 0
        misses = hr["misses"] or 0
        total = hits + misses
        if total < MIN_EVENTS_FOR_TRUST:
            logger.debug(
                f"[event {c['event_id']}] пропущено: n={total} < "
                f"{MIN_EVENTS_FOR_TRUST}"
            )
            continue
        hit_rate = (hits / total * 100) if total > 0 else 0
        if hit_rate < HIT_RATE_THRESHOLD:
            logger.debug(
                f"[event {c['event_id']}] пропущено: hit_rate={hit_rate:.0f}% < "
                f"{HIT_RATE_THRESHOLD}%"
            )
            continue

        # --- Таймінг ---
        event_ts = _iso_to_ms(c["time_utc"])
        if event_ts is None:
            continue
        entry_ts = event_ts + 60_000
        exit_ts = event_ts + 60 * 60_000
        if now_ms < entry_ts:
            continue
        if now_ms > exit_ts:
            continue  # вікно минуло

        # --- Вхід ---
        entry_price = float(c["price_t1"])

        # Slippage на вході
        if direction == "long":
            entry_price_eff = entry_price * (1 + SLIPPAGE_PCT / 100)
        else:
            entry_price_eff = entry_price * (1 - SLIPPAGE_PCT / 100)

        entry_fee = TRADE_SIZE_USD * (COMMISSION_PCT / 100)

        await db.execute("""
            INSERT INTO paper_trades
            (event_id, symbol, direction, entry_ts, entry_price, entry_fee,
             size_usd, status, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
        """, (
            c["event_id"], SYMBOL, direction, entry_ts,
            entry_price_eff, entry_fee, TRADE_SIZE_USD,
            f"hit_rate={hit_rate:.0f}% n={total}"
        ))

        # Списуємо з балансу
        await db.execute("""
            UPDATE paper_account
            SET balance = balance - ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
        """, (TRADE_SIZE_USD + entry_fee,))

        opened += 1

        # --- Telegram ---
        dir_emoji = "🟢" if direction == "long" else "🔴"
        text = (
            f"{dir_emoji} *Відкрито {direction.upper()} {SYMBOL}*\n"
            f"Подія: {c['title']}\n"
            f"Очікуваний напрямок: {c['expected_dir']}\n"
            f"Hit rate: {hit_rate:.0f}% (n={total})\n"
            f"Вхід: ${entry_price_eff:.4f}\n"
            f"Розмір: ${TRADE_SIZE_USD:.2f}"
        )
        await _notify(text)

        logger.info(
            f"[paper] opened {direction} on event {c['event_id']} "
            f"@ {entry_price_eff:.4f} (hit_rate={hit_rate:.0f}%)"
        )

    return opened


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso_to_ms(iso_str: str) -> Optional[int]:
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


async def _notify(text: str):
    bot = get_bot()
    if bot is None or not settings.tg_chat_id:
        return
    try:
        await bot.send_message(settings.tg_chat_id, text)
    except Exception as ex:
        logger.warning(f"Telegram notify failed: {ex}")