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


TABLES_SQL = """
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
    price_baseline REAL,
    price_t1 REAL, price_t5 REAL, price_t15 REAL, price_t30 REAL, price_t60 REAL,
    ret_1m REAL, ret_5m REAL, ret_15m, ret_30m, ret_60m,
    dir_1m TEXT, dir_5m TEXT, dir_15m TEXT, dir_30m TEXT, dir_60m TEXT,
    expected_dir TEXT,
    hit TEXT,
    threshold_pct REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_id, symbol)
);

CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initial_balance REAL NOT NULL DEFAULT 100.0,
    balance REAL NOT NULL DEFAULT 100.0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    event_title TEXT,
    event_time_utc TEXT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_ts INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    entry_fee REAL NOT NULL,
    exit_ts INTEGER,
    exit_price REAL,
    exit_fee REAL,
    size_usd REAL NOT NULL,
    pnl REAL,
    pnl_pct REAL,
    status TEXT NOT NULL DEFAULT 'CLOSED',
    reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_time_utc, symbol, direction)
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_ts INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    entry_fee REAL NOT NULL,
    exit_ts INTEGER,
    exit_price REAL,
    exit_fee REAL,
    size_usd REAL NOT NULL,
    pnl REAL,
    pnl_pct REAL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    reason TEXT,
    opened_at TEXT DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT,
    UNIQUE(event_id, symbol)
);

CREATE TABLE IF NOT EXISTS event_reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_id)
);

-- NAIVE: "тупо тест" — угода на КОЖНУ high-подію (без фільтрів paper)
CREATE TABLE IF NOT EXISTS naive_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_group_key TEXT NOT NULL,
    event_ids TEXT,
    event_title TEXT,
    event_time_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    votes_up INTEGER NOT NULL DEFAULT 0,
    votes_down INTEGER NOT NULL DEFAULT 0,
    entry_ts INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    entry_fee REAL NOT NULL,
    exit_ts INTEGER,
    exit_price REAL,
    exit_fee REAL,
    size_usd REAL NOT NULL,
    pnl REAL,
    pnl_pct REAL,
    status TEXT NOT NULL DEFAULT 'CLOSED',
    reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_group_key, symbol)
);

-- LIVE: реальні угоди на Bybit (записуються ТІЛЬКИ якщо live_enabled=True)
CREATE TABLE IF NOT EXISTS live_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_group_key TEXT NOT NULL,
    event_ids TEXT,
    event_title TEXT,
    event_time_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    votes_up INTEGER NOT NULL DEFAULT 0,
    votes_down INTEGER NOT NULL DEFAULT 0,
    size_usd REAL NOT NULL,
    bybit_order_id TEXT,
    entry_price REAL,
    entry_ts INTEGER,
    status TEXT NOT NULL DEFAULT 'PENDING',
    error_message TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_group_key, symbol)
);
"""

MIGRATIONS = [
    ("event_impact", "expected_dir", "TEXT"),
    ("event_impact", "hit", "TEXT"),
]

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_events_time ON events(time_utc);
CREATE INDEX IF NOT EXISTS idx_prices_ts ON prices(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_impact_event ON event_impact(event_id);
CREATE INDEX IF NOT EXISTS idx_impact_hit ON event_impact(hit);
CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_paper_trades_event ON paper_trades(event_id);
CREATE INDEX IF NOT EXISTS idx_reminders_event ON event_reminders(event_id);

CREATE INDEX IF NOT EXISTS idx_backtest_event ON backtest_trades(event_id);
CREATE INDEX IF NOT EXISTS idx_backtest_symbol ON backtest_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_backtest_ts ON backtest_trades(event_time_utc);

CREATE INDEX IF NOT EXISTS idx_naive_ts ON naive_trades(event_time_utc);
CREATE INDEX IF NOT EXISTS idx_naive_symbol ON naive_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_naive_group ON naive_trades(event_group_key);

CREATE INDEX IF NOT EXISTS idx_live_ts ON live_trades(event_time_utc);
CREATE INDEX IF NOT EXISTS idx_live_symbol ON live_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_live_status ON live_trades(status);
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


async def _table_sql(db: aiosqlite.Connection, table: str) -> str | None:
    cur = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    )
    row = await cur.fetchone()
    return row["sql"] if row else None


async def _migrate_backtest_trades(db: aiosqlite.Connection):
    sql = await _table_sql(db, "backtest_trades")
    if sql is None:
        return
    needs_recreate = (
        "UNIQUE(event_time_utc, symbol, direction)" not in sql
    )
    if not needs_recreate:
        return
    logger.warning("backtest_trades має стару схему — пересоздаю")
    await db.execute("DROP TABLE IF EXISTS backtest_trades")
    await db.commit()
    await db.execute("""
        CREATE TABLE backtest_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            event_title TEXT,
            event_time_utc TEXT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_ts INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            entry_fee REAL NOT NULL,
            exit_ts INTEGER,
            exit_price REAL,
            exit_fee REAL,
            size_usd REAL NOT NULL,
            pnl REAL,
            pnl_pct REAL,
            status TEXT NOT NULL DEFAULT 'CLOSED',
            reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(event_time_utc, symbol, direction)
        )
    """)
    await db.commit()
    logger.info("backtest_trades пересоздано")


async def _migrate_naive_trades(db: aiosqlite.Connection):
    sql = await _table_sql(db, "naive_trades")
    if sql is None:
        return
    needs_recreate = (
        "UNIQUE(event_group_key, symbol)" not in sql
    )
    if not needs_recreate:
        return
    logger.warning("naive_trades має стару схему — пересоздаю")
    await db.execute("DROP TABLE IF EXISTS naive_trades")
    await db.commit()


async def init_db():
    _ensure_db_dir()
    async with get_db() as db:
        await db.executescript(TABLES_SQL)
        await db.commit()

        for table, col, coltype in MIGRATIONS:
            await _add_column_if_missing(db, table, col, coltype)

        await _migrate_backtest_trades(db)
        await _migrate_naive_trades(db)

        await db.executescript(INDEXES_SQL)
        await db.commit()

        cur = await db.execute("SELECT COUNT(*) AS n FROM paper_account")
        if (await cur.fetchone())["n"] == 0:
            await db.execute(
                "INSERT INTO paper_account (id, initial_balance, balance) VALUES (1, 100.0, 100.0)"
            )
            await db.commit()
            logger.info("paper_account створено: $100.00")

    logger.info(f"БД готова: {settings.db_path_abs}")


async def _add_column_if_missing(db: aiosqlite.Connection,
                                  table: str, col: str, coltype: str):
    cur = await db.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    existing = {r["name"] for r in rows}
    if col in existing:
        return
    try:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        await db.commit()
        logger.info(f"Міграція: додано {table}.{col}")
    except Exception as ex:
        logger.warning(f"Не вдалось додати {table}.{col}: {ex}")