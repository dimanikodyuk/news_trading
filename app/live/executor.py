"""
LIVE trading executor для Bybit Spot.

Режими:
- live_enabled = False (за замовчуванням): DRY-RUN. Логує що б зробив, не відправляє ордери.
- live_enabled = True: реальні Market ордери на Bybit Spot.

Логіка сигналу — та сама, що в naive (Варіант B):
- Всі high-події, групуються по хвилині.
- Зважене голосування: score = up - down.
- score > 0 → LONG (Buy), score < 0 → SHORT (Sell), score == 0 → пропуск.
- Символи: SOLUSDT, BTCUSDT.
- Вхід: Market ордер на T+1 хв після події.
- Розмір: settings.live_trade_size_usd (обмежений % від балансу).

Пише у таблицю live_trades (status: PENDING / FILLED / REJECTED / DRY_RUN).
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from pybit.unified_trading import HTTP

from app.config import settings
from app.db import get_db
from app.impact.expected import get_expected_direction

logger = logging.getLogger(__name__)

# Символи для LIVE (SOL + BTC, без ETH — ширший спред)
LIVE_SYMBOLS = ["SOLUSDT", "BTCUSDT"]

# Той самий group_key, що в naive
def _iso_to_ms(iso_str: str) -> Optional[int]:
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _group_key(time_utc: str) -> Optional[str]:
    try:
        s = time_utc.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M")
    except Exception:
        return None


def _get_session() -> Optional[HTTP]:
    """Повертає Bybit HTTP-сесію або None, якщо ключів немає."""
    if not settings.bybit_api_key or not settings.bybit_api_secret:
        logger.warning("[LIVE] Bybit API ключі не задані в .env")
        return None
    return HTTP(
        testnet=settings.bybit_testnet,
        api_key=settings.bybit_api_key,
        api_secret=settings.bybit_api_secret,
    )


async def get_bybit_balance() -> dict:
    """
    Повертає баланс USDT.
    Працює і для Unified Trading Account (UTA), і для класичного Spot.
    Безпечно обробляє порожні рядки ("") від Bybit.
    """
    session = _get_session()
    if session is None:
        return {"error": "API ключі не задані"}

    def _safe_float(v, default=0.0):
        """float(v) але безпечно для '', None, 'N/A' тощо."""
        if v is None:
            return default
        try:
            s = str(v).strip()
            if not s:
                return default
            return float(s)
        except (ValueError, TypeError):
            return default

    try:
        # 1) Пробуємо UNIFIED (UTA)
        resp = session.get_wallet_balance(accountType="UNIFIED")
        if resp.get("retCode") == 0:
            result_list = resp.get("result", {}).get("list", [])
            if result_list:
                coins = result_list[0].get("coin", [])
                for c in coins:
                    if c.get("coin") == "USDT":
                        # У UTA реальний баланс — у полях equity / walletBalance / free
                        # Пробуємо в порядку пріоритету
                        balance = (
                            _safe_float(c.get("walletBalance"))
                            or _safe_float(c.get("equity"))
                            or _safe_float(c.get("usdValue"))
                        )
                        available = (
                            _safe_float(c.get("availableToWithdraw"))
                            or _safe_float(c.get("free"))
                            or _safe_float(c.get("availableToBorrow"))
                            or balance
                        )
                        return {
                            "usdt_balance": balance,
                            "usdt_available": available,
                            "account_type": "UNIFIED",
                        }
                # USDT не знайдено серед монет
                return {
                    "usdt_balance": 0.0,
                    "usdt_available": 0.0,
                    "account_type": "UNIFIED",
                    "note": "USDT не знайдено серед монет",
                }
            return {
                "usdt_balance": 0.0,
                "usdt_available": 0.0,
                "account_type": "UNIFIED",
                "note": "Порожній список монет",
            }

        # 2) Fallback — класичний SPOT
        resp2 = session.get_wallet_balance(accountType="SPOT")
        if resp2.get("retCode") == 0:
            result_list = resp2.get("result", {}).get("list", [])
            if result_list:
                coins = result_list[0].get("coin", [])
                for c in coins:
                    if c.get("coin") == "USDT":
                        balance = _safe_float(c.get("walletBalance"))
                        available = _safe_float(c.get("free")) or balance
                        return {
                            "usdt_balance": balance,
                            "usdt_available": available,
                            "account_type": "SPOT",
                        }
            return {
                "usdt_balance": 0.0,
                "usdt_available": 0.0,
                "account_type": "SPOT",
            }

        # 3) Не вдалось жодним способом
        return {
            "error": (
                f"Bybit: UNIFIED retCode={resp.get('retCode')} "
                f"({resp.get('retMsg', '?')}); "
                f"SPOT retCode={resp2.get('retCode')} "
                f"({resp2.get('retMsg', '?')})"
            )
        }

    except Exception as ex:
        logger.exception(f"[LIVE] get_bybit_balance failed: {ex}")
        return {"error": f"{type(ex).__name__}: {ex}"}


async def process_live_engine() -> dict:
    """
    Головна функція LIVE engine. Викликається з scheduler (кожну хвилину)
    або вручну через API.

    Повертає статистику: {"opened": N, "skipped": M, "errors": K, "dry_run": bool}
    """
    # Safety interlock: LIVE execution is intentionally disabled in this release.
    # A working dry-run must not be interpreted as exchange execution readiness.
    settings.live_enabled = False
    stats = {"opened": 0, "skipped": 0, "errors": 0, "dry_run": True}

    # Перевірка балансу (тільки якщо LIVE увімкнено)
    available_balance = None
    if settings.live_enabled:
        bal = await get_bybit_balance()
        if "error" in bal:
            logger.error(f"[LIVE] Не вдалось отримати баланс: {bal['error']}")
            stats["errors"] += 1
            return stats
        available_balance = bal.get("usdt_available", 0)
        if available_balance < 1.0:
            logger.warning(f"[LIVE] Недостатньо USDT: {available_balance}")
            stats["skipped"] += 1
            return stats

    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)

    # Вікно: події за останні 2 хвилини (щоб не пропустити T+1)
    # Engine запускається кожну хвилину, тому беремо події з time_utc
    # в межах [now - 90s, now - 30s] (T+1 вже настав, але не пізніше T+30)
    lo_iso = (now.replace(microsecond=0) - __import__("datetime").timedelta(seconds=90)).isoformat()
    hi_iso = (now.replace(microsecond=0) - __import__("datetime").timedelta(seconds=30)).isoformat()

    async with get_db() as db:
        cur = await db.execute("""
            SELECT e.id, e.title, e.time_utc,
                   e.forecast_value, e.actual_value
            FROM events e
            WHERE e.importance = 'high'
              AND e.time_utc BETWEEN ? AND ?
            ORDER BY e.time_utc ASC
        """, (lo_iso, hi_iso))
        events = await cur.fetchall()

        if not events:
            return stats

        # Групуємо
        groups: dict[str, list] = {}
        for ev in events:
            gk = _group_key(ev["time_utc"])
            if gk is None:
                continue
            groups.setdefault(gk, []).append(ev)

        for gk, events_in_group in groups.items():
            votes_up = 0
            votes_down = 0
            titles = []
            event_ids = []
            group_time_utc = events_in_group[0]["time_utc"]

            for ev in events_in_group:
                event_ids.append(ev["id"])
                titles.append(ev["title"])
                direction = get_expected_direction(
                    ev["title"], ev["forecast_value"], ev["actual_value"]
                )
                if direction == "up":
                    votes_up += 1
                elif direction == "down":
                    votes_down += 1

            score = votes_up - votes_down
            if score == 0:
                stats["skipped"] += 1
                continue

            trade_direction = "long" if score > 0 else "short"
            group_title = " | ".join(titles)
            event_ts_ms = _iso_to_ms(group_time_utc)
            if event_ts_ms is None:
                continue

            for sym in LIVE_SYMBOLS:
                # Дедуплікація: чи вже є запис для цієї групи і символу?
                cur = await db.execute("""
                    SELECT 1 FROM live_trades
                    WHERE event_group_key = ? AND symbol = ?
                """, (gk, sym))
                if await cur.fetchone():
                    stats["skipped"] += 1
                    continue

                # Розмір угоди: settings.live_trade_size_usd, але не більше
                # live_max_pct_of_balance % від доступного балансу
                size_usd = settings.live_trade_size_usd
                if available_balance is not None:
                    max_size = available_balance * (settings.live_max_pct_of_balance / 100)
                    if size_usd > max_size:
                        size_usd = max_size
                if size_usd < 1.0:
                    stats["skipped"] += 1
                    continue

                # Записуємо PENDING (або DRY_RUN)
                status = "PENDING" if settings.live_enabled else "DRY_RUN"
                await db.execute("""
                    INSERT INTO live_trades
                    (event_group_key, event_ids, event_title, event_time_utc,
                     symbol, direction, votes_up, votes_down, size_usd, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    gk,
                    ",".join(str(x) for x in event_ids),
                    group_title,
                    group_time_utc,
                    sym,
                    trade_direction,
                    votes_up,
                    votes_down,
                    size_usd,
                    status,
                ))
                await db.commit()

                # Якщо DRY_RUN — тільки лог
                if not settings.live_enabled:
                    logger.info(
                        f"[LIVE DRY-RUN] Було б: {trade_direction.upper()} {sym} "
                        f"${size_usd:.2f} (score={score}, up={votes_up}, down={votes_down})"
                    )
                    stats["opened"] += 1
                    continue

                # Реальний ордер
                session = _get_session()
                if session is None:
                    await db.execute("""
                        UPDATE live_trades SET status='REJECTED', error_message='No API session'
                        WHERE event_group_key=? AND symbol=?
                    """, (gk, sym))
                    await db.commit()
                    stats["errors"] += 1
                    continue

                try:
                    # Отримуємо qty precision для символу
                    info = session.get_instruments_info(category="spot", symbol=sym)
                    if info.get("retCode") != 0:
                        raise RuntimeError(f"get_instruments_info: {info.get('retMsg')}")

                    lot = info["result"]["list"][0]["lotSizeFilter"]
                    qty_step = lot["basePrecision"]
                    min_qty = lot["minOrderQty"]

                    # Отримуємо поточну ціну (last price)
                    ticker = session.get_tickers(category="spot", symbol=sym)
                    if ticker.get("retCode") != 0:
                        raise RuntimeError(f"get_tickers: {ticker.get('retMsg')}")
                    last_price = float(ticker["result"]["list"][0]["lastPrice"])

                    # Розрахунок qty
                    raw_qty = size_usd / last_price
                    from decimal import Decimal, ROUND_DOWN
                    step = Decimal(qty_step)
                    qty = (Decimal(str(raw_qty)) / step).quantize(Decimal('1'), rounding=ROUND_DOWN) * step
                    qty_str = str(qty.normalize())

                    if float(qty_str) < float(min_qty):
                        raise RuntimeError(f"qty {qty_str} < min {min_qty}")

                    side = "Buy" if trade_direction == "long" else "Sell"
                    resp = session.place_order(
                        category="spot",
                        symbol=sym,
                        side=side,
                        orderType="Market",
                        qty=qty_str,
                    )
                    if resp.get("retCode") != 0:
                        raise RuntimeError(f"place_order: {resp.get('retMsg')}")

                    order_id = resp["result"].get("orderId", "")
                    # Отримуємо середню ціну виконання (може бути одразу)
                    try:
                        order_info = session.get_order_history(
                            category="spot", orderId=order_id
                        )
                        if order_info.get("retCode") == 0 and order_info["result"]["list"]:
                            o = order_info["result"]["list"][0]
                            avg_price = float(o.get("avgPrice", 0)) or last_price
                            await db.execute("""
                                UPDATE live_trades
                                SET status='FILLED', bybit_order_id=?, entry_price=?, entry_ts=?
                                WHERE event_group_key=? AND symbol=?
                            """, (order_id, avg_price, now_ms, gk, sym))
                        else:
                            await db.execute("""
                                UPDATE live_trades
                                SET status='FILLED', bybit_order_id=?, entry_price=?, entry_ts=?
                                WHERE event_group_key=? AND symbol=?
                            """, (order_id, last_price, now_ms, gk, sym))
                    except Exception:
                        await db.execute("""
                            UPDATE live_trades
                            SET status='FILLED', bybit_order_id=?, entry_price=?, entry_ts=?
                            WHERE event_group_key=? AND symbol=?
                        """, (order_id, last_price, now_ms, gk, sym))

                    await db.commit()
                    logger.info(
                        f"[LIVE FILLED] {side} {sym} qty={qty_str} @ ~${last_price:.4f} "
                        f"(order={order_id})"
                    )
                    stats["opened"] += 1

                except Exception as ex:
                    logger.exception(f"[LIVE] Order failed for {sym}: {ex}")
                    await db.execute("""
                        UPDATE live_trades SET status='REJECTED', error_message=?
                        WHERE event_group_key=? AND symbol=?
                    """, (str(ex), gk, sym))
                    await db.commit()
                    stats["errors"] += 1

    return stats


async def get_live_trades(limit: int = 200) -> list[dict]:
    async with get_db() as db:
        cur = await db.execute("""
            SELECT * FROM live_trades
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def toggle_live(enabled: bool) -> dict:
    """
    Увімкнути/вимкнути LIVE.
    Пише в settings.live_enabled (в пам'яті; для постійного — треба .env).
    Повертає поточний стан.
    """
    if enabled:
        raise ValueError("LIVE trading is locked pending execution/reconciliation audit")
    settings.live_enabled = False
    logger.warning(f"[LIVE] LIVE_ENABLED = {enabled}")
    return {"live_enabled": enabled}


async def update_live_size(size_usd: float) -> dict:
    """Оновити розмір угоди для LIVE (в пам'яті)."""
    settings.live_trade_size_usd = size_usd
    logger.info(f"[LIVE] trade_size_usd = {size_usd}")
    return {"live_trade_size_usd": size_usd}


async def clear_live() -> dict:
    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*) AS n FROM live_trades")
        n = (await cur.fetchone())["n"]
        await db.execute("DELETE FROM live_trades")
        await db.commit()
    return {"deleted": n}