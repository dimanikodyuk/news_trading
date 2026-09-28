"""
HTML-роути для веб-панелі.
"""

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

from app.db import get_db
from app.impact.stats import get_stats
from app.config import settings
from pathlib import Path

router = APIRouter()

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Головна: сводка."""
    async with get_db() as db:
        # Загальна статистика
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

        # Останні 10 подій
        cur = await db.execute("""
            SELECT id, title, country, importance, time_utc, actual_value
            FROM events ORDER BY time_utc DESC LIMIT 10
        """)
        recent_events = [dict(r) for r in await cur.fetchall()]

    stats = await get_stats()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "events_count": events_count,
        "prices_count": prices_count,
        "impact_count": impact_count,
        "last_ts": last_ts,
        "recent_events": recent_events,
        "stats": stats,
        "symbol": settings.bybit_symbol,
    })


@router.get("/events", response_class=HTMLResponse)
async def events_page(request: Request, importance: str = None):
    """Сторінка з усіма подіями."""
    where = "1=1"
    params = []
    if importance:
        where += " AND importance = ?"
        params.append(importance)

    async with get_db() as db:
        cur = await db.execute(f"""
            SELECT e.*, ei.ret_15m, ei.ret_60m, ei.dir_15m, ei.dir_60m
            FROM events e
            LEFT JOIN event_impact ei ON ei.event_id = e.id
            WHERE {where}
            ORDER BY time_utc DESC
            LIMIT 500
        """, params)
        events = [dict(r) for r in await cur.fetchall()]

    return templates.TemplateResponse("events.html", {
        "request": request,
        "events": events,
        "importance_filter": importance,
    })


@router.get("/event/{event_id}", response_class=HTMLResponse)
async def event_detail(request: Request, event_id: int):
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM events WHERE id = ?", (event_id,))
        event = await cur.fetchone()
        if not event:
            return HTMLResponse("<h1>Event not found</h1>", status_code=404)
        event = dict(event)

        cur = await db.execute("""
            SELECT * FROM event_impact WHERE event_id = ?
        """, (event_id,))
        impact_row = await cur.fetchone()
        impact = dict(impact_row) if impact_row else None

    return templates.TemplateResponse("event_detail.html", {
        "request": request,
        "event": event,
        "impact": impact,
        "symbol": settings.bybit_symbol,
    })


@router.get("/impact", response_class=HTMLResponse)
async def impact_page(request: Request):
    stats = await get_stats()
    return templates.TemplateResponse("impact.html", {
        "request": request,
        "stats": stats,
    })