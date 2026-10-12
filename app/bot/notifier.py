"""
Надсилання сповіщень у Telegram:
- notify_impact — про новий HIT/MISS
- notify_event_soon — нагадування за N хв до події
- notify_weekly_report — щотижневий звіт
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties

from app.config import settings
from app.db import get_db

logger = logging.getLogger(__name__)

_bot: Optional[Bot] = None


def get_bot() -> Optional[Bot]:
    global _bot
    if not settings.tg_bot_token:
        return None
    if _bot is None:
        _bot = Bot(
            token=settings.tg_bot_token,
            default=DefaultBotProperties(parse_mode="Markdown"),
        )
    return _bot


async def _send(text: str, chat_id: Optional[int] = None):
    bot = get_bot()
    if bot is None:
        return
    target = chat_id or settings.tg_chat_id
    if not target:
        logger.debug("tg_chat_id не задано — сповіщення пропущено")
        return
    try:
        await bot.send_message(target, text)
    except Exception as ex:
        logger.warning(f"Telegram send failed: {ex}")


# ---------------------------------------------------------------------------
# 1. Real-time HIT/MISS
# ---------------------------------------------------------------------------
async def notify_impact(event_id: int, chat_id: Optional[int] = None):
    async with get_db() as db:
        cur = await db.execute("""
            SELECT ei.*, e.title, e.country, e.time_utc,
                   e.forecast_value, e.previous_value, e.actual_value
            FROM event_impact ei
            JOIN events e ON e.id = ei.event_id
            WHERE ei.event_id = ? AND ei.symbol = 'SOLUSDT'
        """, (event_id,))
        r = await cur.fetchone()

    if not r:
        return

    hit = r["hit"] or "N/A"
    emoji = {"HIT": "✅", "MISS": "❌", "NEUTRAL": "⚪", "N/A": "➖"}.get(hit, "❔")
    imp_emoji = {"high": "🔴", "medium": "🟡", "low": "⚪"}.get(r["importance"], "⚪")

    def fmt_pct(v):
        return "—" if v is None else f"{v:+.3f}%"

    def fmt_dir(d):
        return {"up": "↑", "down": "↓", "flat": "→"}.get(d, "—")

    text = (
        f"{emoji} *{hit}* — {imp_emoji} {r['title']} ({r['country']})\n"
        f"🕒 {r['time_utc'][:16]} UTC\n"
        f"\n"
        f"📊 Forecast: `{r['forecast_value'] or '—'}`\n"
        f"📊 Actual:   `{r['actual_value'] or '—'}`\n"
        f"\n"
        f"Очікувалось: {fmt_dir(r['expected_dir'])} {r['expected_dir'] or '—'}\n"
        f"Фактично:   {fmt_dir(r['dir_15m'])} {r['dir_15m'] or '—'}  ({fmt_pct(r['ret_15m'])})\n"
    )

    await _send(text, chat_id)


# ---------------------------------------------------------------------------
# 2. Нагадування за N хв
# ---------------------------------------------------------------------------
async def notify_event_soon(event_id: int, minutes_before: int = 5,
                            chat_id: Optional[int] = None):
    async with get_db() as db:
        cur = await db.execute("""
            SELECT id, title, country, importance, time_utc, forecast_value
            FROM events
            WHERE id = ?
        """, (event_id,))
        r = await cur.fetchone()

    if not r:
        return

    imp_emoji = {"high": "🔴", "medium": "🟡"}.get(r["importance"], "⚪")
    dt = datetime.fromisoformat(r["time_utc"].replace("Z", "+00:00"))
    local_time = dt.astimezone(timezone(timedelta(hours=3))).strftime("%H:%M")

    text = (
        f"⏰ *Через {minutes_before} хв — {imp_emoji} {r['title']}* ({r['country']})\n"
        f"🕒 {local_time} Київ · {r['time_utc'][11:16]} UTC\n"
        f"📊 Forecast: `{r['forecast_value'] or '—'}`\n"
        f"\n"
        f"Готуйся!"
    )
    await _send(text, chat_id)


# ---------------------------------------------------------------------------
# 3. Щотижневий звіт
# ---------------------------------------------------------------------------
async def notify_weekly_report(chat_id: Optional[int] = None):
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).isoformat()

    async with get_db() as db:
        cur = await db.execute("""
            SELECT
                COUNT(*) AS n,
                SUM(CASE WHEN hit='HIT' THEN 1 ELSE 0 END) AS hits,
                SUM(CASE WHEN hit='MISS' THEN 1 ELSE 0 END) AS misses,
                SUM(CASE WHEN hit='NEUTRAL' THEN 1 ELSE 0 END) AS neutrals,
                SUM(CASE WHEN hit='NO_DATA' THEN 1 ELSE 0 END) AS no_data
            FROM event_impact
            WHERE symbol = 'SOLUSDT'
              AND event_id IN (SELECT id FROM events WHERE time_utc >= ?)
        """, (week_ago,))
        stats = await cur.fetchone()

        cur = await db.execute("""
            SELECT
                e.title,
                COUNT(*) AS n,
                SUM(CASE WHEN ei.hit='HIT' THEN 1 ELSE 0 END) AS hits,
                SUM(CASE WHEN ei.hit='MISS' THEN 1 ELSE 0 END) AS misses
            FROM event_impact ei
            JOIN events e ON e.id = ei.event_id
            WHERE ei.symbol = 'SOLUSDT'
              AND ei.hit IN ('HIT', 'MISS')
              AND e.time_utc >= ?
            GROUP BY e.title
            HAVING COUNT(*) > 0
            ORDER BY (SUM(CASE WHEN ei.hit='HIT' THEN 1 ELSE 0 END) * 1.0 / COUNT(*)) DESC
            LIMIT 3
        """, (week_ago,))
        top_titles = await cur.fetchall()

        cur = await db.execute("SELECT balance, initial_balance FROM paper_account WHERE id=1")
        acc = await cur.fetchone()

        cur = await db.execute("""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   COALESCE(SUM(pnl), 0) AS total_pnl
            FROM paper_trades
            WHERE status = 'CLOSED'
              AND closed_at >= ?
        """, (week_ago.replace("T", " ")[:19],))
        trades = await cur.fetchone()

    hits = stats["hits"] or 0
    misses = stats["misses"] or 0
    denom = hits + misses
    rate = (hits / denom * 100) if denom > 0 else 0

    text = f"📊 *Тижневий звіт* ({now.strftime('%d.%m.%Y')})\n\n"
    text += f"📌 Подій за тиждень: {stats['n'] or 0}\n"
    text += f"✅ HIT: {hits} · ❌ MISS: {misses} · ⚪ NEUTRAL: {stats['neutrals'] or 0}\n"
    text += f"🎯 Hit Rate: *{rate:.1f}%* (n={denom})\n\n"

    if top_titles:
        text += "🏆 *Кращі події:*\n"
        for r in top_titles:
            t_hits = r["hits"] or 0
            t_misses = r["misses"] or 0
            t_rate = (t_hits / (t_hits + t_misses) * 100) if (t_hits + t_misses) > 0 else 0
            text += f"  • {r['title'][:35]} — {t_rate:.0f}% ({t_hits}/{t_hits + t_misses})\n"
        text += "\n"

    text += f"💰 *Paper:*\n"
    text += f"  Баланс: ${acc['balance']:.2f} (старт ${acc['initial_balance']:.2f})\n"
    text += f"  Угод: {trades['n'] or 0} (wins: {trades['wins'] or 0})\n"
    text += f"  P&L: {trades['total_pnl']:+.4f}$\n"

    await _send(text, chat_id)