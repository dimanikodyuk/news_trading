"""
Backtest paper engine на історичних подіях.

Запуск:
    python scripts/backtest_paper.py            # усі кандидати
    python scripts/backtest_paper.py 30         # тільки за останні 30 днів
    python scripts/backtest_paper.py 30 --reset # очистити backtest-таблиці

Що робить:
1. Бере всі high-події з impact (expected_dir, price_t1, ret_15m).
2. Для кожної — імітує вхід на T+1 і вихід на T+60.
3. Рахує P&L з комісією + slippage.
4. НЕ чіпає paper_account / paper_trades — пише у окремі таблиці
   backtest_trades, щоб не змішувати з реальним paper-акаунтом.

Логіка фільтрів — та сама, що в engine.py:
- importance = high
- hit IN (HIT, MISS)
- hit_rate по title >= 60%
- |ret_15m| >= 0.30%
- SPOT_LONG_ONLY (тільки long)
"""

import asyncio
import sys
import logging
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import get_db, init_db
from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# --- Параметри (мають збігатися з engine.py) ---
TRADE_SYMBOLS = ["SOLUSDT", "BTCUSDT"]
TRADE_SIZE_USD = 10.0
COMMISSION_PCT = 0.1
SLIPPAGE_PCT = 0.05
HIT_RATE_THRESHOLD = 60.0
MIN_EVENTS_FOR_TRUST = 1
PAPER_MIN_MOVE_PCT = 0.30
SPOT_LONG_ONLY = True
ENTRY_OFFSET_MIN = 1     # вхід на T+1
EXIT_OFFSET_MIN = 60     # вихід на T+60


BACKTEST_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    event_title TEXT,
    event_time_utc TEXT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_ts INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    entry_fee REAL NOT NULL,
    exit_ts INTEGER,
    exit_price REAL,
    exit_fee REAL,
    size_usd REAL NOT NULL,
    pnl REAL,
    pnl_pct REAL,
    status TEXT NOT NULL DEFAULT 'CLOSED',
    reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_backtest_event ON backtest_trades(event_id);
CREATE INDEX IF NOT EXISTS idx_backtest_symbol ON backtest_trades(symbol);
"""


def _iso_to_ms(iso_str: str):
    from datetime import datetime
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


async def _get_close(db, symbol, interval, target_ts_ms, tolerance_min=3):
    tol_ms = tolerance_min * 60_000
    cur = await db.execute("""
        SELECT ts, close FROM prices
        WHERE symbol = ? AND interval = ?
          AND ts BETWEEN ? AND ?
          AND confirm = 1
        ORDER BY ABS(ts - ?) ASC
        LIMIT 1
    """, (symbol, interval, target_ts_ms - tol_ms, target_ts_ms + tol_ms,
          target_ts_ms))
    row = await cur.fetchone()
    return float(row["close"]) if row else None


async def _hit_rate(db, title: str) -> tuple[int, int, float]:
    cur = await db.execute("""
        SELECT
            SUM(CASE WHEN ei.hit='HIT' THEN 1 ELSE 0 END) AS hits,
            SUM(CASE WHEN ei.hit='MISS' THEN 1 ELSE 0 END) AS misses
        FROM event_impact ei
        JOIN events e ON e.id = ei.event_id
        WHERE ei.symbol = 'SOLUSDT'
          AND LOWER(TRIM(e.title)) = ?
    """, (title.strip().lower(),))
    r = await cur.fetchone()
    hits = r["hits"] or 0
    misses = r["misses"] or 0
    total = hits + misses
    rate = (hits / total * 100) if total > 0 else 0
    return hits, misses, rate


async def run_backtest(days: int | None = None, reset: bool = False):
    await init_db()

    async with get_db() as db:
        await db.executescript(BACKTEST_TABLES_SQL)
        await db.commit()

        if reset:
            await db.execute("DELETE FROM backtest_trades")
            await db.commit()
            logger.info("backtest_trades очищено")

        # --- Кандидати ---
        where = ["e.importance = 'high'"]
        params = []
        if days:
            from datetime import datetime, timezone, timedelta
            from_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            where.append("e.time_utc >= ?")
            params.append(from_iso)
        where_sql = " AND ".join(where)

        cur = await db.execute(f"""
            SELECT
                e.id, e.title, e.time_utc, e.forecast_value, e.actual_value
            FROM events e
            WHERE {where_sql}
              AND EXISTS (
                  SELECT 1 FROM event_impact ei
                  WHERE ei.event_id = e.id
                    AND ei.expected_dir IS NOT NULL
                    AND ei.hit IN ('HIT', 'MISS')
                    AND ei.price_t1 IS NOT NULL
                    AND ei.ret_15m IS NOT NULL
              )
            ORDER BY e.time_utc ASC
        """, params)
        candidates = await cur.fetchall()

        logger.info(f"Кандидатів: {len(candidates)}")

        opened = 0
        skipped = 0
        for c in candidates:
            event_ts = _iso_to_ms(c["time_utc"])
            if event_ts is None:
                continue

            hits, misses, rate = await _hit_rate(db, c["title"])
            if hits + misses < MIN_EVENTS_FOR_TRUST:
                logger.info(f"skip '{c['title']}': n={hits+misses} < {MIN_EVENTS_FOR_TRUST}")
                skipped += 1
                continue
            if rate < HIT_RATE_THRESHOLD:
                logger.info(f"skip '{c['title']}': hit_rate={rate:.0f}% < {HIT_RATE_THRESHOLD}%")
                skipped += 1
                continue

            for sym in TRADE_SYMBOLS:
                cur = await db.execute("""
                    SELECT 1 FROM backtest_trades
                    WHERE event_id = ? AND symbol = ?
                """, (c["id"], sym))
                if await cur.fetchone():
                    continue

                cur = await db.execute("""
                    SELECT expected_dir, hit, price_t1, ret_15m
                    FROM event_impact
                    WHERE event_id = ? AND symbol = ?
                      AND expected_dir IS NOT NULL
                      AND hit IN ('HIT', 'MISS')
                      AND price_t1 IS NOT NULL
                """, (c["id"], sym))
                row = await cur.fetchone()
                if not row:
                    continue

                ret_15 = row["ret_15m"]
                if ret_15 is None or abs(ret_15) < PAPER_MIN_MOVE_PCT:
                    logger.info(
                        f"skip {sym} '{c['title']}': |ret_15m|={abs(ret_15 or 0):.3f}% < {PAPER_MIN_MOVE_PCT}%"
                    )
                    skipped += 1
                    continue

                direction = "long" if row["expected_dir"] == "up" else "short"
                if SPOT_LONG_ONLY and direction == "short":
                    logger.info(f"skip {sym} '{c['title']}': SHORT (SPOT_LONG_ONLY)")
                    skipped += 1
                    continue

                # Entry на T+1
                entry_ts = event_ts + ENTRY_OFFSET_MIN * 60_000
                entry_price = await _get_close(db, sym, "1", entry_ts)
                if entry_price is None:
                    logger.warning(f"skip {sym} '{c['title']}': немає свічки на entry")
                    skipped += 1
                    continue

                if direction == "long":
                    entry_price_eff = entry_price * (1 + SLIPPAGE_PCT / 100)
                else:
                    entry_price_eff = entry_price * (1 - SLIPPAGE_PCT / 100)
                entry_fee = TRADE_SIZE_USD * (COMMISSION_PCT / 100)

                # Exit на T+60
                exit_ts = event_ts + EXIT_OFFSET_MIN * 60_000
                exit_price = await _get_close(db, sym, "1", exit_ts)
                if exit_price is None:
                    logger.warning(f"skip {sym} '{c['title']}': немає свічки на exit")
                    skipped += 1
                    continue

                if direction == "long":
                    exit_price_eff = exit_price * (1 - SLIPPAGE_PCT / 100)
                else:
                    exit_price_eff = exit_price * (1 + SLIPPAGE_PCT / 100)

                qty = TRADE_SIZE_USD / entry_price_eff
                exit_notional = qty * exit_price_eff
                exit_fee = exit_notional * (COMMISSION_PCT / 100)

                if direction == "long":
                    gross_pnl = qty * (exit_price_eff - entry_price_eff)
                else:
                    gross_pnl = qty * (entry_price_eff - exit_price_eff)

                pnl = gross_pnl - entry_fee - exit_fee
                pnl_pct = (pnl / TRADE_SIZE_USD) * 100

                await db.execute("""
                    INSERT INTO backtest_trades
                    (event_id, event_title, event_time_utc, symbol, direction,
                     entry_ts, entry_price, entry_fee,
                     exit_ts, exit_price, exit_fee,
                     size_usd, pnl, pnl_pct, status, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLOSED', ?)
                """, (
                    c["id"], c["title"], c["time_utc"], sym, direction,
                    entry_ts, entry_price_eff, entry_fee,
                    exit_ts, exit_price_eff, exit_fee,
                    TRADE_SIZE_USD, pnl, pnl_pct,
                    f"hit_rate={rate:.0f}% |ret15|={abs(ret_15):.2f}%",
                ))
                opened += 1
                logger.info(
                    f"backtest {direction} {sym} '{c['title']}' "
                    f"@ {entry_price_eff:.4f} -> {exit_price_eff:.4f} "
                    f"pnl={pnl:+.4f} ({pnl_pct:+.3f}%)"
                )

        await db.commit()

        # --- Підсумок ---
        cur = await db.execute("""
            SELECT
                COUNT(*) AS n,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) AS losses,
                SUM(pnl) AS total_pnl,
                AVG(pnl) AS avg_pnl,
                AVG(CASE WHEN pnl > 0 THEN pnl END) AS avg_win,
                AVG(CASE WHEN pnl <= 0 THEN pnl END) AS avg_loss
            FROM backtest_trades
        """)
        s = await cur.fetchone()

        cur = await db.execute("""
            SELECT symbol,
                   COUNT(*) AS n,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(pnl) AS total_pnl
            FROM backtest_trades GROUP BY symbol
        """)
        by_sym = [dict(r) for r in await cur.fetchall()]

    n = s["n"] or 0
    wins = s["wins"] or 0
    losses = s["losses"] or 0
    total_pnl = s["total_pnl"] or 0
    win_rate = (wins / n * 100) if n > 0 else 0

    print("\n" + "=" * 60)
    print("BACKTEST РЕЗУЛЬТАТИ")
    print("=" * 60)
    print(f"Угод:              {n}")
    print(f"Відкрито (skipped): {skipped}")
    print(f"Wins / Losses:     {wins} / {losses}")
    print(f"Win Rate:          {win_rate:.1f}%")
    print(f"Total P&L:         ${total_pnl:+.4f}")
    print(f"Avg P&L:           ${(s['avg_pnl'] or 0):+.4f}")
    print(f"Avg Win:           ${(s['avg_win'] or 0):+.4f}")
    print(f"Avg Loss:          ${(s['avg_loss'] or 0):+.4f}")
    print()
    print("По символах:")
    for r in by_sym:
        wr = (r["wins"] / r["n"] * 100) if r["n"] > 0 else 0
        print(f"  {r['symbol']:10} n={r['n']:2}  wins={r['wins']:2}  "
              f"WR={wr:5.1f}%  P&L=${r['total_pnl']:+.4f}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    args = sys.argv[1:]
    days = None
    reset = False
    for a in args:
        if a == "--reset":
            reset = True
        elif a.isdigit():
            days = int(a)

    asyncio.run(run_backtest(days=days, reset=reset))