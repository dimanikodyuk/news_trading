import asyncio
import json
import logging
from pybit.unified_trading import WebSocket
from app.config import settings
from app.db import get_db

logger = logging.getLogger(__name__)

def _save_kline(data: dict):
    import sqlite3
    from pathlib import Path
    from app.config import settings

    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute("""
            INSERT OR REPLACE INTO prices
            (symbol, interval, ts, open, close, high, low, volume, confirm)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            settings.bybit_symbol,
            str(settings.bybit_kline_interval),
            int(data["start"]),
            float(data["open"]), float(data["close"]),
            float(data["high"]), float(data["low"]),
            float(data["volume"]), 1 if data.get("confirm") else 0,
        ))
        conn.commit()
    finally:
        conn.close()

def handle_kline(message: dict):
    """Callback від Bybit WebSocket."""
    if message.get("topic", "").startswith("kline"):
        for candle in message.get("data", []):
            _save_kline(candle)
            if candle.get("confirm"):
                logger.debug(f"Closed candle: {candle['close']}")

async def run_ws():
    """Запускає WebSocket у окремому потоці (pybit не async)."""
    ws = WebSocket(
        testnet=False,
        channel_type="spot",
    )
    ws.kline_stream(
        interval=settings.bybit_kline_interval,
        symbol=settings.bybit_symbol,
        callback=handle_kline,
    )
    logger.info(f"Subscribed to kline.{settings.bybit_kline_interval}.{settings.bybit_symbol}")
    while True:
        await asyncio.sleep(60)