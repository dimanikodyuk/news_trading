"""
WebSocket Bybit: підписка на kline для SOLUSDT, BTCUSDT, ETHUSDT.
"""

import asyncio
import logging
import sqlite3
from pathlib import Path

from pybit.unified_trading import WebSocket
from app.config import settings

logger = logging.getLogger(__name__)

# Символи для моніторингу
SYMBOLS = ["SOLUSDT", "BTCUSDT", "ETHUSDT"]


def _save_kline(symbol: str, interval: str, data: dict):
    """Синхронний запис свічки у SQLite (викликається з callback-потоку pybit)."""
    db_path = Path(settings.db_path_abs)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("""
            INSERT OR REPLACE INTO prices
            (symbol, interval, ts, open, close, high, low, volume, confirm)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol, interval,
            int(data["start"]),
            float(data["open"]), float(data["close"]),
            float(data["high"]), float(data["low"]),
            float(data["volume"]),
            1 if data.get("confirm") else 0,
        ))
        conn.commit()
    finally:
        conn.close()


def _make_handler(symbol: str, interval: str):
    """Створює callback для конкретного символу."""
    def handler(message: dict):
        if message.get("topic", "").startswith("kline"):
            for candle in message.get("data", []):
                _save_kline(symbol, interval, candle)
    return handler


async def run_ws():
    """Запускає WebSocket для всіх символів у фоновому режимі."""
    interval = settings.bybit_kline_interval

    ws = WebSocket(testnet=False, channel_type="spot")

    for sym in SYMBOLS:
        ws.kline_stream(
            interval=interval,
            symbol=sym,
            callback=_make_handler(sym, str(interval)),
        )
        logger.info(f"Subscribed to kline.{interval}.{sym}")

    while True:
        await asyncio.sleep(60)