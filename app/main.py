"""
FastAPI-додаток: API для подій, цін, impact, WebSocket для веб-панелі.

Scheduler:
  - refresh_calendar (day=today) — кожні 6 годин
  - refresh_calendar (day=today) — о 00:01 UTC щодня (підхоплення нового дня)
  - load_history_sync — кожні 24 години (довантаження свічок)
  - recompute_impact_job — кожні 30 хвилин (перерахунок impact)
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import init_db, get_db
from app.calendar.parser import fetch_events
from app.prices.bybit_ws import run_ws
from app.prices.history import load_history_sync
from app.impact.batch import process_all_past_events
from app.impact.stats import get_stats

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, msg: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------
async def refresh_calendar(day: str = "today"):
    """Парсить ForexFactory (day=today) і додає нові події в БД."""
    logger.info(f"Refreshing economic calendar (day={day})...")
    try:
        events = fetch_events(day=day)

        # Фільтр по важливості та валютах
        events = [e for e in events if e["importance"] in settings.calendar_importance]
        if settings.calendar_currencies:
            events = [e for e in events if e["country"] in settings.calendar_currencies]

        inserted = 0
        async with get_db() as db:
            for e in events:
                cur = await db.execute("""
                    INSERT OR IGNORE INTO events
                    (provider, title, country, importance, time_utc,
                     forecast_value, previous_value, actual_value)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (e["provider"], e["title"], e["country"], e["importance"],
                      e["time_utc"], e["forecast_value"], e["previous_value"],
                      e["actual_value"]))
                if cur.rowcount > 0:
                    inserted += 1
            await db.commit()

        logger.info(f"Calendar refreshed: fetched={len(events)}, inserted={inserted}")
        await manager.broadcast({"type": "calendar_refreshed",
                                 "fetched": len(events), "inserted": inserted})
    except Exception as ex:
        logger.exception(f"Calendar refresh failed: {ex}")


async def recompute_impact_job():
    """Перераховує impact для всіх минулих подій, що ще не мають даних."""
    try:
        stats = await process_all_past_events(limit=1000)
        if stats["saved"] > 0 or stats["processed"] > 0:
            logger.info(f"Impact recompute: {stats}")
            await manager.broadcast({"type": "impact_recomputed", **stats})
    except Exception as ex:
        logger.exception(f"Impact recompute failed: {ex}")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. БД
    await init_db()

    # 2. Одразу парсимо календар
    await refresh_calendar(day="today")

    # 3. Довантажуємо свічки за останні 2 дні (закриває пропуски WebSocket)
    try:
        await asyncio.to_thread(load_history_sync, 2, "1")
    except Exception as ex:
        logger.exception(f"Initial history load failed: {ex}")

    # 4. Scheduler jobs
    # 4a. Календар — кожні 6 годин
    scheduler.add_job(
        refresh_calendar, "interval",
        hours=settings.calendar_refresh_hours,
        id="calendar_interval",
        kwargs={"day": "today"},
    )
    # 4b. Календар — о 00:01 UTC щодня (підхоплення нового дня)
    scheduler.add_job(
        refresh_calendar, CronTrigger(hour=0, minute=1),
        id="calendar_daily",
        kwargs={"day": "today"},
    )
    # 4c. Свічки — кожні 24 години
    scheduler.add_job(
        load_history_sync, "interval",
        hours=24, id="history_daily",
        kwargs={"days": 2, "interval": "1"},
    )
    # 4d. Impact — кожні 30 хвилин
    scheduler.add_job(
        recompute_impact_job, "interval",
        minutes=30, id="impact_recompute_auto",
    )
    scheduler.start()
    logger.info(f"Scheduler started. Jobs: {[j.id for j in scheduler.get_jobs()]}")

    # 5. WebSocket Bybit — у фоні
    asyncio.create_task(run_ws())

    # 6. Одразу рахуємо impact (для тих, що вже минули)
    asyncio.create_task(recompute_impact_job())

    yield

    scheduler.shutdown()


app = FastAPI(title="News Trading Bot", lifespan=lifespan)


# ============================================================================
# HTML
# ============================================================================
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# ============================================================================
# API
# ============================================================================
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/stats")
async def api_stats():
    """Загальні метрики для дашборду."""
    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*) AS n FROM events")
        events_count = (await cur.fetchone())["n"]

        cur = await db.execute("SELECT COUNT(*) AS n FROM prices")
        prices_count = (await cur.fetchone())["n"]

        cur = await db.execute("SELECT COUNT(*) AS n FROM event_impact")
        impact_count = (await cur.fetchone())["n"]

        cur = await db.execute(
            "SELECT MAX(ts) AS mx FROM prices WHERE symbol = ?",
            (settings.bybit_symbol,)
        )
        last_ts = (await cur.fetchone())["mx"]

    last_ts_str = None
    if last_ts:
        from datetime import datetime, timezone
        last_ts_str = datetime.fromtimestamp(
            last_ts / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")

    return {
        "events_count": events_count,
        "prices_count": prices_count,
        "impact_count": impact_count,
        "last_ts": last_ts_str,
    }


@app.get("/events")
async def api_events(limit: int = 50):
    async with get_db() as db:
        cur = await db.execute("""
            SELECT e.id, e.title, e.country, e.importance, e.time_utc,
                   e.forecast_value, e.previous_value, e.actual_value,
                   ei.ret_15m, ei.ret_60m, ei.dir_15m, ei.dir_60m,
                   ei.expected_dir, ei.hit
            FROM events e
            LEFT JOIN event_impact ei ON ei.event_id = e.id
            ORDER BY e.time_utc DESC
            LIMIT ?
        """, (limit,))
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.get("/event/{event_id}")
async def api_event(event_id: int):
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM events WHERE id = ?", (event_id,))
        row = await cur.fetchone()
    return dict(row) if row else {"error": "not found"}


@app.get("/prices/latest")
async def api_prices(limit: int = 100):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT * FROM prices WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
            (settings.bybit_symbol, limit)
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.post("/impact/recompute")
async def api_impact_recompute(limit: int = 500):
    return await process_all_past_events(limit=limit)


@app.get("/impact/stats")
async def api_impact_stats(symbol: str | None = None,
                            importance: str | None = None,
                            min_events: int = 1):
    return await get_stats(symbol=symbol, importance=importance, min_events=min_events)


@app.get("/impact/event/{event_id}")
async def api_impact_event(event_id: int):
    async with get_db() as db:
        cur = await db.execute("""
            SELECT ei.*, e.title, e.country, e.time_utc
            FROM event_impact ei
            JOIN events e ON e.id = ei.event_id
            WHERE ei.event_id = ?
        """, (event_id,))
        row = await cur.fetchone()
    return dict(row) if row else {"error": "not found"}


@app.post("/events/load_history")
async def api_events_load_history(week: str = "last", include_this_week: bool = True):
    """Завантажує події з ForexFactory за вказаний тиждень (необов'язковий endpoint)."""
    total_inserted = 0
    weeks = [week]
    if include_this_week and week != "this":
        weeks.append("this")

    per_week: dict = {}
    for w in weeks:
        try:
            events = fetch_events(week=w)
        except Exception as ex:
            per_week[w] = {"error": str(ex)}
            continue
        events = [e for e in events if e["importance"] in settings.calendar_importance]
        if settings.calendar_currencies:
            events = [e for e in events if e["country"] in settings.calendar_currencies]

        inserted = 0
        async with get_db() as db:
            for e in events:
                cur = await db.execute("""
                    INSERT OR IGNORE INTO events
                    (provider, title, country, importance, time_utc,
                     forecast_value, previous_value, actual_value)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (e["provider"], e["title"], e["country"], e["importance"],
                      e["time_utc"], e["forecast_value"], e["previous_value"],
                      e["actual_value"]))
                if cur.rowcount > 0:
                    inserted += 1
            await db.commit()
        per_week[w] = {"fetched": len(events), "inserted": inserted}
        total_inserted += inserted

    return {"per_week": per_week, "total_inserted": total_inserted}


# ============================================================================
# WebSocket для веб-панелі
# ============================================================================
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)