"""
Paper Trading Engine: відкриття та закриття віртуальних угод.

Режим: LONG + SHORT (симуляція).
Символи: SOLUSDT + BTCUSDT.
Фільтр руху: PAPER_MIN_MOVE_PCT — не торгуємо, якщо |ret_15m| < 0.30%.

FIX:
- Прибрано жорстку вимогу price_t60 IS NOT NULL з candidates
  (достатньо price_t1; T+60 дорахується при закритті).
- Розширено вікно входу: T+1 .. T+30 (замість T+1 .. T+60),
  щоб engine (кожні 5 хв) встигав побачити подію.
- Hit rate рахується по нормалізованій назві (lower, strip),
  а не по точному title.
- Додано детальне діагностичне логування причин skip.
- ORDER BY e.time_utc DESC LIMIT 50 (більше кандидатів).
- MAX_OPEN_TRADES лишається 6, але перевірка коректна.
- SPOT_LONG_ONLY = True (SPOT не має short).
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from app.db import get_db
from app.config import settings
from app.bot.notifier import get_bot

logger = logging.getLogger(__name__)

# --- Символи для paper trading ---
TRADE_SYMBOLS = ["SOLUSDT", "BTCUSDT"]

# --- Параметри стратегії ---
TRADE_SIZE_USD = 10.0
COMMISSION_PCT = 0.1        # 0.1% Bybit spot taker
SLIPPAGE_PCT = 0.05         # 0.05% slippage
HIT_RATE_THRESHOLD = 60.0   # %
MIN_EVENTS_FOR_TRUST = 1    # ТЕСТ: 1 (для продакшену — 5)
MAX_OPEN_TRADES = 6         # 3 на символ × 2 символи

# --- Мінімальний рух для торгівлі ---
PAPER_MIN_MOVE_PCT = 0.30

# --- Вікно входу після події (FIX: було T+60) ---
ENTRY_WINDOW_MIN = 30       # вхід дозволено в межах T+1 .. T+30
EXIT_AFTER_MIN = 60         # вихід на T+60

# --- Режим торгівлі ---
SPOT_LONG_ONLY = True       # FIX: SPOT не має short


async def process_paper_engine() -> dict:
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

        exit_ts = event_ts_ms + EXIT_AFTER_MIN * 60_000
        if now_ms < exit_ts:
            continue

        cur = await db.execute("""
            SELECT close FROM prices
            WHERE symbol = ? AND interval = '1' AND confirm = 1
              AND ts BETWEEN ? AND ?
            ORDER BY ABS(ts - ?) ASC LIMIT 1
        """, (t["symbol"], exit_ts - 2 * 60_000, exit_ts + 2 * 60_000, exit_ts))
        row = await cur.fetchone()
        if not row:
            logger.warning(f"[trade {t['id']}] немає свічки на T+{EXIT_AFTER_MIN}")
            continue

        exit_price = float(row["close"])
        await _close_trade(db, t, exit_price, exit_ts)
        closed += 1

    return closed


async def _close_trade(db, trade, exit_price: float, exit_ts: int):
    entry_price = float(trade["entry_price"])
    size_usd = float(trade["size_usd"])
    direction = trade["direction"]
    entry_fee = float(trade["entry_fee"])

    qty = size_usd / entry_price

    if direction == "long":
        exit_price_eff = exit_price * (1 - SLIPPAGE_PCT / 100)
    else:
        exit_price_eff = exit_price * (1 + SLIPPAGE_PCT / 100)

    exit_notional = qty * exit_price_eff
    exit_fee = exit_notional * (COMMISSION_PCT / 100)

    if direction == "long":
        gross_pnl = qty * (exit_price_eff - entry_price)
    else:
        gross_pnl = qty * (entry_price - exit_price_eff)

    pnl = gross_pnl - entry_fee - exit_fee
    pnl_pct = (pnl / size_usd) * 100
    return_amount = size_usd + pnl

    await db.execute("""
        UPDATE paper_trades
        SET exit_ts = ?, exit_price = ?, exit_fee = ?,
            pnl = ?, pnl_pct = ?, status = 'CLOSED',
            closed_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (exit_ts, exit_price, exit_fee, pnl, pnl_pct, trade["id"]))

    await db.execute("""
        UPDATE paper_account
        SET balance = balance + ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
    """, (return_amount,))

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
        f"[trade {trade['id']}] closed {direction} {trade['symbol']}: "
        f"entry={entry_price:.4f} exit={exit_price_eff:.4f} "
        f"pnl={pnl:.4f} ({pnl_pct:.3f}%)"
    )


# ---------------------------------------------------------------------------
# Відкриття
# ---------------------------------------------------------------------------

def _norm_title(title: str) -> str:
    """FIX: нормалізована назва для групування hit rate."""
    return (title or "").strip().lower()


async def _open_new_trades(db) -> int:
    cur = await db.execute("SELECT COUNT(*) AS n FROM paper_trades WHERE status='OPEN'")
    open_count = (await cur.fetchone())["n"]
    if open_count >= MAX_OPEN_TRADES:
        logger.debug(f"[paper] skip: open_count={open_count} >= {MAX_OPEN_TRADES}")
        return 0

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    # FIX: прибрано price_t60 IS NOT NULL; достатньо price_t1 і ret_15m.
    # FIX: LIMIT 50 замість 20.
    cur = await db.execute("""
        SELECT
            e.id AS event_id, e.title, e.importance, e.time_utc
        FROM events e
        WHERE e.importance = 'high'
          AND EXISTS (
              SELECT 1 FROM event_impact ei
              WHERE ei.event_id = e.id
                AND ei.symbol IN ('SOLUSDT', 'BTCUSDT')
                AND ei.expected_dir IS NOT NULL
                AND ei.hit IN ('HIT', 'MISS')
                AND ei.price_t1 IS NOT NULL
                AND ei.ret_15m IS NOT NULL
          )
          AND (
              SELECT COUNT(*) FROM paper_trades pt
              WHERE pt.event_id = e.id
          ) < ?
        ORDER BY e.time_utc DESC
        LIMIT 50
    """, (len(TRADE_SYMBOLS),))
    candidates = await cur.fetchall()

    logger.info(f"[paper] candidates: {len(candidates)} (open_count={open_count})")

    opened = 0
    for c in candidates:
        if open_count + opened >= MAX_OPEN_TRADES:
            break

        # Таймінг
        event_ts = _iso_to_ms(c["time_utc"])
        if event_ts is None:
            logger.debug(f"[paper] skip '{c['title']}': bad time_utc")
            continue

        entry_min_ts = event_ts + 60_000
        entry_max_ts = event_ts + ENTRY_WINDOW_MIN * 60_000

        if now_ms < entry_min_ts:
            logger.debug(
                f"[paper] skip '{c['title']}': ще не час "
                f"(now={now_ms}, entry_min={entry_min_ts})"
            )
            continue
        if now_ms > entry_max_ts:
            logger.debug(
                f"[paper] skip '{c['title']}': вікно входу минуло "
                f"(now={now_ms}, entry_max={entry_max_ts})"
            )
            continue

        # FIX: hit rate по нормалізованій назві
        norm_title = _norm_title(c["title"])
        cur = await db.execute("""
            SELECT
                SUM(CASE WHEN ei.hit='HIT' THEN 1 ELSE 0 END) AS hits,
                SUM(CASE WHEN ei.hit='MISS' THEN 1 ELSE 0 END) AS misses
            FROM event_impact ei
            JOIN events e ON e.id = ei.event_id
            WHERE ei.symbol = 'SOLUSDT'
              AND LOWER(TRIM(e.title)) = ?
        """, (norm_title,))
        hr = await cur.fetchone()
        hits = hr["hits"] or 0
        misses = hr["misses"] or 0
        total = hits + misses

        if total < MIN_EVENTS_FOR_TRUST:
            logger.info(
                f"[paper] skip '{c['title']}': hit_rate n={total} < {MIN_EVENTS_FOR_TRUST}"
            )
            continue

        hit_rate = (hits / total * 100) if total > 0 else 0
        if hit_rate < HIT_RATE_THRESHOLD:
            logger.info(
                f"[paper] skip '{c['title']}': hit_rate={hit_rate:.0f}% < {HIT_RATE_THRESHOLD}%"
            )
            continue

        # Для КОЖНОГО символу окремо
        for sym in TRADE_SYMBOLS:
            if open_count + opened >= MAX_OPEN_TRADES:
                break

            cur = await db.execute("""
                SELECT 1 FROM paper_trades
                WHERE event_id = ? AND symbol = ?
            """, (c["event_id"], sym))
            if await cur.fetchone():
                continue

            cur = await db.execute("""
                SELECT expected_dir, hit, price_t1, ret_15m
                FROM event_impact
                WHERE event_id = ? AND symbol = ?
                  AND expected_dir IS NOT NULL
                  AND hit IN ('HIT', 'MISS')
                  AND price_t1 IS NOT NULL
            """, (c["event_id"], sym))
            row = await cur.fetchone()
            if not row:
                logger.debug(f"[paper] skip {sym} '{c['title']}': немає impact row")
                continue

            ret_15 = row["ret_15m"]
            if ret_15 is None or abs(ret_15) < PAPER_MIN_MOVE_PCT:
                logger.info(
                    f"[paper] skip {sym} '{c['title']}': "
                    f"|ret_15m|={abs(ret_15 or 0):.3f}% < {PAPER_MIN_MOVE_PCT}%"
                )
                continue

            direction = "long" if row["expected_dir"] == "up" else "short"
            if SPOT_LONG_ONLY and direction == "short":
                logger.info(
                    f"[paper] skip {sym} '{c['title']}': SHORT заблоковано (SPOT_LONG_ONLY)"
                )
                continue

            entry_price = float(row["price_t1"])

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
                c["event_id"], sym, direction, entry_ts,
                entry_price_eff, entry_fee, TRADE_SIZE_USD,
                f"hit_rate={hit_rate:.0f}% n={total} |ret15|={abs(ret_15):.2f}%"
            ))

            await db.execute("""
                UPDATE paper_account
                SET balance = balance - ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
            """, (TRADE_SIZE_USD + entry_fee,))

            opened += 1

            dir_emoji = "🟢" if direction == "long" else "🔴"
            text = (
                f"{dir_emoji} *Відкрито {direction.upper()} {sym}*\n"
                f"Подія: {c['title']}\n"
                f"Очікуваний напрямок: {row['expected_dir']}\n"
                f"Hit rate: {hit_rate:.0f}% (n={total})\n"
                f"Ret +15хв: {ret_15:+.3f}%\n"
                f"Вхід: ${entry_price_eff:.4f}\n"
                f"Розмір: ${TRADE_SIZE_USD:.2f}"
            )
            await _notify(text)
            logger.info(
                f"[paper] opened {direction} {sym} on event {c['event_id']} "
                f"@ {entry_price_eff:.4f} (hit_rate={hit_rate:.0f}%, ret15={ret_15:+.3f}%)"
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