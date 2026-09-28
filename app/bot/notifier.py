"""
Надсилання сповіщень у Telegram про результати impact.
"""

import logging
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


async def notify_impact(event_id: int, chat_id: Optional[int] = None):
    """Надсилає сповіщення про результат impact однієї події."""
    bot = get_bot()
    if bot is None:
        return

    target = chat_id or settings.tg_chat_id
    if not target:
        logger.debug("tg_chat_id не задано — сповіщення пропущено")
        return

    async with get_db() as db:
        cur = await db.execute("""
            SELECT ei.*, e.title, e.country, e.time_utc,
                   e.forecast_value, e.previous_value, e.actual_value
            FROM event_impact ei
            JOIN events e ON e.id = ei.event_id
            WHERE ei.event_id = ?
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

    price_b = r["price_baseline"]
    price_t15 = r["price_t15"]
    price_line = "—"
    if price_b is not None and price_t15 is not None:
        price_line = f"${price_b:.4f} → ${price_t15:.4f}"

    text = (
        f"{emoji} *{hit}* — {imp_emoji} {r['title']} ({r['country']})\n"
        f"🕒 {r['time_utc'][:16]} UTC\n"
        f"\n"
        f"📊 Forecast: `{r['forecast_value'] or '—'}`\n"
        f"📊 Actual:   `{r['actual_value'] or '—'}`\n"
        f"\n"
        f"Очікувалось: {fmt_dir(r['expected_dir'])} {r['expected_dir'] or '—'}\n"
        f"Фактично:   {fmt_dir(r['dir_15m'])} {r['dir_15m'] or '—'}  ({fmt_pct(r['ret_15m'])})\n"
        f"\n"
        f"Ціна: {price_line}"
    )

    try:
        await bot.send_message(target, text)
    except Exception as ex:
        logger.warning(f"Не вдалось надіслати сповіщення: {ex}")