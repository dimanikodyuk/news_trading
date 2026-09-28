"""
Діагностика парсера ForexFactory.
Показує: скільки подій, які importance, які часи, який HTML.
"""
import requests
from bs4 import BeautifulSoup

from app.calendar.parser import (
    FF_URL, FF_COOKIES, HEADERS,
    fetch_events,
)

# ---------------------------------------------------------------
# 1. Що повертає парсер
# ---------------------------------------------------------------
print("\n=== 1. Результат fetch_events(day='today') ===\n")
events = fetch_events(day="today")
print(f"Всього подій: {len(events)}\n")
for e in events[:20]:
    print(f"[{e['importance']:6}] {e['country']:4} "
          f"{e['title'][:55]:55} | {e['time_utc']}")

# ---------------------------------------------------------------
# 2. Сирий HTML — структура
# ---------------------------------------------------------------
print("\n=== 2. Сирий HTML ===\n")
r = requests.get(FF_URL, params={"day": "today"},
                 cookies=FF_COOKIES, headers=HEADERS, timeout=30)
soup = BeautifulSoup(r.content, "lxml")
rows = soup.select("tr.calendar__row")
print(f"Рядків з класом .calendar__row: {len(rows)}")

# ---------------------------------------------------------------
# 3. Перші 15 time_cell + title (з захистом від None)
# ---------------------------------------------------------------
print("\n=== 3. time_cell vs title (перші 15) ===\n")
for i, row in enumerate(rows[:15]):
    tc = row.select_one(".calendar__time")
    title = row.select_one(".calendar__event-title")
    currency = row.select_one(".calendar__currency")

    time_val = tc.get_text(strip=True) if tc else "<no cell>"
    title_val = title.get_text(strip=True) if title else "<no title>"
    currency_val = currency.get_text(strip=True) if currency else "<no cur>"

    print(f"{i:2}: time={time_val!r:15} | {currency_val:4} | {title_val[:50]}")

# ---------------------------------------------------------------
# 4. Повний HTML одного рядка з часom, який має бути непорожнім
# ---------------------------------------------------------------
print("\n=== 4. HTML першого рядка з непорожнім часом ===\n")
found = False
for row in rows:
    tc = row.select_one(".calendar__time")
    if tc and tc.get_text(strip=True):
        print(row.prettify()[:3500])
        found = True
        break

if not found:
    print("ЖОДЕН рядок не має непорожнього .calendar__time!")
    print("Перший рядок для довідки:")
    if rows:
        print(rows[0].prettify()[:3500])

# ---------------------------------------------------------------
# 5. Перевірка data-event-datetime
# ---------------------------------------------------------------
print("\n=== 5. Наявність data-event-datetime у рядків ===\n")
count_with_dt = sum(1 for row in rows if row.get("data-event-datetime"))
print(f"Рядків з data-event-datetime: {count_with_dt} / {len(rows)}")
if count_with_dt:
    for row in rows:
        dt = row.get("data-event-datetime")
        if dt:
            print(f"  приклад: {dt!r}")
            break