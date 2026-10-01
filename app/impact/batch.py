"""
Пакетний розрахунок impact для всіх минулих подій × всіх символів.
"""

import logging
from datetime import datetime, timezone

from app.db import get_db
from app.impact.calculator import compute_impact

logger = logging.getLogger(__name__)

SYMBOLS = ["SOLUSDT", "BTCUSDT", "ETHUSDT"]


async def process_all_past_events(limit: int = 500) -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()

    stats = {"processed": 0, "saved": 0, "skipped": 0, "errors": 0}

    async with get_db() as db:
        # Перераховуємо події, якщо:
        #   1) немає жодного impact з повними даними (< 3 записи)
        #   2) hit = 'NO_DATA' або 'N/A', але тепер є actual_value
        #      (правило могло додатись або actual з'явився пізніше)
        cur = await db.execute("""
            SELECT e.* FROM events e
            WHERE e.time_utc < ?
              AND e.importance IN ('high', 'medium')
              AND (
                -- Немає жодного impact з повними даними
                (SELECT COUNT(*) FROM event_impact ei
                 WHERE ei.event_id = e.id AND ei.ret_60m IS NOT NULL) < 3
                OR
                -- Або hit = NO_DATA/N/A, але тепер є actual → треба перерахувати
                EXISTS (
                    SELECT 1 FROM event_impact ei
                    WHERE ei.event_id = e.id
                      AND ei.hit IN ('NO_DATA', 'N/A')
                      AND e.actual_value IS NOT NULL
                      AND e.actual_value != ''
                )
              )
            ORDER BY e.time_utc DESC
            LIMIT ?
        """, (now_iso, limit))
        events = await cur.fetchall()

        logger.info(f"Знайдено {len(events)} подій для обробки")

        for ev in events:
            stats["processed"] += 1
            try:
                any_saved = False
                for sym in SYMBOLS:
                    impact = await compute_impact(db, ev, symbol=sym)
                    if impact is None:
                        continue

                    await db.execute("""
                        INSERT OR REPLACE INTO event_impact
                        (event_id, symbol, importance,
                         price_baseline, price_t1, price_t5, price_t15, price_t30, price_t60,
                         ret_1m, ret_5m, ret_15m, ret_30m, ret_60m,
                         dir_1m, dir_5m, dir_15m, dir_30m, dir_60m,
                         expected_dir, hit,
                         threshold_pct)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        impact["event_id"], impact["symbol"], impact["importance"],
                        impact["price_baseline"],
                        impact.get("price_t1"), impact.get("price_t5"),
                        impact.get("price_t15"), impact.get("price_t30"),
                        impact.get("price_t60"),
                        impact.get("ret_1m"), impact.get("ret_5m"),
                        impact.get("ret_15m"), impact.get("ret_30m"),
                        impact.get("ret_60m"),
                        impact.get("dir_1m"), impact.get("dir_5m"),
                        impact.get("dir_15m"), impact.get("dir_30m"),
                        impact.get("dir_60m"),
                        impact.get("expected_dir"), impact.get("hit"),
                        impact["threshold_pct"],
                    ))
                    any_saved = True

                if any_saved:
                    stats["saved"] += 1
                else:
                    stats["skipped"] += 1

            except Exception as ex:
                logger.exception(f"[event {ev['id']}] помилка: {ex}")
                stats["errors"] += 1

        await db.commit()

    logger.info(f"Готово: {stats}")
    return stats