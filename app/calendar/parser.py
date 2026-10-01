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
def _js_object_to_json(js: str) -> str:
    """
    Груба конвертація JS-об'єкта у JSON:
    1. Екрановані слеші: \\/ → /
    2. Ключі без лапок: word: → "word":
    3. Одинарні лапки → подвійні (тільки для рядків, не всередині "..."):
       'text' → "text"
    4. Trailing commas
    """
    import re as _re

    # 1) Екрановані слеші
    s = js.replace("\\/", "/")

    # 2) Ключі без лапок: {word: або ,word: → {"word": або ,"word":
    s = _re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)', r'\1"\2"\3', s)

    # 3) Конвертація одинарних лапок у подвійні (акуратно)
    s = _single_to_double_quotes(s)

    # 4) Trailing commas перед } або ]
    s = _re.sub(r',(\s*[}\]])', r'\1', s)

    return s


def _single_to_double_quotes(s: str) -> str:
    """
    Замінює 'text' на "text", але не чіпає:
    - Рядки всередині "..." (подвійні лапки)
    - Екрановані \'
    """
    result = []
    i = 0
    n = len(s)
    in_double = False
    in_single = False
    escape = False

    while i < n:
        ch = s[i]

        if escape:
            result.append(ch)
            escape = False
            i += 1
            continue

        if ch == "\\":
            result.append(ch)
            escape = True
            i += 1
            continue

        if in_double:
            result.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue

        if in_single:
            if ch == "'":
                # Кінець рядка в одинарних лапках — закриваємо подвійною
                result.append('"')
                in_single = False
            elif ch == '"':
                # Лапка всередині '...' — екрануємо
                result.append('\\"')
            else:
                result.append(ch)
            i += 1
            continue

        # Не в лапках
        if ch == '"':
            in_double = True
            result.append(ch)
        elif ch == "'":
            in_single = True
            result.append('"')
        else:
            result.append(ch)
        i += 1

    return "".join(result)

def _extract_calendar_json(html: str) -> Optional[str]:
    """
    Знаходить `window.calendarComponentStates[1] = {...}` і повертає
    валідний JSON-рядок.

    ForexFactory віддає JS-об'єкт (без лапок на ключах, з коментарями,
    з function() {...}). Тому треба конвертувати JS → JSON.
    """
    marker = "window.calendarComponentStates[1]"
    idx = html.find(marker)
    if idx < 0:
        return None

    eq_idx = html.find("=", idx)
    if eq_idx < 0:
        return None
    brace_start = html.find("{", eq_idx)
    if brace_start < 0:
        return None

    # Балансуємо фігурні дужки, враховуючи лапки (' і ") та escape
    depth = 0
    in_string = False
    string_char = None
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
        if in_string:
            if ch == string_char:
                in_string = False
                string_char = None
            continue
        if ch in ('"', "'"):
            in_string = True
            string_char = ch
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

    raw_js = html[brace_start:end]

    # --- Конвертуємо JS → JSON ---
    json_str = _js_object_to_json(raw_js)
    return json_str





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