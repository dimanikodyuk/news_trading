"""
Розрахунок впливу новини на ціну SOLUSDT.

Для кожної події рахує:
- baseline (T-1min close)
- ціну через T+1, T+5, T+15, T+30, T+60 хвилин
- % зміни
- напрямок (up/down/flat) за динамічним порогом (ATR-based)
"""

import logging
from typing import Optional
from statistics import median

import aiosqlite

logger = logging.getLogger(__name__)

# Вікна у хвилинах
WINDOWS = [1, 5, 15, 30, 60]

# Мінімальний поріг (у %) — щоб у спокійні дні не було 100% flat
MIN_THRESHOLD_PCT = 0.10

# Множник для динамічного порогу (2 * медіанна 1m-волатильність)
THRESHOLD_MULT = 2.0

# Скільки хвилин історії брати для оцінки волатильності
VOLATILITY_LOOKBACK_MIN = 24 * 60


async def compute_impact(db: aiosqlite.Connection,
                         event_row: aiosqlite.Row) -> Optional[dict]:
    """
    Обчислює impact однієї події. Повертає dict для вставки в event_impact
    або None, якщо даних недостатньо.
    """
    from app.config import settings

    symbol = settings.bybit_symbol
    interval = str(settings.bybit_kline_interval)  # "1"

    # Парсимо час події у мілісекунди
    event_ts_ms = _iso_to_ms(event_row["time_utc"])
    if event_ts_ms is None:
        return None

    # Baseline: T-1 хвилина
    baseline_ts = event_ts_ms - 60_000
    baseline = await _get_close(db, symbol, interval, baseline_ts)
    if baseline is None:
        logger.debug(f"[event {event_row['id']}] немає baseline")
        return None

    # Ціни у вікнах
    prices: dict[int, Optional[float]] = {}
    for w in WINDOWS:
        target_ts = event_ts_ms + w * 60_000
        prices[w] = await _get_close(db, symbol, interval, target_ts)

    if all(v is None for v in prices.values()):
        logger.debug(f"[event {event_row['id']}] немає жодної ціни у вікнах")
        return None

    # Динамічний поріг
    threshold = await _compute_threshold(db, symbol, interval, event_ts_ms)

    # Розрахунок
    result = {
        "event_id": event_row["id"],
        "symbol": symbol,
        "importance": event_row["importance"],
        "price_baseline": baseline,
        "threshold_pct": threshold,
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

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_close(db: aiosqlite.Connection,
                     symbol: str, interval: str,
                     target_ts_ms: int,
                     tolerance_min: int = 2) -> Optional[float]:
    """
    Повертає close найближчої свічки до target_ts_ms у межах ±tolerance_min хвилин.
    """
    tol_ms = tolerance_min * 60_000
    lo = target_ts_ms - tol_ms
    hi = target_ts_ms + tol_ms

    cur = await db.execute("""
        SELECT ts, close FROM prices
        WHERE symbol = ? AND interval = ?
          AND ts BETWEEN ? AND ?
          AND confirm = 1
        ORDER BY ABS(ts - ?) ASC
        LIMIT 1
    """, (symbol, interval, lo, hi, target_ts_ms))
    row = await cur.fetchone()
    return float(row["close"]) if row else None


async def _compute_threshold(db: aiosqlite.Connection,
                             symbol: str, interval: str,
                             event_ts_ms: int) -> float:
    """
    Динамічний поріг: 2 * медіанна |%| зміна за 1 хв
    на вікні VOLATILITY_LOOKBACK_MIN до події.
    Мінімум — MIN_THRESHOLD_PCT.
    """
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
    """ISO 8601 → epoch ms."""
    from datetime import datetime
    try:
        # Підтримка 'Z' та '+00:00'
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None