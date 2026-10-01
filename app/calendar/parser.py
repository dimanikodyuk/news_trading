"""
Парсер економічного календаря ForexFactory через JSON у HTML.

ForexFactory вбудовує дані подій як JS-об'єкт у змінну
`window.calendarComponentStates[1]`. Це дає нам:
- actual, forecast, previous, revision
- impactClass (high/medium/low)
- dateline (unix timestamp)
- actualBetterWorse

JS-об'єкт не є валідним JSON, тому його конвертуємо:
1. Object.freeze(X) → X (з балансуванням дужок)
2. Прибираємо \\/
3. Додаємо лапки навколо ключів
4. Замінюємо одинарні лапки на подвійні
5. Прибираємо trailing commas

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


# ---------------------------------------------------------------------------
# Публічний API
# ---------------------------------------------------------------------------

def fetch_events(day: str = "today", week: Optional[str] = None) -> list[dict]:
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

    raw_json = _extract_calendar_json(r.text)
    if raw_json is None:
        logger.error("Не вдалось знайти calendarComponentStates у HTML")
        return []

    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as ex:
        logger.error(f"Помилка парсингу JSON: {ex}")
        try:
            pos = ex.pos
            frag = raw_json[max(0, pos - 100):pos + 100]
            logger.error(f"Позиція {pos}: ...{frag}...")
        except Exception:
            pass
        return []

    days = data.get("days", [])
    events: list[dict] = []

    for day_obj in days:
        for ev in day_obj.get("events", []):
            parsed = _parse_event(ev)
            if parsed is not None:
                events.append(parsed)

    logger.info(f"Fetched {len(events)} events (day={day}, week={week or '-'})")
    return events


# ---------------------------------------------------------------------------
# Витягування JSON з HTML
# ---------------------------------------------------------------------------

def _extract_calendar_json(html: str) -> Optional[str]:
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
    return _js_object_to_json(raw_js)


# ---------------------------------------------------------------------------
# Конвертація JS → JSON
# ---------------------------------------------------------------------------

def _js_object_to_json(js: str) -> str:
    # 1) Екрановані слеші
    s = js.replace("\\/", "/")

    # 2) Object.freeze(X) → X (з балансуванням дужок)
    s = _unwrap_object_freeze(s)

    # 3) Ключі без лапок
    s = re.sub(
        r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)',
        r'\1"\2"\3',
        s,
    )

    # 4) Одинарні лапки → подвійні
    s = _single_to_double_quotes(s)

    # 5) Trailing commas
    s = re.sub(r',(\s*[}\]])', r'\1', s)

    return s


def _unwrap_object_freeze(s: str) -> str:
    """
    Object.freeze(X) → X, з балансуванням дужок.
    Object.freeze(({"a": 1})) → ({"a": 1}) — вміст зберігається як є.
    Працює як для об'єктів {}, так і для масивів [].
    """
    result = []
    i = 0
    n = len(s)
    marker = "Object.freeze("
    marker_len = len(marker)

    while i < n:
        if s[i:i+marker_len] == marker:
            # Знаходимо парну ")" для цього "("
            j = i + marker_len
            depth = 1
            in_str = False
            str_ch = None
            esc = False

            while j < n and depth > 0:
                ch = s[j]
                if esc:
                    esc = False
                    j += 1
                    continue
                if ch == "\\":
                    esc = True
                    j += 1
                    continue
                if in_str:
                    if ch == str_ch:
                        in_str = False
                        str_ch = None
                    j += 1
                    continue
                if ch in ('"', "'"):
                    in_str = True
                    str_ch = ch
                    j += 1
                    continue
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1

            # Вміст між "Object.freeze(" і відповідною ")"
            inner = s[i + marker_len:j]
            result.append(inner)
            i = j + 1  # пропускаємо закриваючу ")"
        else:
            result.append(s[i])
            i += 1

    return "".join(result)


def _single_to_double_quotes(s: str) -> str:
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
                result.append('"')
                in_single = False
            elif ch == '"':
                result.append('\\"')
            else:
                result.append(ch)
            i += 1
            continue

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


# ---------------------------------------------------------------------------
# Парсинг однієї події
# ---------------------------------------------------------------------------

def _parse_event(ev: dict) -> Optional[dict]:
    name = ev.get("name") or ev.get("soloTitle") or ""
    if not name:
        return None

    currency = ev.get("currency") or ""
    if not currency:
        country = ev.get("country") or ""
        currency = country[:3].upper() if len(country) >= 3 else country
    if not currency:
        return None

    dateline = ev.get("dateline")
    if not dateline:
        return None
    try:
        dt = datetime.fromtimestamp(int(dateline), tz=timezone.utc)
        time_utc = dt.isoformat()
    except (ValueError, TypeError, OSError):
        return None

    impact = _parse_impact(ev.get("impactClass") or ev.get("impactName") or "")

    forecast = _clean_value(ev.get("forecast"))
    previous = _clean_value(ev.get("previous"))
    actual = _clean_value(ev.get("actual"))
    revision = _clean_value(ev.get("revision"))

    return {
        "provider": "forex_factory",
        "title": name,
        "country": currency,
        "importance": impact,
        "time_utc": time_utc,
        "forecast_value": forecast,
        "previous_value": previous,
        "actual_value": actual,
        "_revision": revision,
        "_actual_better_worse": ev.get("actualBetterWorse"),
        "_event_id": ev.get("id"),
    }


def _parse_impact(impact_str: str) -> str:
    s = (impact_str or "").lower()
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
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    return s