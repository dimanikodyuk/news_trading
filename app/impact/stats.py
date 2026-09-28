"""
Агрегована статистика по impact.
"""

from app.db import get_db


async def get_stats(symbol: str | None = None,
                    importance: str | None = None,
                    min_events: int = 1) -> dict:
    where = ["1=1"]
    params: list = []
    if symbol:
        where.append("ei.symbol = ?")
        params.append(symbol)
    if importance:
        where.append("ei.importance = ?")
        params.append(importance)
    where_sql = " AND ".join(where)

    async with get_db() as db:
        # Загальна статистика
        cur = await db.execute(f"""
            SELECT
                COUNT(*) AS n,
                AVG(ret_5m)  AS avg_5m,
                AVG(ret_15m) AS avg_15m,
                AVG(ret_30m) AS avg_30m,
                AVG(ret_60m) AS avg_60m,
                SUM(CASE WHEN dir_15m = 'up'   THEN 1 ELSE 0 END) AS up_15m,
                SUM(CASE WHEN dir_15m = 'down' THEN 1 ELSE 0 END) AS down_15m,
                SUM(CASE WHEN dir_15m = 'flat' THEN 1 ELSE 0 END) AS flat_15m,
                SUM(CASE WHEN hit = 'HIT'     THEN 1 ELSE 0 END) AS hits,
                SUM(CASE WHEN hit = 'MISS'    THEN 1 ELSE 0 END) AS misses,
                SUM(CASE WHEN hit = 'NEUTRAL' THEN 1 ELSE 0 END) AS neutrals,
                SUM(CASE WHEN hit = 'N/A'     THEN 1 ELSE 0 END) AS na
            FROM event_impact ei
            WHERE {where_sql}
        """, params)
        overall = await cur.fetchone()

        # По кожному title — для таблиці + діаграм
        cur = await db.execute(f"""
            SELECT
                e.title,
                ei.importance,
                COUNT(*) AS n,
                AVG(ei.ret_5m)  AS avg_5m,
                AVG(ei.ret_15m) AS avg_15m,
                AVG(ei.ret_30m) AS avg_30m,
                AVG(ei.ret_60m) AS avg_60m,
                SUM(CASE WHEN ei.dir_15m = 'up'   THEN 1 ELSE 0 END) AS up_15m,
                SUM(CASE WHEN ei.dir_15m = 'down' THEN 1 ELSE 0 END) AS down_15m,
                SUM(CASE WHEN ei.dir_15m = 'flat' THEN 1 ELSE 0 END) AS flat_15m,
                SUM(CASE WHEN ei.hit = 'HIT'     THEN 1 ELSE 0 END) AS hits,
                SUM(CASE WHEN ei.hit = 'MISS'    THEN 1 ELSE 0 END) AS misses,
                SUM(CASE WHEN ei.hit = 'NEUTRAL' THEN 1 ELSE 0 END) AS neutrals,
                SUM(CASE WHEN ei.hit = 'N/A'     THEN 1 ELSE 0 END) AS na
            FROM event_impact ei
            JOIN events e ON e.id = ei.event_id
            WHERE {where_sql}
            GROUP BY e.title, ei.importance
            HAVING COUNT(*) >= ?
            ORDER BY n DESC
        """, params + [min_events])
        by_title = [dict(r) for r in await cur.fetchall()]

    return {
        "overall": dict(overall) if overall else {},
        "by_title": by_title,
    }