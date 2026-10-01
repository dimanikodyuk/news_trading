"""
Метрики paper-акаунту.
"""

from app.db import get_db


async def get_account() -> dict:
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM paper_account WHERE id = 1")
        acc = await cur.fetchone()
        if not acc:
            return {"error": "no account"}

        cur = await db.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status='OPEN'   THEN 1 ELSE 0 END) AS open_n,
                SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END) AS closed_n,
                SUM(CASE WHEN status='CLOSED' AND pnl > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN status='CLOSED' AND pnl <= 0 THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN status='CLOSED' THEN pnl ELSE 0 END) AS total_pnl,
                AVG(CASE WHEN status='CLOSED' AND pnl > 0 THEN pnl END) AS avg_win,
                AVG(CASE WHEN status='CLOSED' AND pnl <= 0 THEN pnl END) AS avg_loss
            FROM paper_trades
        """)
        s = await cur.fetchone()

    initial = float(acc["initial_balance"])
    balance = float(acc["balance"])
    # open trades «заморожують» частину балансу — додаємо їх для повної equity
    # (для простоти: equity = balance + сума size_usd відкритих)
    async with get_db() as db:
        cur = await db.execute("""
            SELECT COALESCE(SUM(size_usd + entry_fee), 0) AS frozen
            FROM paper_trades WHERE status='OPEN'
        """)
        frozen = float((await cur.fetchone())["frozen"])

    equity = balance + frozen
    total_pnl = equity - initial
    total_pnl_pct = (total_pnl / initial * 100) if initial > 0 else 0

    wins = s["wins"] or 0
    losses = s["losses"] or 0
    closed_n = s["closed_n"] or 0
    win_rate = (wins / closed_n * 100) if closed_n > 0 else None

    return {
        "initial_balance": initial,
        "balance": round(balance, 4),
        "frozen": round(frozen, 4),
        "equity": round(equity, 4),
        "total_pnl": round(total_pnl, 4),
        "total_pnl_pct": round(total_pnl_pct, 3),
        "total_trades": s["total"] or 0,
        "open_trades": s["open_n"] or 0,
        "closed_trades": closed_n,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 1) if win_rate is not None else None,
        "avg_win": round(s["avg_win"], 4) if s["avg_win"] is not None else None,
        "avg_loss": round(s["avg_loss"], 4) if s["avg_loss"] is not None else None,
    }


async def get_trades(limit: int = 100, status: str | None = None) -> list[dict]:
    where = "1=1"
    params: list = []
    if status:
        where += " AND pt.status = ?"
        params.append(status)

    async with get_db() as db:
        cur = await db.execute(f"""
            SELECT
                pt.*,
                e.title AS event_title,
                e.time_utc AS event_time
            FROM paper_trades pt
            JOIN events e ON e.id = pt.event_id
            WHERE {where}
            ORDER BY pt.id DESC
            LIMIT ?
        """, params + [limit])
        rows = await cur.fetchall()

    return [dict(r) for r in rows]


async def get_equity_curve() -> list[dict]:
    """
    Простий equity curve: для кожної закритої угоди — кумулятивний P&L.
    """
    async with get_db() as db:
        cur = await db.execute("""
            SELECT id, closed_at, pnl
            FROM paper_trades
            WHERE status = 'CLOSED'
            ORDER BY closed_at ASC
        """)
        rows = await cur.fetchall()

    points = []
    cum = 0.0
    for r in rows:
        cum += float(r["pnl"] or 0)
        points.append({
            "trade_id": r["id"],
            "ts": r["closed_at"],
            "cum_pnl": round(cum, 4),
        })
    return points


async def reset_account() -> dict:
    """Скидає баланс до $100 і видаляє всі угоди."""
    async with get_db() as db:
        await db.execute("DELETE FROM paper_trades")
        await db.execute("""
            UPDATE paper_account
            SET balance = initial_balance,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
        """)
        await db.commit()

    return {"status": "reset", "balance": 100.0}