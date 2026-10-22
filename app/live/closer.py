"""
Автоматичне закриття LIVE-позицій на Bybit Spot.

Логіка:
1. Кожну хвилину scheduler викликає `process_live_closer()`.
2. Знаходить усі `live_trades` зі статусом `FILLED`.
3. Для кожної:
   - Перевіряє stop-loss: якщо ціна впала > stop_loss_pct від entry (для LONG)
     або виросла (для SHORT) — закриває примусово.
   - Перевіряє T+exit_offset_min: якщо настав — закриває планово.
4. Відправляє Market ордер протилежного напрямку (LONG → Sell, SHORT → Buy).
5. Записує exit_price, pnl, pnl_pct, closed_at, статус → CLOSED.
6. При помилці — 3 спроби (close_attempts). Після 3 — статус CLOSE_FAILED.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from pybit.unified_trading import HTTP

from app.config import settings
from app.db import get_db
from app.bot.notifier import _send as _notify

logger = logging.getLogger(__name__)

COMMISSION_PCT = 0.1
SLIPPAGE_PCT = 0.05
MAX_CLOSE_ATTEMPTS = 3


def _get_session() -> Optional[HTTP]:
    if not settings.bybit_api_key or not settings.bybit_api_secret:
        return None
    return HTTP(
        testnet=settings.bybit_testnet,
        api_key=settings.bybit_api_key,
        api_secret=settings.bybit_api_secret,
    )


def _iso_to_ms(iso_str: str) -> Optional[int]:
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _safe_float(v, default=0.0):
    if v is None:
        return default
    try:
        s = str(v).strip()
        if not s:
            return default
        return float(s)
    except (ValueError, TypeError):
        return default


async def _get_current_price(session: HTTP, symbol: str) -> Optional[float]:
    try:
        ticker = session.get_tickers(category="spot", symbol=symbol)
        if ticker.get("retCode") != 0:
            return None
        return _safe_float(ticker["result"]["list"][0]["lastPrice"])
    except Exception as ex:
        logger.warning(f"[closer] get_tickers {symbol}: {ex}")
        return None


async def _close_position(
    db,
    trade,
    session: HTTP,
    reason: str,
) -> bool:
    """
    Закриває одну позицію. Повертає True якщо успішно.
    """
    trade_id = trade["id"]
    symbol = trade["symbol"]
    direction = trade["direction"]
    entry_price = _safe_float(trade["entry_price"])
    size_usd = _safe_float(trade["size_usd"])
    entry_ts = trade["entry_ts"] or 0

    if entry_price <= 0 or size_usd <= 0:
        logger.warning(f"[closer] trade {trade_id}: invalid entry_price/size")
        await db.execute("""
            UPDATE live_trades SET status='CLOSE_FAILED',
                   error_message='invalid entry_price/size'
            WHERE id=?
        """, (trade_id,))
        await db.commit()
        return False

    # Отримуємо поточну ціну
    current_price = await _get_current_price(session, symbol)
    if current_price is None:
        logger.warning(f"[closer] trade {trade_id}: no price for {symbol}")
        await _bump_attempts(db, trade_id, "no_price")
        return False

    # Скільки монет у нас є (qty)
    qty = size_usd / entry_price

    # Округлення qty до basePrecision
    try:
        info = session.get_instruments_info(category="spot", symbol=symbol)
        if info.get("retCode") != 0:
            raise RuntimeError(info.get("retMsg", "get_instruments_info failed"))
        lot = info["result"]["list"][0]["lotSizeFilter"]
        qty_step = lot["basePrecision"]
        min_qty = lot["minOrderQty"]

        from decimal import Decimal, ROUND_DOWN
        step = Decimal(qty_step)
        qty_dec = (Decimal(str(qty)) / step).quantize(Decimal('1'), rounding=ROUND_DOWN) * step
        qty_str = str(qty_dec.normalize())

        if float(qty_str) < float(min_qty):
            # Замало для мінімального ордера — закриваємо вручну в БД
            logger.warning(f"[closer] trade {trade_id}: qty {qty_str} < min {min_qty}, закриваю в БД")
            pnl = (current_price - entry_price) * qty
            if direction == "short":
                pnl = -pnl
            # Комісія приблизно
            pnl_pct = (pnl / size_usd * 100) if size_usd > 0 else 0
            await db.execute("""
                UPDATE live_trades
                SET exit_price=?, exit_ts=?, pnl=?, pnl_pct=?,
                    status='CLOSED', closed_at=CURRENT_TIMESTAMP,
                    error_message=?
                WHERE id=?
            """, (current_price, int(datetime.now(timezone.utc).timestamp() * 1000),
                  pnl, pnl_pct, f"qty<min ({qty_str})", trade_id))
            await db.commit()
            return True
    except Exception as ex:
        logger.warning(f"[closer] trade {trade_id}: qty calculation failed: {ex}")
        await _bump_attempts(db, trade_id, f"qty_calc: {ex}")
        return False

    # Відправляємо протилежний ордер
    side = "Sell" if direction == "long" else "Buy"

    try:
        resp = session.place_order(
            category="spot",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=qty_str,
        )
        if resp.get("retCode") != 0:
            raise RuntimeError(f"place_order: {resp.get('retMsg')}")

        order_id = resp["result"].get("orderId", "")

        # Отримуємо середню ціну виконання
        exit_price = current_price
        try:
            order_info = session.get_order_history(category="spot", orderId=order_id)
            if order_info.get("retCode") == 0 and order_info["result"]["list"]:
                o = order_info["result"]["list"][0]
                avg = _safe_float(o.get("avgPrice"))
                if avg > 0:
                    exit_price = avg
        except Exception:
            pass

        # Розрахунок P&L
        if direction == "long":
            gross = (exit_price - entry_price) * qty
        else:
            gross = (entry_price - exit_price) * qty

        # Комісії: entry + exit (приблизно 0.1% * 2)
        fee = size_usd * (COMMISSION_PCT / 100) * 2
        pnl = gross - fee
        pnl_pct = (pnl / size_usd * 100) if size_usd > 0 else 0

        await db.execute("""
            UPDATE live_trades
            SET exit_price=?, exit_ts=?, pnl=?, pnl_pct=?,
                status='CLOSED', closed_at=CURRENT_TIMESTAMP,
                error_message=?
            WHERE id=?
        """, (exit_price, int(datetime.now(timezone.utc).timestamp() * 1000),
              pnl, pnl_pct, reason, trade_id))
        await db.commit()

        # Сповіщення
        emoji = "✅" if pnl > 0 else "❌"
        sign = "+" if pnl >= 0 else ""
        text = (
            f"{emoji} *Закрито LIVE {direction.upper()} {symbol}*\n"
            f"Причина: {reason}\n"
            f"Вхід: ${entry_price:.4f} → Вихід: ${exit_price:.4f}\n"
            f"Розмір: ${size_usd:.2f}\n"
            f"*P&L: {sign}${pnl:.4f} ({sign}{pnl_pct:.3f}%)*"
        )
        await _notify(text)

        logger.info(
            f"[closer] trade {trade_id} CLOSED: {direction} {symbol} "
            f"entry={entry_price:.4f} exit={exit_price:.4f} "
            f"pnl={pnl:.4f} ({pnl_pct:.3f}%) reason={reason}"
        )
        return True

    except Exception as ex:
        logger.exception(f"[closer] trade {trade_id}: close failed: {ex}")
        await _bump_attempts(db, trade_id, str(ex))
        return False


async def _bump_attempts(db, trade_id: int, err_msg: str):
    """Збільшує close_attempts і, якщо >= MAX, ставить CLOSE_FAILED."""
    cur = await db.execute(
        "SELECT close_attempts FROM live_trades WHERE id=?", (trade_id,)
    )
    row = await cur.fetchone()
    attempts = (row["close_attempts"] or 0) + 1 if row else 1
    new_status = "CLOSE_FAILED" if attempts >= MAX_CLOSE_ATTEMPTS else "FILLED"
    await db.execute("""
        UPDATE live_trades
        SET close_attempts=?, status=?, error_message=?
        WHERE id=?
    """, (attempts, new_status, err_msg, trade_id))
    await db.commit()


async def process_live_closer(manual: bool = False) -> dict:
    """
    Головна функція. Викликається scheduler кожну хвилину.
    manual=True — закрити всі FILLED незалежно від T+60 і stop-loss
    (для кнопки "Закрити всі").
    """
    stats = {"processed": 0, "closed": 0, "skipped": 0, "errors": 0}

    if not settings.live_auto_close and not manual:
        return stats

    session = _get_session()
    if session is None:
        logger.warning("[closer] no Bybit session")
        return stats

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    async with get_db() as db:
        cur = await db.execute("""
            SELECT * FROM live_trades
            WHERE status = 'FILLED'
            ORDER BY created_at ASC
        """)
        trades = await cur.fetchall()

        for t in trades:
            stats["processed"] += 1
            trade_id = t["id"]
            symbol = t["symbol"]
            direction = t["direction"]
            entry_price = _safe_float(t["entry_price"])

            if entry_price <= 0:
                stats["skipped"] += 1
                continue

            event_ts = _iso_to_ms(t["event_time_utc"])
            if event_ts is None:
                stats["skipped"] += 1
                continue

            # T+exit_offset_min
            exit_ts_target = event_ts + settings.live_exit_offset_min * 60_000

            # Перевірка stop-loss
            current_price = await _get_current_price(session, symbol)
            stop_loss_hit = False
            if current_price is not None and settings.live_stop_loss_pct > 0:
                if direction == "long":
                    loss_pct = (entry_price - current_price) / entry_price * 100
                else:
                    loss_pct = (current_price - entry_price) / entry_price * 100
                if loss_pct >= settings.live_stop_loss_pct:
                    stop_loss_hit = True
                    logger.info(
                        f"[closer] trade {trade_id}: STOP-LOSS hit "
                        f"loss={loss_pct:.2f}% >= {settings.live_stop_loss_pct}%"
                    )

            # Чи треба закривати?
            if manual:
                reason = "manual_close"
            elif stop_loss_hit:
                reason = f"stop_loss ({settings.live_stop_loss_pct}%)"
            elif now_ms >= exit_ts_target:
                reason = f"T+{settings.live_exit_offset_min}min"
            else:
                stats["skipped"] += 1
                continue

            ok = await _close_position(db, t, session, reason)
            if ok:
                stats["closed"] += 1
            else:
                stats["errors"] += 1

    if stats["closed"] or stats["errors"]:
        logger.info(f"[closer] result: {stats}")
    return stats


async def get_live_equity_curve() -> list[dict]:
    """Кумулятивний P&L по закритих live_trades."""
    async with get_db() as db:
        cur = await db.execute("""
            SELECT id, closed_at, pnl
            FROM live_trades
            WHERE status = 'CLOSED' AND pnl IS NOT NULL
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