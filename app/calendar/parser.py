"""
Парсер економічного календаря ForexFactory (v4).

Особливості:
- fetch_events приймає або day, або week (week='this'|'last'|'next').
- Дата визначається з data-day-dateline на рядках calendar__row--new-day.
- Forward-fill часу в межах одного дня; скидається при переході на новий день.
- Importance: CSS-класи icon--ff-impact-* + fallback по title.
- Шумові події (bond auction, bank holiday, тощо) фільтруються.
"""

import logging
from datetime import datetime, timezone, date, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

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
# Класифікація важливості
# ---------------------------------------------------------------------------

HIGH_MARKERS = [
    # USD
    "federal funds rate", "fomc statement", "fomc economic projections",
    "fomc press conference", "non-farm employment", "nonfarm payrolls",
    "cpi m/m", "cpi y/y", "core cpi", "core pce", "gdp",
    "unemployment rate", "retail sales", "ism manufacturing pmi",
    "ism services pmi", "interest rate decision", "monetary policy statement",
    "fed chair", "average hourly earnings", "adp non-farm",
    # AUD
    "cash rate", "rate statement", "official cash rate",
    # EUR
    "main refinancing rate", "deposit facility rate", "refinancing rate",
    "ecb press conference", "ecb interest rate decision",
    "flash cpi", "prelim cpi", "trimmed mean cpi", "ecb monetary policy",
    # GBP
    "boe bank rate", "bank rate", "monetary policy decision",
    "monetary policy report", "mpc official bank rate",
    # JPY
    "boj press conference", "boj policy rate", "boj monetary policy",
    # Загальні
    "annual budget",
]

MEDIUM_MARKERS = [
    "fomc member", "speaks", "speech", "press conference",
    "testimony", "mpc member", "ecb president", "ecb member",
    "boe governor", "boe member", "boj governor", "boj member",
    "buba president", "boc governor",
    "ppi", "building permits", "consumer confidence",
    "industrial production", "trade balance", "mps vote", "mpc vote",
    "current account", "employment change", "average earnings",
    "gdp q/q", "gdp m/m", "retail sales m/m",
]

# Події, які пропускаємо (не мають конкретного часу, не рухають ринок)
SKIP_TITLE_PATTERNS = [
    "bond auction", "note auction", "bill auction",
    "bank holiday", "total vehicle sales", "holiday", "auction",
]


# ---------------------------------------------------------------------------
# Головна функція
# ---------------------------------------------------------------------------

def fetch_events(day: str = "today", week: Optional[str] = None) -> list[dict]:
    """
    Повертає список подій з ForexFactory.

    Аргументи:
        day:  'today' | 'tomorrow' | 'yesterday' (якщо week=None)
        week: 'this' | 'last' | 'next' (якщо задано, day ігнорується)
    """
    params: dict = {}
    if week:
        params["week"] = week
    else:
        params["day"] = day

    r = requests.get(
        FF_URL,
        params=params,
        cookies=FF_COOKIES,
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()

    soup = BeautifulSoup(r.content, "lxml")
    rows = soup.select("tr.calendar__row")

    events: list[dict] = []
    current_date: Optional[date] = _resolve_day(day)  # fallback
    last_time_text: str = ""

    for row in rows:
        row_classes = row.get("class") or []

        # --- Новий день: оновлюємо дату, скидаємо forward-fill ---
        if "calendar__row--new-day" in row_classes:
            dateline = row.get("data-day-dateline")
            if dateline:
                try:
                    current_date = datetime.fromtimestamp(
                        int(dateline), tz=timezone.utc
                    ).date()
                except (ValueError, TypeError):
                    pass
            last_time_text = ""

        title_cell = row.select_one(".calendar__event-title")
        currency_cell = row.select_one(".calendar__currency")
        time_cell = row.select_one(".calendar__time")

        if not title_cell or not currency_cell:
            continue

        title_text = title_cell.get_text(strip=True)
        currency_text = currency_cell.get_text(strip=True)
        if not title_text or not currency_text:
            continue

        # --- Фільтр шумових подій ---
        title_lower = title_text.lower()
        if any(p in title_lower for p in SKIP_TITLE_PATTERNS):
            logger.debug(f"Skip (noise): {title_text}")
            continue

        # --- Forward-fill часу ---
        time_text = time_cell.get_text(strip=True) if time_cell else ""
        if time_text:
            last_time_text = time_text
        elif last_time_text:
            time_text = last_time_text
        else:
            logger.debug(f"Skip (no time): {title_text}")
            continue

        impact = _parse_importance(row, title_text)
        time_utc = _extract_datetime(row, current_date, time_text)
        if time_utc is None:
            logger.warning(f"Skip (unparsable time): {title_text} [{currency_text}]")
            continue

        events.append({
            "provider": "forex_factory",
            "title": title_text,
            "country": currency_text,
            "importance": impact,
            "time_utc": time_utc,
            "forecast_value": _cell_text(row, ".calendar__forecast"),
            "previous_value": _cell_text(row, ".calendar__previous"),
            "actual_value": _cell_text(row, ".calendar__actual"),
        })

    logger.info(
        f"Fetched {len(events)} events "
        f"(day={day if not week else '-'}, week={week or '-'})"
    )
    return events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_day(day: str) -> date:
    """Fallback-дата, якщо в HTML немає data-day-dateline."""
    today = datetime.now(timezone.utc).date()
    mapping = {
        "today": today,
        "tomorrow": today + timedelta(days=1),
        "yesterday": today - timedelta(days=1),
    }
    return mapping.get(day, today)


def _parse_importance(row, title_text: str) -> str:
    """
    Пріоритет:
      1. CSS-клас іконки: icon--ff-impact-red / -ora / -yel
      2. Fallback по title (HIGH_MARKERS / MEDIUM_MARKERS)
    """
    impact = "low"

    impact_cell = row.select_one(".calendar__impact")
    if impact_cell:
        icon = (
            impact_cell.select_one("span[class*='icon--ff-impact']")
            or impact_cell.select_one("span[class*='impact-icon--']")
        )
        classes = " ".join(icon.get("class", [])) if icon else ""
        if not classes:
            classes = " ".join(impact_cell.get("class", []))

        if "impact-red" in classes or "impact-icon--high" in classes:
            impact = "high"
        elif "impact-ora" in classes or "impact-icon--medium" in classes:
            impact = "medium"
        elif "impact-yel" in classes or "impact-icon--low" in classes:
            impact = "low"

    if impact == "low":
        t = title_text.lower()
        if any(m in t for m in HIGH_MARKERS):
            impact = "high"
        elif any(m in t for m in MEDIUM_MARKERS):
            impact = "medium"

    return impact


def _extract_datetime(row, target_date: Optional[date], time_text: str) -> Optional[str]:
    """
    ISO UTC або None.

    Джерела:
      1. data-event-datetime на <tr>
      2. target_date + time_text ('3:15pm', '8:30am', '15:15')
      3. 'All Day' / 'Tentative' → 00:00 UTC
    """
    dt_attr = row.get("data-event-datetime")
    if dt_attr:
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %I:%M%p", "%Y/%m/%d %H:%M"):
            try:
                dt = datetime.strptime(dt_attr.strip(), fmt)
                return dt.replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                continue

    if target_date is None:
        return None

    t = (time_text or "").strip().lower()

    if not t or t in ("all day", "tentative", "day 1", "day 2"):
        dt = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
        return dt.isoformat()

    time_part = None
    for fmt in ("%I:%M%p", "%I%p", "%H:%M"):
        try:
            time_part = datetime.strptime(t, fmt).time()
            break
        except ValueError:
            continue

    if time_part is None:
        return None

    dt = datetime.combine(target_date, time_part, tzinfo=timezone.utc)
    return dt.isoformat()


def _cell_text(row, selector: str) -> Optional[str]:
    cell = row.select_one(selector)
    if not cell:
        return None
    text = cell.get_text(strip=True)
    return text or None