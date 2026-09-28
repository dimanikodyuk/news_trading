"""Швидка перевірка: що повертає ForexFactory для week=last."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.calendar.parser import fetch_events
import sqlite3
from app.config import settings


def show(label: str, events: list[dict], limit: int = 25):
    print(f"\n=== {label}: {len(events)} подій ===\n")
    for e in events[:limit]:
        print(f"[{e['importance']:6}] {e['country']:4} "
              f"{e['title'][:55]:55} | {e['time_utc']}")


def main():
    print(f"DB = {settings.db_path_abs}")

    # 1) week=last
    try:
        last = fetch_events(week="last")
        show("week=last", last)
    except Exception as ex:
        print(f"week=last FAILED: {ex}")

    # 2) week=this
    try:
        this = fetch_events(week="this")
        show("week=this", this)
    except Exception as ex:
        print(f"week=this FAILED: {ex}")

    # 3) day=today (для порівняння)
    try:
        today = fetch_events(day="today")
        show("day=today", today)
    except Exception as ex:
        print(f"day=today FAILED: {ex}")

    # 4) Що вже в БД
    print("\n=== БД: події за датами ===")
    c = sqlite3.connect(settings.db_path_abs)
    c.row_factory = sqlite3.Row
    total = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    print(f"Всього в БД: {total}")

    rows = c.execute("""
        SELECT substr(time_utc, 1, 10) AS d, COUNT(*) AS n
        FROM events GROUP BY d ORDER BY d
    """).fetchall()
    for r in rows:
        print(f"  {r['d']}: {r['n']}")

    print("\n=== БД: останні 15 подій ===")
    for r in c.execute(
        "SELECT id, title, time_utc, importance FROM events ORDER BY time_utc LIMIT 15"
    ):
        print(f"  [{r['importance']:6}] {r['title'][:50]:50} | {r['time_utc']}")


if __name__ == "__main__":
    main()