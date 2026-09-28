"""
Ініціалізація та міграції БД.
"""

import aiosqlite
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from app.config import settings

logger = logging.getLogger(__name__)


def _ensure_db_dir():
    Path(settings.db_path_abs).parent.mkdir(parents=True, exist_ok=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    title TEXT NOT NULL,
    country TEXT,
    importance TEXT,
    time_utc TEXT NOT NULL,
    forecast_value TEXT,
    previous_value TEXT,
    actual_value TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(provider, title, time_utc)
);

CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    ts INTEGER NOT NULL,
    open REAL, close REAL, high REAL, low REAL,
    volume REAL,
    confirm INTEGER DEFAULT 0,
    UNIQUE(symbol, interval, ts)
);

CREATE TABLE IF NOT EXISTS event_impact (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    importance TEXT,

    -- Ціни
    price_baseline REAL,
    price_t1 REAL, price_t5 REAL, price_t15 REAL, price_t30 REAL, price_t60 REAL,

    -- % зміни
    ret_1m REAL, ret_5m REAL, ret_15m REAL, ret_30m REAL, ret_60m REAL,

    -- Напрямок (up/down/flat)
    dir_1m TEXT, dir_5m TEXT, dir_15m TEXT, dir_30m TEXT, dir_60m TEXT,

    -- Очікування та влучання
    expected_dir TEXT,   -- up/down/flat/None
    hit TEXT,            -- HIT/MISS/NEUTRAL/N/A

    threshold_pct REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_events_time ON events(time_utc);
CREATE INDEX IF NOT EXISTS idx_prices_ts ON prices(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_impact_event ON event_impact(event_id);
CREATE INDEX IF NOT EXISTS idx_impact_hit ON event_impact(hit);
"""


@asynccontextmanager
async def get_db():
    _ensure_db_dir()
    db = await aiosqlite.connect(settings.db_path_abs)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


async def init_db():
    _ensure_db_dir()
    async with get_db() as db:
        await db.executescript(SCHEMA)
        await db.commit()

        # --- Міграції для існуючих БД ---
        await _migrate(db)


async def _migrate(db: aiosqlite.Connection):
    """Додає нові колонки у вже існуючі таблиці (ідемпотентно)."""

    async def add_col(table: str, col: str, coltype: str):
        try:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
            await db.commit()
            logger.info(f"Міграція: додано {table}.{col}")
        except Exception:
            # Колонка вже є — нормально
            pass

    await add_col("event_impact", "expected_dir", "TEXT")
    await add_col("event_impact", "hit", "TEXT")