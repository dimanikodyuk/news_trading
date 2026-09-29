"""
WebSocket Bybit: kline-стрім для SOL/BTC/ETH.

ВАЖЛИВО: pybit WebSocket працює у власному потоці і НЕ сумісний
з asyncio event loop FastAPI. Тому запускаємо його як звичайний
Python-потік (threading.Thread), а не як asyncio-task.
"""

import asyncio
import logging
import sqlite3
import threading
import time
from pathlib import Path

from pybit.unified_trading import WebSocket

from app.config import settings

logger = logging.getLogger(__name__)

SYMBOLS = ["SOLUSDT", "BTCUSDT", "ETHUSDT"]

# Глобальний стан
_ws_instances: list = []
_last_save_ts: dict[str, int] = {}
_thread: threading.Thread | None = None


def _save_kline(symbol: str, interval: str, data: dict):
    """Синхронний запис свічки у SQLite."""
    db_path = Path(settings.db_path_abs)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
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
            _last_save_ts[symbol] = int(data["start"])
        finally:
            conn.close()
    except Exception as ex:
        logger.warning(f"[{symbol}] _save_kline failed: {ex}")


def _make_handler(symbol: str, interval: str):
    def handler(message: dict):
        try:
            topic = message.get("topic", "")
            if topic.startswith("kline"):
                data_list = message.get("data", [])
                for candle in data_list:
                    _save_kline(symbol, interval, candle)
        except Exception as ex:
            logger.warning(f"[{symbol}] handler error: {ex}")
    return handler


def start_ws_thread() -> threading.Thread:
    """Запускає WebSocket у фоновому потоці."""
    global _thread
    if _thread is not None and _thread.is_alive():
        logger.info("[WS] Потік вже запущено, пропускаємо")
        return _thread

    def _runner():
        interval = settings.bybit_kline_interval
        logger.info(f"[WS] Запуск потоку WebSocket (interval={interval})")

        try:
            ws = WebSocket(testnet=False, channel_type="spot")
            _ws_instances.append(ws)

            for sym in SYMBOLS:
                try:
                    ws.kline_stream(
                        interval=interval,
                        symbol=sym,
                        callback=_make_handler(sym, str(interval)),
                    )
                    logger.info(f"[WS] Subscribed to kline.{interval}.{sym}")
                except Exception as ex:
                    logger.error(f"[WS] Помилка підписки на {sym}: {ex}")

            # Тримаємо потік живим + health-перевірка
            while True:
                time.sleep(60)
                for sym in SYMBOLS:
                    ts = _last_save_ts.get(sym)
                    if ts:
                        age = (time.time() * 1000 - ts) / 1000
                        if age > 600:
                            logger.warning(f"[WS] {sym}: немає нових свічок {age:.0f}с")

        except Exception as ex:
            logger.exception(f"[WS] Критична помилка в потоці: {ex}")

    _thread = threading.Thread(target=_runner, daemon=True, name="bybit-ws")
    _thread.start()
    logger.info("[WS] Потік запущено")
    return _thread


async def run_ws():
    """Застаріла async-обгортка. Використовуй start_ws_thread()."""
    start_ws_thread()
    while True:
        await asyncio.sleep(3600)