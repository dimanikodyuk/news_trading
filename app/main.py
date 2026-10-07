"""
FastAPI-додаток: API для подій, цін, impact, LIVE, Paper Trading, Tools.

Scheduler:
  - refresh_calendar (day=today) — кожні 15 хв
  - refresh_calendar (day=today) — кожні 6 год
  - refresh_calendar (day=today) — о 00:01 UTC щодня
  - load_history_sync — кожні 24 години
  - recompute_impact_job — кожні 30 хвилин
  - paper_engine_job — кожні 5 хвилин
"""

import asyncio
import csv
import io
import logging
import subprocess
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Body
from fastapi.responses import FileResponse, StreamingResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import init_db, get_db
from app.calendar.parser import fetch_events
from app.prices.bybit_ws import start_ws_thread
from app.prices.history import load_history_sync
from app.impact.batch import process_all_past_events
from app.impact.stats import get_stats, get_heatmap, get_cross_asset_detail
from app.bot.notifier import notify_impact
from app.paper.engine import process_paper_engine
from app.paper.stats import get_account, get_trades, get_equity_curve, reset_account

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


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


def _iso_to_ms(iso_str: str | None) -> int | None:
    if not iso_str:
        return None
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------
async def refresh_calendar(day: str = "today"):
    logger.info(f"Refreshing economic calendar (day={day})...")
    try:
        events = fetch_events(day=day)
        events = [e for e in events if e["importance"] in settings.calendar_importance]
        if settings.calendar_currencies:
            events = [e for e in events if e["country"] in settings.calendar_currencies]

        inserted = 0
        updated = 0

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
                else:
                    upd = await db.execute("""
                        UPDATE events
                        SET forecast_value = COALESCE(?, forecast_value),
                            previous_value = COALESCE(?, previous_value),
                            actual_value = COALESCE(?, actual_value)
                        WHERE provider = ? AND title = ? AND time_utc = ?
                          AND (
                            (actual_value IS NULL AND ? IS NOT NULL) OR
                            (forecast_value IS NULL AND ? IS NOT NULL)
                          )
                    """, (e["forecast_value"], e["previous_value"], e["actual_value"],
                          e["provider"], e["title"], e["time_utc"],
                          e["actual_value"], e["forecast_value"]))
                    if upd.rowcount > 0:
                        updated += 1

            await db.commit()

        logger.info(
            f"Calendar refreshed: fetched={len(events)}, "
            f"inserted={inserted}, updated={updated}"
        )
        await manager.broadcast({
            "type": "calendar_refreshed",
            "fetched": len(events),
            "inserted": inserted,
            "updated": updated,
        })
        return {"fetched": len(events), "inserted": inserted, "updated": updated}
    except Exception as ex:
        logger.exception(f"Calendar refresh failed: {ex}")
        return {"error": str(ex)}


async def recompute_impact_job():
    try:
        async with get_db() as db:
            cur = await db.execute("""
                SELECT e.id FROM events e
                LEFT JOIN event_impact ei ON ei.event_id = e.id
                WHERE e.time_utc < datetime('now')
                  AND e.importance IN ('high', 'medium')
                  AND (ei.id IS NULL OR ei.hit IS NULL)
                ORDER BY e.time_utc DESC
                LIMIT 50
            """)
            pending = [r["id"] for r in await cur.fetchall()]

        stats = await process_all_past_events(limit=1000)
        if stats["saved"] > 0 or stats["processed"] > 0:
            logger.info(f"Impact recompute: {stats}")
            await manager.broadcast({"type": "impact_recomputed", **stats})
            for eid in pending:
                await notify_impact(eid)
        return stats
    except Exception as ex:
        logger.exception(f"Impact recompute failed: {ex}")
        return {"error": str(ex)}


async def paper_engine_job():
    try:
        stats = await process_paper_engine()
        if stats["opened"] or stats["closed"]:
            await manager.broadcast({"type": "paper_updated", **stats})
        return stats
    except Exception as ex:
        logger.exception(f"Paper engine failed: {ex}")
        return {"error": str(ex)}


async def live_broadcast_loop():
    while True:
        try:
            if manager.active:
                data = await _build_live_current()
                if data["active"]:
                    await manager.broadcast({"type": "live_update", "data": data})
        except Exception as ex:
            logger.debug(f"live_broadcast_loop error: {ex}")
        await asyncio.sleep(2)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await refresh_calendar(day="today")

    try:
        await asyncio.to_thread(load_history_sync, 2, "1")
    except Exception as ex:
        logger.exception(f"Initial history load failed: {ex}")

    scheduler.add_job(refresh_calendar, "interval",
                      minutes=15, id="calendar_frequent",
                      kwargs={"day": "today"})
    scheduler.add_job(refresh_calendar, "interval",
                      hours=settings.calendar_refresh_hours, id="calendar_interval",
                      kwargs={"day": "today"})
    scheduler.add_job(refresh_calendar, CronTrigger(hour=0, minute=1),
                      id="calendar_daily", kwargs={"day": "today"})
    scheduler.add_job(load_history_sync, "interval",
                      hours=24, id="history_daily",
                      kwargs={"days": 2, "interval": "1"})
    scheduler.add_job(recompute_impact_job, "interval",
                      minutes=30, id="impact_recompute_auto")
    scheduler.add_job(paper_engine_job, "interval",
                      minutes=5, id="paper_engine")
    scheduler.start()
    logger.info(f"Scheduler started. Jobs: {[j.id for j in scheduler.get_jobs()]}")

    start_ws_thread()

    asyncio.create_task(recompute_impact_job())
    asyncio.create_task(live_broadcast_loop())

    yield
    scheduler.shutdown()


app = FastAPI(title="News Trading Bot", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)


# ============================================================================
# HTML
# ============================================================================
@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# ============================================================================
# API: stats / events / prices
# ============================================================================
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/stats")
async def api_stats():
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
async def api_events(
    limit: int = 50,
    days_back: int | None = None,
    days_forward: int | None = None,
    search: str | None = None,
    importance: str | None = None,
):
    where = ["1=1"]
    params: list = []

    if days_back is not None:
        from_iso = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        where.append("e.time_utc >= ?")
        params.append(from_iso)

    if days_forward is not None:
        to_iso = (datetime.now(timezone.utc) + timedelta(days=days_forward)).isoformat()
        where.append("e.time_utc <= ?")
        params.append(to_iso)

    if search:
        where.append("LOWER(e.title) LIKE ?")
        params.append(f"%{search.lower()}%")

    if importance:
        where.append("e.importance = ?")
        params.append(importance)

    where_sql = " AND ".join(where)

    async with get_db() as db:
        cur = await db.execute(f"""
            SELECT e.id, e.title, e.country, e.importance, e.time_utc,
                   e.forecast_value, e.previous_value, e.actual_value,
                   ei.ret_15m, ei.ret_60m, ei.dir_15m, ei.dir_60m,
                   ei.expected_dir, ei.hit
            FROM events e
            LEFT JOIN event_impact ei ON ei.event_id = e.id AND ei.symbol = 'SOLUSDT'
            WHERE {where_sql}
            ORDER BY e.time_utc DESC
            LIMIT ?
        """, params + [limit])
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.get("/events/calendar")
async def api_events_calendar(days: int = 7):
    from_iso = datetime.now(timezone.utc).isoformat()
    to_iso = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    async with get_db() as db:
        cur = await db.execute("""
            SELECT id, title, country, importance, time_utc,
                   forecast_value, actual_value
            FROM events
            WHERE time_utc BETWEEN ? AND ?
            ORDER BY time_utc ASC
        """, (from_iso, to_iso))
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.get("/events/upcoming_high")
async def api_events_upcoming_high(days: int = 7, limit: int = 20):
    """Найближчі high-importance події."""
    from_iso = datetime.now(timezone.utc).isoformat()
    to_iso = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    async with get_db() as db:
        cur = await db.execute("""
            SELECT id, title, country, importance, time_utc,
                   forecast_value, previous_value
            FROM events
            WHERE time_utc BETWEEN ? AND ?
              AND importance = 'high'
            ORDER BY time_utc ASC
            LIMIT ?
        """, (from_iso, to_iso, limit))
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


@app.get("/prices/range")
async def api_prices_range(hours: int = 24, symbol: str | None = None):
    sym = symbol or settings.bybit_symbol
    now_ms = int(_time.time() * 1000)
    from_ms = now_ms - hours * 60 * 60 * 1000

    async with get_db() as db:
        cur = await db.execute("""
            SELECT ts, open, high, low, close, volume
            FROM prices
            WHERE symbol = ? AND ts >= ?
            ORDER BY ts ASC
        """, (sym, from_ms))
        candles = [dict(r) for r in await cur.fetchall()]

        from_iso = datetime.fromtimestamp(
            from_ms / 1000, tz=timezone.utc
        ).isoformat()

        cur = await db.execute("""
            SELECT e.id, e.title, e.country, e.importance, e.time_utc,
                   ei.hit, ei.ret_15m, ei.dir_15m, ei.expected_dir
            FROM events e
            LEFT JOIN event_impact ei ON ei.event_id = e.id AND ei.symbol = 'SOLUSDT'
            WHERE e.time_utc >= ?
            ORDER BY e.time_utc ASC
        """, (from_iso,))
        events = [dict(r) for r in await cur.fetchall()]

    return {"symbol": sym, "hours": hours, "candles": candles, "events": events}


# ============================================================================
# API: CSV export
# ============================================================================
@app.get("/events/export.csv")
async def api_events_export_csv(
    days_back: int | None = 30,
    importance: str | None = None,
    search: str | None = None,
):
    where = ["1=1"]
    params: list = []

    if days_back is not None:
        from_iso = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        where.append("e.time_utc >= ?")
        params.append(from_iso)
    if importance:
        where.append("e.importance = ?")
        params.append(importance)
    if search:
        where.append("LOWER(e.title) LIKE ?")
        params.append(f"%{search.lower()}%")

    where_sql = " AND ".join(where)

    async with get_db() as db:
        cur = await db.execute(f"""
            SELECT
                e.id, e.time_utc, e.title, e.country, e.importance,
                e.forecast_value, e.previous_value, e.actual_value,
                ei.hit, ei.expected_dir,
                ei.ret_1m, ei.ret_5m, ei.ret_15m, ei.ret_30m, ei.ret_60m,
                ei.dir_1m, ei.dir_5m, ei.dir_15m, ei.dir_30m, ei.dir_60m,
                ei.price_baseline, ei.threshold_pct
            FROM events e
            LEFT JOIN event_impact ei ON ei.event_id = e.id AND ei.symbol = 'SOLUSDT'
            WHERE {where_sql}
            ORDER BY e.time_utc DESC
        """, params)
        rows = await cur.fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "time_utc", "title", "country", "importance",
        "forecast", "previous", "actual",
        "hit", "expected_dir",
        "ret_1m", "ret_5m", "ret_15m", "ret_30m", "ret_60m",
        "dir_1m", "dir_5m", "dir_15m", "dir_30m", "dir_60m",
        "price_baseline", "threshold_pct",
    ])
    for r in rows:
        writer.writerow([r[k] for k in r.keys()])

    buf.seek(0)
    filename = f"events_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/paper/trades/export.csv")
async def api_paper_trades_export_csv():
    async with get_db() as db:
        cur = await db.execute("""
            SELECT
                pt.id, pt.event_id, pt.symbol, pt.direction,
                pt.entry_ts, pt.entry_price, pt.entry_fee,
                pt.exit_ts, pt.exit_price, pt.exit_fee,
                pt.size_usd, pt.pnl, pt.pnl_pct, pt.status, pt.reason,
                pt.opened_at, pt.closed_at,
                e.title AS event_title, e.time_utc AS event_time,
                e.importance, e.forecast_value, e.actual_value
            FROM paper_trades pt
            JOIN events e ON e.id = pt.event_id
            ORDER BY pt.id DESC
        """)
        rows = await cur.fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "event_id", "event_time", "event_title", "importance",
        "symbol", "direction",
        "entry_ts", "entry_price", "entry_fee",
        "exit_ts", "exit_price", "exit_fee",
        "size_usd", "pnl", "pnl_pct", "status", "reason",
        "opened_at", "closed_at",
    ])
    for r in rows:
        writer.writerow([r[k] for k in r.keys()])

    buf.seek(0)
    filename = f"paper_trades_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ============================================================================
# API: impact
# ============================================================================
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
            WHERE ei.event_id = ? AND ei.symbol = 'SOLUSDT'
        """, (event_id,))
        row = await cur.fetchone()
    return dict(row) if row else {"error": "not found"}


@app.get("/impact/heatmap")
async def api_impact_heatmap(importance: str | None = None):
    return await get_heatmap(importance=importance)


@app.get("/impact/cross_asset/{event_id}")
async def api_impact_cross_asset(event_id: int):
    return await get_cross_asset_detail(event_id)


# ============================================================================
# API: LIVE
# ============================================================================
async def _build_live_current() -> dict:
    now_ms = int(_time.time() * 1000)
    window_ms = 2 * 60 * 1000

    lo_iso = datetime.fromtimestamp((now_ms - window_ms) / 1000, tz=timezone.utc).isoformat()
    hi_iso = datetime.fromtimestamp((now_ms + window_ms) / 1000, tz=timezone.utc).isoformat()

    async with get_db() as db:
        cur = await db.execute("""
            SELECT id, title, country, importance, time_utc,
                   forecast_value, previous_value, actual_value
            FROM events
            WHERE time_utc BETWEEN ? AND ?
              AND importance IN ('high', 'medium')
            ORDER BY time_utc ASC
        """, (lo_iso, hi_iso))
        events = [dict(r) for r in await cur.fetchall()]

        result = []
        for ev in events:
            ev_ts = _iso_to_ms(ev["time_utc"])
            if ev_ts is None:
                continue
            baseline_ts = ev_ts - 60_000
            symbols_data = {}
            for sym in ["SOLUSDT", "BTCUSDT", "ETHUSDT"]:
                cur = await db.execute("""
                    SELECT close FROM prices
                    WHERE symbol = ? AND interval = '1' AND confirm = 1
                      AND ts BETWEEN ? AND ?
                    ORDER BY ABS(ts - ?) ASC LIMIT 1
                """, (sym, baseline_ts - 60_000, baseline_ts + 60_000, baseline_ts))
                row = await cur.fetchone()
                baseline = float(row["close"]) if row else None

                cur = await db.execute("""
                    SELECT ts, close FROM prices
                    WHERE symbol = ? AND interval = '1'
                    ORDER BY ts DESC LIMIT 1
                """, (sym,))
                row = await cur.fetchone()
                if baseline and row:
                    current = float(row["close"])
                    ret_pct = (current - baseline) / baseline * 100.0
                    symbols_data[sym] = {
                        "baseline": baseline,
                        "current": current,
                        "current_ts": row["ts"],
                        "ret_pct": ret_pct,
                    }
                else:
                    symbols_data[sym] = None

            result.append({
                "event": ev,
                "seconds_since": max(0, int((now_ms - ev_ts) / 1000)),
                "seconds_to": max(0, int((ev_ts - now_ms) / 1000)),
                "symbols": symbols_data,
            })
    return {"now_ms": now_ms, "active": result}


@app.get("/live/current")
async def api_live_current():
    return await _build_live_current()


# ============================================================================
# API: Paper Trading
# ============================================================================
@app.get("/paper/account")
async def api_paper_account():
    return await get_account()


@app.get("/paper/trades")
async def api_paper_trades(limit: int = 100, status: str | None = None):
    return await get_trades(limit=limit, status=status)


@app.get("/paper/trades/range")
async def api_paper_trades_range(hours: int = 24):
    now_ms = int(_time.time() * 1000)
    from_ms = now_ms - hours * 60 * 60 * 1000

    async with get_db() as db:
        cur = await db.execute("""
            SELECT
                pt.id, pt.event_id, pt.symbol, pt.direction,
                pt.entry_ts, pt.entry_price, pt.entry_fee,
                pt.exit_ts, pt.exit_price, pt.exit_fee,
                pt.size_usd, pt.pnl, pt.pnl_pct, pt.status,
                pt.opened_at, pt.closed_at,
                e.title AS event_title, e.time_utc AS event_time
            FROM paper_trades pt
            JOIN events e ON e.id = pt.event_id
            WHERE pt.entry_ts >= ?
               OR (pt.exit_ts IS NOT NULL AND pt.exit_ts >= ?)
            ORDER BY pt.entry_ts ASC
        """, (from_ms, from_ms))
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.get("/paper/equity_curve")
async def api_paper_equity_curve():
    return await get_equity_curve()


@app.post("/paper/reset")
async def api_paper_reset():
    return await reset_account()


@app.post("/paper/run")
async def api_paper_run():
    return await process_paper_engine()


# ============================================================================
# API: Tools (для вкладки «Інструменти»)
# ============================================================================
@app.post("/tools/recompute_impact")
async def tools_recompute_impact(limit: int = Query(1000, ge=1, le=10000)):
    """Перерахувати impact для всіх минулих подій."""
    stats = await process_all_past_events(limit=limit)
    await manager.broadcast({"type": "impact_recomputed", **stats})
    return stats


@app.post("/tools/run_paper_engine")
async def tools_run_paper_engine():
    """Запустити paper engine вручну."""
    stats = await process_paper_engine()
    await manager.broadcast({"type": "paper_updated", **stats})
    return stats


@app.post("/tools/load_history")
async def tools_load_history(days: int = Query(2, ge=1, le=90), interval: str = "1"):
    """Довантажити свічки через REST Bybit."""
    try:
        total = await asyncio.to_thread(load_history_sync, days, interval)
        return {"loaded": total, "days": days, "interval": interval}
    except Exception as ex:
        logger.exception(f"load_history failed: {ex}")
        return {"error": str(ex)}


@app.post("/tools/refresh_calendar")
async def tools_refresh_calendar(day: str = "today"):
    """Парсити ForexFactory (day=today|tomorrow|yesterday)."""
    return await refresh_calendar(day=day)


@app.post("/tools/load_calendar_week")
async def tools_load_calendar_week(week: str = "this"):
    """Завантажити тиждень подій з ForexFactory (week=this|last|next)."""
    total_inserted = 0
    per_week: dict = {}
    weeks = [week]
    if week != "this":
        weeks.append("this")

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


@app.post("/tools/clear_impact")
async def tools_clear_impact():
    """ОЧИСТИТИ всю таблицю event_impact (небезпечно!)."""
    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*) AS n FROM event_impact")
        n_before = (await cur.fetchone())["n"]
        await db.execute("DELETE FROM event_impact")
        await db.commit()
    logger.warning(f"event_impact cleared: {n_before} rows deleted")
    return {"deleted": n_before}


@app.get("/tools/db_stats")
async def tools_db_stats():
    """Загальна статистика БД."""
    async with get_db() as db:
        stats = {}

        # Кількість подій
        cur = await db.execute("SELECT COUNT(*) AS n FROM events")
        stats["events_total"] = (await cur.fetchone())["n"]

        # По важливості
        cur = await db.execute("""
            SELECT importance, COUNT(*) AS n FROM events GROUP BY importance
        """)
        stats["events_by_importance"] = {r["importance"]: r["n"] for r in await cur.fetchall()}

        # Свічки
        cur = await db.execute("""
            SELECT symbol, COUNT(*) AS n, MIN(ts) AS mn, MAX(ts) AS mx
            FROM prices GROUP BY symbol
        """)
        stats["prices"] = []
        for r in await cur.fetchall():
            stats["prices"].append({
                "symbol": r["symbol"],
                "count": r["n"],
                "min_ts": r["mn"],
                "max_ts": r["mx"],
            })

        # Impact по hit
        cur = await db.execute("""
            SELECT hit, COUNT(*) AS n FROM event_impact GROUP BY hit
        """)
        stats["impact_by_hit"] = {r["hit"]: r["n"] for r in await cur.fetchall()}

        # Impact по symbol
        cur = await db.execute("""
            SELECT symbol, COUNT(*) AS n FROM event_impact GROUP BY symbol
        """)
        stats["impact_by_symbol"] = {r["symbol"]: r["n"] for r in await cur.fetchall()}

        # Paper trades
        cur = await db.execute("""
            SELECT status, COUNT(*) AS n FROM paper_trades GROUP BY status
        """)
        stats["paper_trades"] = {r["status"]: r["n"] for r in await cur.fetchall()}

        # Account
        cur = await db.execute("SELECT balance, initial_balance FROM paper_account WHERE id=1")
        acc = await cur.fetchone()
        if acc:
            stats["paper_account"] = {
                "balance": acc["balance"],
                "initial": acc["initial_balance"],
            }

    return stats


@app.get("/tools/logs")
async def tools_logs(lines: int = 100):
    """Останні N рядків логів сервісу."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", "newsbot.service", "-n", str(lines), "--no-pager", "-o", "cat"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return {"lines": result.stdout.split("\n")}
    except FileNotFoundError:
        return {"error": "journalctl недоступний (не systemd?)"}
    except Exception as ex:
        return {"error": str(ex)}


@app.get("/tools/diagnostics")
async def tools_diagnostics():
    """Діагностика: розбивка impact по hit/importance."""
    async with get_db() as db:
        # Загальна
        cur = await db.execute("""
            SELECT
                COUNT(*) AS n,
                SUM(CASE WHEN hit='HIT' THEN 1 ELSE 0 END) AS hits,
                SUM(CASE WHEN hit='MISS' THEN 1 ELSE 0 END) AS misses,
                SUM(CASE WHEN hit='NEUTRAL' THEN 1 ELSE 0 END) AS neutrals,
                SUM(CASE WHEN hit='NO_DATA' THEN 1 ELSE 0 END) AS no_data,
                SUM(CASE WHEN hit='N/A' THEN 1 ELSE 0 END) AS na
            FROM event_impact
            WHERE symbol = 'SOLUSDT'
        """)
        overall = dict(await cur.fetchone())

        # По importance
        cur = await db.execute("""
            SELECT
                importance,
                COUNT(*) AS n,
                SUM(CASE WHEN hit='HIT' THEN 1 ELSE 0 END) AS hits,
                SUM(CASE WHEN hit='MISS' THEN 1 ELSE 0 END) AS misses,
                SUM(CASE WHEN hit='NEUTRAL' THEN 1 ELSE 0 END) AS neutrals,
                SUM(CASE WHEN hit='NO_DATA' THEN 1 ELSE 0 END) AS no_data
            FROM event_impact
            WHERE symbol = 'SOLUSDT'
            GROUP BY importance
        """)
        by_importance = [dict(r) for r in await cur.fetchall()]

    return {"overall": overall, "by_importance": by_importance}


# ============================================================================
# API: history loading (legacy)
# ============================================================================
@app.post("/events/load_history")
async def api_events_load_history(week: str = "last", include_this_week: bool = True):
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
# WebSocket
# ============================================================================
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)