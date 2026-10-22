"""
"Тупо тест" (naive test) — угода на КОЖНУ high-подію, без фільтрів paper.

Варіант B: зважене голосування.
- Кожен up = +1, кожен down = -1.
- score = votes_up - votes_down.
- score > 0 → LONG, score < 0 → SHORT, score == 0 → пропуск.
- Це стійкіше до склеєних подій (4 копії CPI m/m дадуть -4, а не -3).

Пише у окрему таблицю naive_trades.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from app.db import get_db
from app.impact.expected import get_expected_direction

logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = ["SOLUSDT", "BTCUSDT", "ETHUSDT"]
DEFAULT_SIZE_USD = 10.0
DEFAULT_INITIAL_BALANCE = 200.0
COMMISSION_PCT = 0.1
SLIPPAGE_PCT = 0.05
ENTRY_OFFSET_MIN = 1
EXIT_OFFSET_MIN = 60
GROUP_TOLERANCE_SEC = 60


def _iso_to_ms(iso_str: str) -> Optional[int]:
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


async def _get_close(db, symbol: str, interval: str, target_ts_ms: int,
                     tolerance_min: int = 3) -> Optional[float]:
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


def _group_key(time_utc: str) -> Optional[str]:
    dt = _iso_to_dt(time_utc)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M")


def _iso_to_dt(iso_str: str) -> Optional[datetime]:
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _vote_direction(votes_up: int, votes_down: int) -> Optional[str]:
    """
    Варіант B: score = up - down.
    score > 0 → long, score < 0 → short, score == 0 → None.
    """
    score = votes_up - votes_down
    if score > 0:
        return "long"
    if score < 0:
        return "short"
    return None


async def run_naive_test(
    days: Optional[int] = None,
    symbols: Optional[list[str]] = None,
    size_usd: float = DEFAULT_SIZE_USD,
    exit_offset_min: int = EXIT_OFFSET_MIN,
    reset: bool = True,
) -> dict:
    syms = symbols or DEFAULT_SYMBOLS
    interval = "1"

    async with get_db() as db:
        if reset:
            await db.execute("DELETE FROM naive_trades")
            await db.commit()

        where = ["e.importance = 'high'"]
        params: list = []
        if days:
            from_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            where.append("e.time_utc >= ?")
            params.append(from_iso)
        where_sql = " AND ".join(where)

        cur = await db.execute(f"""
            SELECT e.id, e.title, e.time_utc,
                   e.forecast_value, e.actual_value
            FROM events e
            WHERE {where_sql}
            ORDER BY e.time_utc ASC
        """, params)
        all_events = await cur.fetchall()

        logger.info(f"[naive] всього high-подій: {len(all_events)}")

        groups: dict[str, list] = {}
        for ev in all_events:
            gk = _group_key(ev["time_utc"])
            if gk is None:
                continue
            groups.setdefault(gk, []).append(ev)

        logger.info(f"[naive] груп (унікальних хвилин): {len(groups)}")

        opened = 0
        skipped_reasons: dict[str, int] = {}

        def _skip(reason: str):
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1

        for gk, events_in_group in groups.items():
            votes_up = 0
            votes_down = 0
            titles = []
            event_ids = []
            group_time_utc = events_in_group[0]["time_utc"]

            for ev in events_in_group:
                event_ids.append(ev["id"])
                titles.append(ev["title"])
                direction = get_expected_direction(
                    ev["title"],
                    ev["forecast_value"],
                    ev["actual_value"],
                )
                if direction == "up":
                    votes_up += 1
                elif direction == "down":
                    votes_down += 1

            trade_direction = _vote_direction(votes_up, votes_down)
            if trade_direction is None:
                _skip("vote_tie")
                continue

            event_ts_ms = _iso_to_ms(group_time_utc)
            if event_ts_ms is None:
                _skip("bad_time")
                continue

            group_title = " | ".join(titles)
            score = votes_up - votes_down

            for sym in syms:
                cur = await db.execute("""
                    SELECT 1 FROM naive_trades
                    WHERE event_group_key = ? AND symbol = ?
                """, (gk, sym))
                if await cur.fetchone():
                    _skip("duplicate")
                    continue

                entry_ts = event_ts_ms + ENTRY_OFFSET_MIN * 60_000
                entry_price = await _get_close(db, sym, interval, entry_ts)
                if entry_price is None:
                    _skip("no_entry_candle")
                    continue

                if trade_direction == "long":
                    entry_price_eff = entry_price * (1 + SLIPPAGE_PCT / 100)
                else:
                    entry_price_eff = entry_price * (1 - SLIPPAGE_PCT / 100)
                entry_fee = size_usd * (COMMISSION_PCT / 100)

                exit_ts = event_ts_ms + exit_offset_min * 60_000
                exit_price = await _get_close(db, sym, interval, exit_ts)
                if exit_price is None:
                    _skip("no_exit_candle")
                    continue

                if trade_direction == "long":
                    exit_price_eff = exit_price * (1 - SLIPPAGE_PCT / 100)
                else:
                    exit_price_eff = exit_price * (1 + SLIPPAGE_PCT / 100)

                qty = size_usd / entry_price_eff
                exit_notional = qty * exit_price_eff
                exit_fee = exit_notional * (COMMISSION_PCT / 100)

                if trade_direction == "long":
                    gross_pnl = qty * (exit_price_eff - entry_price_eff)
                else:
                    gross_pnl = qty * (entry_price_eff - exit_price_eff)

                pnl = gross_pnl - entry_fee - exit_fee
                pnl_pct = (pnl / size_usd) * 100 if size_usd > 0 else 0

                await db.execute("""
                    INSERT INTO naive_trades
                    (event_group_key, event_ids, event_title, event_time_utc,
                     symbol, direction, votes_up, votes_down,
                     entry_ts, entry_price, entry_fee,
                     exit_ts, exit_price, exit_fee,
                     size_usd, pnl, pnl_pct, status, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLOSED', ?)
                """, (
                    gk,
                    ",".join(str(x) for x in event_ids),
                    group_title,
                    group_time_utc,
                    sym,
                    trade_direction,
                    votes_up,
                    votes_down,
                    entry_ts,
                    entry_price_eff,
                    entry_fee,
                    exit_ts,
                    exit_price_eff,
                    exit_fee,
                    size_usd,
                    pnl,
                    pnl_pct,
                    f"score={score} (up={votes_up} down={votes_down})",
                ))
                opened += 1

        await db.commit()

        cur = await db.execute("""
            SELECT
                COUNT(*) AS n,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) AS losses,
                SUM(pnl) AS total_pnl,
                AVG(pnl) AS avg_pnl,
                AVG(CASE WHEN pnl > 0 THEN pnl END) AS avg_win,
                AVG(CASE WHEN pnl <= 0 THEN pnl END) AS avg_loss
            FROM naive_trades
        """)
        s = await cur.fetchone()

        cur = await db.execute("""
            SELECT symbol,
                   COUNT(*) AS n,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(pnl) AS total_pnl
            FROM naive_trades GROUP BY symbol
        """)
        by_sym = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute("""
            SELECT direction,
                   COUNT(*) AS n,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(pnl) AS total_pnl
            FROM naive_trades GROUP BY direction
        """)
        by_dir = [dict(r) for r in await cur.fetchall()]

    n = s["n"] or 0
    wins = s["wins"] or 0
    losses = s["losses"] or 0
    total_pnl = s["total_pnl"] or 0
    win_rate = (wins / n * 100) if n > 0 else 0

    initial_balance = DEFAULT_INITIAL_BALANCE
    final_balance = initial_balance + total_pnl
    total_pnl_pct = (total_pnl / initial_balance * 100) if initial_balance > 0 else 0

    return {
        "params": {
            "days": days,
            "symbols": syms,
            "size_usd": size_usd,
            "exit_offset_min": exit_offset_min,
            "initial_balance": initial_balance,
            "vote_mode": "weighted (B)",
        },
        "high_events_total": len(all_events),
        "groups_total": len(groups),
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
            "initial_balance": initial_balance,
            "final_balance": round(final_balance, 4),
            "total_pnl_pct": round(total_pnl_pct, 3),
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
            for r in by_dir
        ],
    }


async def get_naive_trades(limit: int = 1000) -> list[dict]:
    async with get_db() as db:
        cur = await db.execute("""
            SELECT * FROM naive_trades
            ORDER BY event_time_utc DESC, symbol ASC
            LIMIT ?
        """, (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def get_naive_equity_curve() -> list[dict]:
    async with get_db() as db:
        cur = await db.execute("""
            SELECT id, event_time_utc, pnl
            FROM naive_trades
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


async def clear_naive() -> dict:
    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*) AS n FROM naive_trades")
        n = (await cur.fetchone())["n"]
        await db.execute("DELETE FROM naive_trades")
        await db.commit()
    return {"deleted": n}