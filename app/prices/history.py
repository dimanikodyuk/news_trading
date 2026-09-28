"""
Завантаження історичних свічок SOLUSDT через Bybit REST.

Використовується і скриптом scripts/load_history.py, і scheduler-ом
усередині FastAPI (щоденне довантаження останніх 2 днів).
"""

import time
import sqlite3
import logging
from pathlib import Path

from pybit.unified_trading import HTTP

from app.config import settings

logger = logging.getLogger(__name__)


def load_history_sync(days: int = 30, interval: str = "1") -> int:
    """
    Синхронне завантаження свічок SOLUSDT за останні `days` днів.
    Повертає кількість збережених свічок.
    """
    session = HTTP(testnet=False)

    symbol = settings.bybit_symbol
    db_path = Path(settings.db_path_abs)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    limit = 1000
    end_ts = None
    total = 0
    page = 0

    cutoff_ms = int((time.time() - days * 24 * 60 * 60) * 1000)

    while True:
        page += 1
        params = {
            "category": "spot",
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if end_ts:
            params["end"] = end_ts

        # --- Retry ×3 з backoff ---
        resp = None
        for attempt in range(3):
            try:
                resp = session.get_kline(**params)
                break
            except Exception as ex:
                logger.warning(f"Спроба {attempt + 1}/3 не вдалась: {ex}")
                time.sleep(1.5 * (attempt + 1))

        if resp is None:
            logger.error("Bybit недоступний після 3 спроб. Зупиняємось.")
            break

        if resp.get("retCode") != 0:
            logger.error(f"Bybit error: {resp}")
            break

        candles = resp["result"]["list"]
        if not candles:
            logger.info("Порожня відповідь — досягли кінця історії.")
            break

        for c in candles:
            ts = int(c[0])
            conn.execute("""
                INSERT OR REPLACE INTO prices
                (symbol, interval, ts, open, high, low, close, volume, confirm)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (
                symbol, interval, ts,
                float(c[1]), float(c[2]), float(c[3]),
                float(c[4]), float(c[5]),
            ))
            total += 1

        conn.commit()

        oldest_ts = int(candles[-1][0])
        end_ts = oldest_ts - 1

        if page % 5 == 0 or oldest_ts < cutoff_ms:
            logger.info(
                f"Сторінка {page:3}: +{len(candles)} (всього {total}), "
                f"найстаріша = {_ts_to_str(oldest_ts)}"
            )

        if oldest_ts < cutoff_ms:
            break

        time.sleep(0.15)

    conn.close()
    logger.info(f"load_history_sync: {total} свічок (days={days})")
    return total


def _ts_to_str(ts_ms: int) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(
        ts_ms / 1000, tz=_dt.timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")