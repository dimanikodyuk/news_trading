"""Read-only database diagnostics. No live execution."""
from app.db import get_db

async def diagnostics_v21():
    async with get_db() as db:
        result = {}
        for name in ('events','event_impact','prices','backtest_trades','paper_trades'):
            cur=await db.execute(f'SELECT COUNT(*) AS n FROM {name}')
            result[name]=(await cur.fetchone())['n']
        cur=await db.execute("SELECT hit, COUNT(*) AS n FROM event_impact GROUP BY hit")
        result['hit_distribution']={r['hit']:r['n'] for r in await cur.fetchall()}
        cur=await db.execute("SELECT COUNT(*) AS n FROM event_impact i LEFT JOIN events e ON e.id=i.event_id WHERE e.id IS NULL")
        result['orphan_impacts']=(await cur.fetchone())['n']
        cur=await db.execute("SELECT COUNT(DISTINCT time_utc) AS n FROM events")
        result['distinct_release_timestamps']=(await cur.fetchone())['n']
        result['note']='HIT/MISS is not a profitable-trade metric; timestamps are clusters, not necessarily independent macro shocks.'
        return result

async def clusters_v21(limit=100):
    async with get_db() as db:
        cur=await db.execute("""SELECT time_utc, COUNT(*) AS event_count,
               GROUP_CONCAT(title, ' | ') AS titles,
               SUM(CASE WHEN importance='high' THEN 1 ELSE 0 END) AS high_count
               FROM events GROUP BY time_utc ORDER BY time_utc DESC LIMIT ?""",(limit,))
        return {'clusters':[dict(r) for r in await cur.fetchall()], 'note':'One timestamp is one release cluster for statistical grouping.'}
