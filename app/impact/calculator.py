"""
Розрахунок впливу новини на ціну SOLUSDT.
"""

import logging
from typing import Optional
from statistics import median

import aiosqlite

from app.impact.expected import get_expected_direction, classify_hit

logger = logging.getLogger(__name__)

WINDOWS = [1, 5, 15, 30, 60]
MIN_THRESHOLD_PCT = 0.10
THRESHOLD_MULT = 2.0
VOLATILITY_LOOKBACK_MIN = 24 * 60


async def compute_impact(db: aiosqlite.Connection,
                         event_row: aiosqlite.Row) -> Optional[dict]:
    from app.config import settings

    symbol = settings.bybit_symbol
    interval = str(settings.bybit_kline_interval)

    event_ts_ms = _iso_to_ms(event_row["time_utc"])
    if event_ts_ms is None:
        return None

    baseline_ts = event_ts_ms - 60_000
    baseline = await _get_close(db, symbol, interval, baseline_ts)
    if baseline is None:
        logger.debug(f"[event {event_row['id']}] немає baseline")
        return None

    prices: dict[int, Optional[float]] = {}
    for w in WINDOWS:
        target_ts = event_ts_ms + w * 60_000
        prices[w] = await _get_close(db, symbol, interval, target_ts)

    if all(v is None for v in prices.values()):
        logger.debug(f"[event {event_row['id']}] немає жодної ціни у вікнах")
        return None

    threshold = await _compute_threshold(db, symbol, interval, event_ts_ms)

    # --- Expected / Hit ---
    expected = get_expected_direction(
        event_row["title"],
        event_row["forecast_value"],
        event_row["actual_value"],
    )

    result = {
        "event_id": event_row["id"],
        "symbol": symbol,
        "importance": event_row["importance"],
        "price_baseline": baseline,
        "threshold_pct": threshold,
        "expected_dir": expected,
    }

    for w in WINDOWS:
        p = prices[w]
        if p is None:
            result[f"price_t{w}"] = None
            result[f"ret_{w}m"] = None
            result[f"dir_{w}m"] = None
            continue

        ret_pct = (p - baseline) / baseline * 100.0
        result[f"price_t{w}"] = p
        result[f"ret_{w}m"] = ret_pct
        result[f"dir_{w}m"] = _classify(ret_pct, threshold)

    # Hit рахуємо за 15m (основне вікно)
    result["hit"] = classify_hit(expected, result.get("dir_15m"))

    return result


async def _get_close(db, symbol, interval, target_ts_ms, tolerance_min=2):
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


async def _compute_threshold(db, symbol, interval, event_ts_ms):
    lo = event_ts_ms - VOLATILITY_LOOKBACK_MIN * 60_000
    hi = event_ts_ms

    cur = await db.execute("""
        SELECT open, close FROM prices
        WHERE symbol = ? AND interval = ?
          AND ts BETWEEN ? AND ?
          AND confirm = 1
        ORDER BY ts ASC
    """, (symbol, interval, lo, hi))
    rows = await cur.fetchall()

    if len(rows) < 30:
        return MIN_THRESHOLD_PCT

    changes = []
    for r in rows:
        o, c = float(r["open"]), float(r["close"])
        if o > 0:
            changes.append(abs((c - o) / o * 100.0))

    if not changes:
        return MIN_THRESHOLD_PCT

    med = median(changes)
    threshold = THRESHOLD_MULT * med
    return max(threshold, MIN_THRESHOLD_PCT)


def _classify(ret_pct: float, threshold: float) -> str:
    if ret_pct > threshold:
        return "up"
    if ret_pct < -threshold:
        return "down"
    return "flat"


def _iso_to_ms(iso_str: str) -> Optional[int]:
    from datetime import datetime
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None