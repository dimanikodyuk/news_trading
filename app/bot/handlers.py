from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message
from app.db import get_db

router = Router()

@router.message(Command("start"))
async def cmd_start(msg: Message):
    await msg.answer(
        "👋 News Bot v0.1\n\n"
        "Команди:\n"
        "/events — найближчі події\n"
        "/event <id> — деталі події"
    )

@router.message(Command("events"))
async def cmd_events(msg: Message):
    async with get_db() as db:
        cur = await db.execute("""
            SELECT id, title, country, importance, time_utc
            FROM events
            WHERE time_utc >= datetime('now')
            ORDER BY time_utc ASC LIMIT 10
        """)
        rows = await cur.fetchall()

    if not rows:
        await msg.answer("Подій не знайдено.")
        return

    lines = ["📅 *Найближчі події:*\n"]
    for r in rows:
        emoji = {"high": "🔴", "medium": "🟡", "low": "⚪"}.get(r["importance"], "⚪")
        lines.append(f"{emoji} `{r['id']}` *{r['title']}* ({r['country']})\n   🕒 {r['time_utc']}")

    await msg.answer("\n".join(lines), parse_mode="Markdown")

@router.message(Command("impact"))
async def cmd_impact(msg: Message):
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.answer("Використання: /impact <event_id>")
        return

    event_id = int(parts[1])
    async with get_db() as db:
        cur = await db.execute("""
            SELECT ei.*, e.title, e.country, e.time_utc
            FROM event_impact ei
            JOIN events e ON e.id = ei.event_id
            WHERE ei.event_id = ?
        """, (event_id,))
        r = await cur.fetchone()

    if not r:
        await msg.answer("Impact ще не розраховано для цієї події.")
        return

    def fmt(v, suffix=""):
        return f"{v:+.2f}{suffix}" if v is not None else "—"

    await msg.answer(
        f"📊 *{r['title']}* ({r['country']})\n"
        f"🕒 {r['time_utc']}\n"
        f"Важливість: {r['importance']}\n"
        f"Поріг: {r['threshold_pct']:.2f}%\n\n"
        f"Базова ціна (T-1): ${r['price_baseline']:.4f}\n\n"
        f"*Зміни:*\n"
        f"  +1 хв:  {fmt(r['ret_1m'], '%')}  ({r['dir_1m']})\n"
        f"  +5 хв:  {fmt(r['ret_5m'], '%')}  ({r['dir_5m']})\n"
        f"  +15 хв: {fmt(r['ret_15m'], '%')}  ({r['dir_15m']})\n"
        f"  +30 хв: {fmt(r['ret_30m'], '%')}  ({r['dir_30m']})\n"
        f"  +60 хв: {fmt(r['ret_60m'], '%')}  ({r['dir_60m']})",
        parse_mode="Markdown"
    )

@router.message(Command("event"))
async def cmd_event(msg: Message):
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.answer("Використання: /event <id>")
        return

    async with get_db() as db:
        cur = await db.execute("SELECT * FROM events WHERE id = ?", (int(parts[1]),))
        row = await cur.fetchone()

    if not row:
        await msg.answer("Подію не знайдено.")
        return

    await msg.answer(
        f"📌 *{row['title']}*\n"
        f"Країна: {row['country']}\n"
        f"Важливість: {row['importance']}\n"
        f"Час (UTC): {row['time_utc']}\n"
        f"Прогноз: {row['forecast_value'] or '—'}\n"
        f"Попереднє: {row['previous_value'] or '—'}\n"
        f"Факт: {row['actual_value'] or 'ще не опубліковано'}",
        parse_mode="Markdown"
    )