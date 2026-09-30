"""
Агрегована статистика по impact + cross-asset + excess return.
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
        # --- Overall ---
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
                SUM(CASE WHEN hit = 'N/A'     THEN 1 ELSE 0 END) AS na,
                SUM(CASE WHEN hit = 'NO_DATA' THEN 1 ELSE 0 END) AS no_data
            FROM event_impact ei
            WHERE {where_sql}
        """, params)
        overall = await cur.fetchone()

        # --- By title ---
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
                SUM(CASE WHEN ei.hit = 'N/A'     THEN 1 ELSE 0 END) AS na,
                SUM(CASE WHEN ei.hit = 'NO_DATA' THEN 1 ELSE 0 END) AS no_data
            FROM event_impact ei
            JOIN events e ON e.id = ei.event_id
            WHERE {where_sql}
            GROUP BY e.title, ei.importance
            HAVING COUNT(*) >= ?
            ORDER BY n DESC
        """, params + [min_events])
        by_title = [dict(r) for r in await cur.fetchall()]

        # --- Cross-asset: SOL vs BTC vs ETH ---
        cur = await db.execute("""
            SELECT
                ei.event_id,
                e.title,
                e.time_utc,
                e.importance,
                MAX(CASE WHEN ei.symbol = 'SOLUSDT' THEN ei.ret_15m END) AS sol_15m,
                MAX(CASE WHEN ei.symbol = 'SOLUSDT' THEN ei.dir_15m END) AS sol_dir,
                MAX(CASE WHEN ei.symbol = 'BTCUSDT' THEN ei.ret_15m END) AS btc_15m,
                MAX(CASE WHEN ei.symbol = 'BTCUSDT' THEN ei.dir_15m END) AS btc_dir,
                MAX(CASE WHEN ei.symbol = 'ETHUSDT' THEN ei.ret_15m END) AS eth_15m,
                MAX(CASE WHEN ei.symbol = 'ETHUSDT' THEN ei.dir_15m END) AS eth_dir
            FROM event_impact ei
            JOIN events e ON e.id = ei.event_id
            WHERE ei.ret_15m IS NOT NULL
            GROUP BY ei.event_id
            ORDER BY e.time_utc DESC
            LIMIT 200
        """)
        cross_asset = [dict(r) for r in await cur.fetchall()]

        # Агрегація: скільки разів SOL рухався в той самий бік, що BTC
        same_as_btc = 0
        total_with_btc = 0
        sum_sol = 0.0
        sum_btc = 0.0
        n_excess = 0

        for row in cross_asset:
            if row["sol_dir"] and row["btc_dir"] and row["sol_dir"] != "flat" and row["btc_dir"] != "flat":
                total_with_btc += 1
                if row["sol_dir"] == row["btc_dir"]:
                    same_as_btc += 1

            if row["sol_15m"] is not None and row["btc_15m"] is not None:
                sum_sol += row["sol_15m"]
                sum_btc += row["btc_15m"]
                n_excess += 1

        corr_btc = (same_as_btc / total_with_btc * 100) if total_with_btc > 0 else None
        avg_sol = (sum_sol / n_excess) if n_excess > 0 else None
        avg_btc = (sum_btc / n_excess) if n_excess > 0 else None
        excess = (avg_sol - avg_btc) if (avg_sol is not None and avg_btc is not None) else None

    return {
        "overall": dict(overall) if overall else {},
        "by_title": by_title,
        "cross_asset": cross_asset,
        "cross_asset_summary": {
            "n_events": total_with_btc,
            "same_direction_as_btc": same_as_btc,
            "correlation_pct": corr_btc,
            "avg_sol_ret_15m": avg_sol,
            "avg_btc_ret_15m": avg_btc,
            "avg_excess_sol_vs_btc": excess,
        },
    }


async def get_heatmap(importance: str | None = None) -> dict:
    where = "WHERE ei.ret_15m IS NOT NULL"
    params: list = []
    if importance:
        where += " AND e.importance = ?"
        params.append(importance)

    async with get_db() as db:
        cur = await db.execute(f"""
            SELECT
                CAST(strftime('%w', e.time_utc) AS INTEGER) AS dow,
                CAST(strftime('%H', e.time_utc) AS INTEGER) AS hour,
                COUNT(*) AS n,
                AVG(ei.ret_15m) AS avg_15m,
                AVG(ei.ret_60m) AS avg_60m
            FROM event_impact ei
            JOIN events e ON e.id = ei.event_id
            {where}
            GROUP BY dow, hour
            ORDER BY dow, hour
        """, params)
        rows = [dict(r) for r in await cur.fetchall()]

    return {"cells": rows}


async def get_cross_asset_detail(event_id: int) -> dict:
    async with get_db() as db:
        cur = await db.execute("""
            SELECT symbol, ret_1m, ret_5m, ret_15m, ret_30m, ret_60m,
                   dir_1m, dir_5m, dir_15m, dir_30m, dir_60m
            FROM event_impact
            WHERE event_id = ?
        """, (event_id,))
        rows = [dict(r) for r in await cur.fetchall()]
    return {r["symbol"]: r for r in rows}