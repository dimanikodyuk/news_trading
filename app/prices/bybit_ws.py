"""
WebSocket Bybit: kline-стрім для SOL/BTC/ETH.

Фічі:
- Reconnect при розриві з'єднання (Bybit рве кожні 24 год)
- Health-check кожні 60 сек
- Якщо немає нових свічок > 3 хв — перепідключення
"""

import logging
import sqlite3
import threading
import time
from pathlib import Path

from pybit.unified_trading import WebSocket

from app.config import settings

logger = logging.getLogger(__name__)

SYMBOLS = ["SOLUSDT", "BTCUSDT", "ETHUSDT"]

_last_save_ts: dict[str, int] = {}
_ws_instances: list = []
_stop_flag = threading.Event()


def _save_kline(symbol: str, interval: str, data: dict):
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
            _last_save_ts[symbol] = int(time.time() * 1000)
        finally:
            conn.close()
    except Exception as ex:
        logger.warning(f"[{symbol}] _save_kline failed: {ex}")


def _make_handler(symbol: str, interval: str):
    def handler(message: dict):
        try:
            if message.get("topic", "").startswith("kline"):
                for candle in message.get("data", []):
                    _save_kline(symbol, interval, candle)
        except Exception as ex:
            logger.warning(f"[{symbol}] handler error: {ex}")
    return handler


def _connect_once():
    """Створює WebSocket, підписується. Повертає інстанс або None."""
    interval = settings.bybit_kline_interval
    try:
        ws = WebSocket(testnet=False, channel_type="spot")
        _ws_instances.append(ws)
        for sym in SYMBOLS:
            ws.kline_stream(
                interval=interval,
                symbol=sym,
                callback=_make_handler(sym, str(interval)),
            )
            logger.info(f"[WS] Subscribed to kline.{interval}.{sym}")
        return ws
    except Exception as ex:
        logger.error(f"[WS] Помилка підключення: {ex}")
        return None


def start_ws_thread() -> threading.Thread:
    """Запускає WebSocket з автоматичним reconnect у фоновому потоці."""

    def _runner():
        logger.info("[WS] Потік запущено")
        while not _stop_flag.is_set():
            try:
                ws = _connect_once()
                if ws is None:
                    logger.warning("[WS] Не вдалось підключитись, чекаю 30с...")
                    time.sleep(30)
                    continue

                logger.info("[WS] Підключено. Моніторю активність...")

                # Health-check: якщо свічок немає > 3 хв → перепідключення
                while not _stop_flag.is_set():
                    time.sleep(30)
                    now_ms = int(time.time() * 1000)
                    dead_symbols = []
                    for sym in SYMBOLS:
                        last = _last_save_ts.get(sym, 0)
                        if last == 0 or (now_ms - last) > 3 * 60 * 1000:
                            dead_symbols.append(sym)
                    if dead_symbols:
                        logger.warning(
                            f"[WS] Немає свічок >3хв: {dead_symbols}. "
                            f"Перепідключення..."
                        )
                        break

            except Exception as ex:
                logger.exception(f"[WS] Помилка в циклі: {ex}")
                time.sleep(10)

            # Cleanup перед реконектом
            time.sleep(5)

        logger.info("[WS] Потік зупинено")

    t = threading.Thread(target=_runner, daemon=True, name="bybit-ws")
    t.start()
    return t