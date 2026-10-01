"""
Парсер економічного календаря ForexFactory через JSON у HTML.

ForexFactory вбудовує дані подій як JSON у змінну
`window.calendarComponentStates[1]`. Це дає нам:
- actual, forecast, previous, revision (усе, чого не було в HTML)
- impactClass (high/medium/low) — точніше за HTML-парсинг
- dateline (unix timestamp) — точний час
- actualBetterWorse — ForexFactory каже, чи actual кращий за forecast

Parser v5 (JSON-based).
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

FF_URL = "https://www.forexfactory.com/calendar.php"
FF_COOKIES = {"fftimezoneoffset": "0", "ffdstonoff": "1"}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_events(day: str = "today", week: Optional[str] = None) -> list[dict]:
    """
    Повертає список подій з ForexFactory.

    Аргументи:
        day:  'today' | 'tomorrow' | 'yesterday'
        week: 'this' | 'last' | 'next' (якщо задано — day ігнорується)
    """
    params: dict = {}
    if week:
        params["week"] = week
    else:
        params["day"] = day

    r = requests.get(
        FF_URL, params=params, cookies=FF_COOKIES,
        headers=HEADERS, timeout=30,
    )
    r.raise_for_status()

    # --- Витягуємо JSON з window.calendarComponentStates[1] ---
    raw_json = _extract_calendar_json(r.text)
    if raw_json is None:
        logger.error("Не вдалось знайти calendarComponentStates у HTML")
        return []

    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as ex:
        logger.error(f"Помилка парсингу JSON: {ex}")
        return []

    days = data.get("days", [])
    events: list[dict] = []

    for day_obj in days:
        day_events = day_obj.get("events", [])
        for ev in day_events:
            parsed = _parse_event(ev)
            if parsed is not None:
                events.append(parsed)

    logger.info(f"Fetched {len(events)} events (day={day}, week={week or '-'})")
    return events


# ---------------------------------------------------------------------------
# Витягування JSON
# ---------------------------------------------------------------------------

def _extract_calendar_json(html: str) -> Optional[str]:
    """
    Знаходить `window.calendarComponentStates[1] = {...}` і повертає
    JSON-рядок (збалансований по фігурних дужках).
    """
    marker = "window.calendarComponentStates[1]"
    idx = html.find(marker)
    if idx < 0:
        return None

    # Знаходимо першу "{" після "="
    eq_idx = html.find("=", idx)
    if eq_idx < 0:
        return None
    brace_start = html.find("{", eq_idx)
    if brace_start < 0:
        return None

    # Балансуємо дужки, враховуючи лапки і екранування
    depth = 0
    in_string = False
    escape = False
    end = -1

    for i in range(brace_start, len(html)):
        ch = html[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end < 0:
        return None

    raw = html[brace_start:end]

    # ForexFactory екранує слеші: `\/` → `/`
    raw = raw.replace("\\/", "/")

    return raw


# ---------------------------------------------------------------------------
# Парсинг однієї події
# ---------------------------------------------------------------------------

def _parse_event(ev: dict) -> Optional[dict]:
    """Перетворює JSON події у наш формат."""
    name = ev.get("name") or ev.get("soloTitle") or ""
    if not name:
        return None

    # Валюта: 'USD', 'EUR', ...
    currency = ev.get("currency") or ""
    if not currency:
        # Інколи country='US', currency='USD'
        country = ev.get("country") or ""
        currency = country[:3].upper() if len(country) >= 3 else country

    # Час: dateline (unix seconds)
    dateline = ev.get("dateline")
    if dateline:
        try:
            dt = datetime.fromtimestamp(int(dateline), tz=timezone.utc)
            time_utc = dt.isoformat()
        except (ValueError, TypeError, OSError):
            time_utc = None
    else:
        time_utc = None

    if time_utc is None:
        return None

    # Важливість: з impactClass
    impact = _parse_impact(ev.get("impactClass") or ev.get("impactName") or "")

    # Значення
    forecast = _clean_value(ev.get("forecast"))
    previous = _clean_value(ev.get("previous"))
    actual = _clean_value(ev.get("actual"))
    revision = _clean_value(ev.get("revision"))

    # Якщо є revision — додаємо до previous як окреме поле? Поки ігноруємо

    return {
        "provider": "forex_factory",
        "title": name,
        "country": currency,
        "importance": impact,
        "time_utc": time_utc,
        "forecast_value": forecast,
        "previous_value": previous,
        "actual_value": actual,
        # Додаткові поля (не входять у схему БД, але корисні)
        "_revision": revision,
        "_actual_better_worse": ev.get("actualBetterWorse"),
        "_event_id": ev.get("id"),
    }


def _parse_impact(impact_str: str) -> str:
    """'icon--ff-impact-ora' → 'medium'."""
    s = impact_str.lower()
    if "impact-red" in s or "high" in s:
        return "high"
    if "impact-ora" in s or "medium" in s:
        return "medium"
    if "impact-yel" in s or "low" in s:
        return "low"
    if "impact-gra" in s or "non-economic" in s or "holiday" in s:
        return "low"
    return "low"


def _clean_value(v) -> Optional[str]:
    """Порожній рядок → None."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    return s