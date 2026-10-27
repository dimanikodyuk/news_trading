"""
Backtest paper engine на історичних подіях.

Не чіпає paper_account / paper_trades — пише у backtest_trades.

FIX (v3):
- Додано direction_filter: "long" | "short" | None (both).
  Дозволяє чистий SHORT-only або LONG-only прогін.
- Дедуплікація по (event_time_utc, symbol, direction).
- by_direction у відповіді.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from app.db import get_db

logger = logging.getLogger(__name__)

TRADE_SYMBOLS = ["SOLUSDT", "BTCUSDT"]
TRADE_SIZE_USD = 10.0
COMMISSION_PCT = 0.1
SLIPPAGE_PCT = 0.05
HIT_RATE_THRESHOLD = 60.0
MIN_EVENTS_FOR_TRUST = 1
PAPER_MIN_MOVE_PCT = 0.30
SPOT_LONG_ONLY = True
ENTRY_OFFSET_MIN = 1
EXIT_OFFSET_MIN = 60


def _iso_to_ms(iso_str: str) -> Optional[int]:
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


async def _hit_rate(db, title: str, before_time: str) -> tuple[int, int, float]:
    cur = await db.execute("""
        SELECT
            SUM(CASE WHEN ei.hit='HIT' THEN 1 ELSE 0 END) AS hits,
            SUM(CASE WHEN ei.hit='MISS' THEN 1 ELSE 0 END) AS misses
        FROM event_impact ei
        JOIN events e ON e.id = ei.event_id
        WHERE ei.symbol = 'SOLUSDT'
          AND LOWER(TRIM(e.title)) = ?
          AND julianday(e.time_utc) + 60.0/1440 < julianday(?)
    """, (title.strip().lower(), before_time))
    r = await cur.fetchone()
    hits = r["hits"] or 0
    misses = r["misses"] or 0
    total = hits + misses
    rate = (hits / total * 100) if total > 0 else 0
    return hits, misses, rate


async def run_backtest(
    days: int | None = None,
    reset: bool = True,
    hit_rate_threshold: float = HIT_RATE_THRESHOLD,
    min_move_pct: float = PAPER_MIN_MOVE_PCT,
    min_events: int = MIN_EVENTS_FOR_TRUST,
    symbols: list[str] | None = None,
    spot_long_only: bool = SPOT_LONG_ONLY,
    exit_offset_min: int = EXIT_OFFSET_MIN,
    trade_size_usd: float = TRADE_SIZE_USD,
    direction_filter: str | None = None,   # NEW: "long" | "short" | None
) -> dict:
    syms = symbols or TRADE_SYMBOLS
    if direction_filter not in (None, "long", "short"):
        raise ValueError(f"direction_filter must be 'long'/'short'/None, got {direction_filter!r}")

    async with get_db() as db:
        if reset:
            await db.execute("DELETE FROM backtest_trades")
            await db.commit()

        where = ["e.importance = 'high'"]
        params: list = []
        if days:
            from_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            where.append("e.time_utc >= ?")
            params.append(from_iso)
        where_sql = " AND ".join(where)

        cur = await db.execute(f"""
            SELECT e.id, e.title, e.time_utc, e.forecast_value, e.actual_value
            FROM events e
            WHERE {where_sql}
              AND EXISTS (
                  SELECT 1 FROM event_impact ei
                  WHERE ei.event_id = e.id
                    AND ei.expected_dir IS NOT NULL
                    AND ei.hit IN ('HIT', 'MISS')
                    AND ei.price_t1 IS NOT NULL
              )
            ORDER BY e.time_utc ASC
        """, params)
        candidates = await cur.fetchall()

        logger.info(f"[backtest] candidates={len(candidates)}, "
                    f"hit_rate_thr={hit_rate_threshold}, "
                    f"min_move={min_move_pct}, symbols={syms}, "
                    f"spot_long_only={spot_long_only}, "
                    f"exit_offset={exit_offset_min}, "
                    f"direction_filter={direction_filter}")

        opened = 0
        skipped_reasons: dict[str, int] = {}

        def _skip(reason: str, detail: str = ""):
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
            if detail:
                logger.debug(f"[backtest] skip {reason}: {detail}")

        for c in candidates:
            event_ts = _iso_to_ms(c["time_utc"])
            if event_ts is None:
                _skip("bad_time", c["title"])
                continue

            hits, misses, rate = await _hit_rate(db, c["title"], c["time_utc"])
            if hits + misses < min_events:
                _skip("n_too_small",
                      f"{c['title']} n={hits+misses} < {min_events}")
                continue
            if rate < hit_rate_threshold:
                _skip("hit_rate_low",
                      f"{c['title']} rate={rate:.0f}% < {hit_rate_threshold}%")
                continue

            for sym in syms:
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
                    _skip("no_impact", f"{c['title']} {sym}")
                    continue

                # Causal momentum: only candle data available by entry time.
                # min_move_pct is now the threshold for the PRE-ENTRY move,
                # not a future 15-minute return.
                pre_ts = event_ts
                pre_price = await _get_close(db, sym, "1", pre_ts, tolerance_min=0)
                entry_ts = event_ts + ENTRY_OFFSET_MIN * 60_000
                entry_price = await _get_close(db, sym, "1", entry_ts, tolerance_min=0)
                if not pre_price or not entry_price:
                    _skip("missing_causal_candles")
                    continue
                observed_move = (entry_price / pre_price - 1) * 100
                if abs(observed_move) < min_move_pct:
                    _skip("move_too_small")
                    continue

                direction = "long" if row["expected_dir"] == "up" else "short"

                # NEW: фільтр напрямку
                if direction_filter and direction != direction_filter:
                    _skip("direction_filter",
                          f"{c['title']} {sym} dir={direction} != {direction_filter}")
                    continue

                if spot_long_only and direction == "short":
                    _skip("spot_long_only",
                          f"{c['title']} {sym} dir={direction}")
                    continue

                cur = await db.execute("""
                    SELECT 1 FROM backtest_trades
                    WHERE event_time_utc = ? AND symbol = ? AND direction = ?
                """, (c["time_utc"], sym, direction))
                if await cur.fetchone():
                    _skip("duplicate_ts_symbol_direction",
                          f"{c['time_utc']} {sym} {direction} (вже є)")
                    continue

                if entry_price is None:
                    _skip("no_entry_candle",
                          f"{c['title']} {sym} entry_ts={entry_ts}")
                    continue

                if direction == "long":
                    entry_price_eff = entry_price * (1 + SLIPPAGE_PCT / 100)
                else:
                    entry_price_eff = entry_price * (1 - SLIPPAGE_PCT / 100)
                entry_fee = trade_size_usd * (COMMISSION_PCT / 100)

                exit_ts = event_ts + exit_offset_min * 60_000
                exit_price = await _get_close(db, sym, "1", exit_ts, tolerance_min=0)
                if exit_price is None:
                    _skip("no_exit_candle",
                          f"{c['title']} {sym} exit_ts={exit_ts}")
                    continue

                if direction == "long":
                    exit_price_eff = exit_price * (1 - SLIPPAGE_PCT / 100)
                else:
                    exit_price_eff = exit_price * (1 + SLIPPAGE_PCT / 100)

                qty = trade_size_usd / entry_price_eff
                exit_notional = qty * exit_price_eff
                exit_fee = exit_notional * (COMMISSION_PCT / 100)

                if direction == "long":
                    gross_pnl = qty * (exit_price_eff - entry_price_eff)
                else:
                    gross_pnl = qty * (entry_price_eff - exit_price_eff)

                pnl = gross_pnl - entry_fee - exit_fee
                pnl_pct = (pnl / trade_size_usd) * 100

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
                    trade_size_usd, pnl, pnl_pct,
                    f"past_hit_rate={rate:.0f}% |pre_entry_move|={abs(observed_move):.2f}%",
                ))
                opened += 1

        await db.commit()

        # Підсумок
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
            SELECT symbol, COUNT(*) AS n,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(pnl) AS total_pnl
            FROM backtest_trades GROUP BY symbol
        """)
        by_sym = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute("""
            SELECT direction, COUNT(*) AS n,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(pnl) AS total_pnl
            FROM backtest_trades GROUP BY direction
        """)
        by_direction = [dict(r) for r in await cur.fetchall()]

        # NEW: агрегація по незалежних подіях (event_time_utc + direction)
        cur = await db.execute("""
            SELECT
                COUNT(*) AS n_events,
                SUM(CASE WHEN pnl_sum > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN pnl_sum <= 0 THEN 1 ELSE 0 END) AS losses,
                SUM(pnl_sum) AS total_pnl,
                AVG(pnl_sum) AS avg_pnl
            FROM (
                SELECT event_time_utc, direction, SUM(pnl) AS pnl_sum
                FROM backtest_trades
                GROUP BY event_time_utc, direction
            )
        """)
        se = await cur.fetchone()

    n = s["n"] or 0
    wins = s["wins"] or 0
    losses = s["losses"] or 0
    total_pnl = s["total_pnl"] or 0
    win_rate = (wins / n * 100) if n > 0 else 0

    n_events = se["n_events"] or 0
    ev_wins = se["wins"] or 0
    ev_losses = se["losses"] or 0
    ev_total_pnl = se["total_pnl"] or 0
    ev_win_rate = (ev_wins / n_events * 100) if n_events > 0 else 0

    return {
        "params": {
            "days": days,
            "hit_rate_threshold": hit_rate_threshold,
            "min_move_pct": min_move_pct,
            "min_events": min_events,
            "symbols": syms,
            "spot_long_only": spot_long_only,
            "exit_offset_min": exit_offset_min,
            "trade_size_usd": trade_size_usd,
            "direction_filter": direction_filter,
        },
        "candidates": len(candidates),
        "opened": opened,
        "skipped_reasons": skipped_reasons,
        "summary": {
            "n": n,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 4),
            "avg_pnl": round(s["avg_pnl"] or 0, 4),
            "avg_win": round(s["avg_win"] or 0, 4),
            "avg_loss": round(s["avg_loss"] or 0, 4),
        },
        "by_events": {
            "n": n_events,
            "wins": ev_wins,
            "losses": ev_losses,
            "win_rate": round(ev_win_rate, 1),
            "total_pnl": round(ev_total_pnl, 4),
            "avg_pnl": round(se["avg_pnl"] or 0, 4),
        },
        "by_symbol": [
            {
                "symbol": r["symbol"],
                "n": r["n"] or 0,
                "wins": r["wins"] or 0,
                "win_rate": round((r["wins"] or 0) / r["n"] * 100, 1) if r["n"] else 0,
                "total_pnl": round(r["total_pnl"] or 0, 4),
            }
            for r in by_sym
        ],
        "by_direction": [
            {
                "direction": r["direction"],
                "n": r["n"] or 0,
                "wins": r["wins"] or 0,
                "win_rate": round((r["wins"] or 0) / r["n"] * 100, 1) if r["n"] else 0,
                "total_pnl": round(r["total_pnl"] or 0, 4),
            }
            for r in by_direction
        ],
    }


async def get_backtest_trades(limit: int = 500) -> list[dict]:
    async with get_db() as db:
        cur = await db.execute("""
            SELECT * FROM backtest_trades
            ORDER BY event_time_utc DESC, symbol ASC, direction ASC
            LIMIT ?
        """, (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def get_backtest_equity_curve() -> list[dict]:
    async with get_db() as db:
        cur = await db.execute("""
            SELECT id, event_time_utc, pnl
            FROM backtest_trades
            WHERE status = 'CLOSED'
            ORDER BY event_time_utc ASC, id ASC
        """)
        rows = await cur.fetchall()

    points = []
    cum = 0.0
    for r in rows:
        cum += float(r["pnl"] or 0)
        points.append({
            "trade_id": r["id"],
            "ts": r["event_time_utc"],
            "cum_pnl": round(cum, 4),
        })
    return points


async def clear_backtest() -> dict:
    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*) AS n FROM backtest_trades")
        n = (await cur.fetchone())["n"]
        await db.execute("DELETE FROM backtest_trades")
        await db.commit()
    return {"deleted": n}