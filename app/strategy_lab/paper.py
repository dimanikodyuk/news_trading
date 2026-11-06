"""Paper-only forward simulator. Uses confirmed 1-minute candles; never sends orders."""
import asyncio
import time
from datetime import datetime, timezone
from app.db import get_db
from app.strategy_lab.engine import signal

SYMBOLS=('BTCUSDT','ETHUSDT','SOLUSDT')
STRATEGIES=('momentum','reversal','news_surprise')
FEE_BPS=10.0
SLIPPAGE_BPS=5.0
SIZE_USD=10.0
HOLD_MIN=15
ENTRY_MIN=2
_lock=asyncio.Lock()

SCHEMA="""
CREATE TABLE IF NOT EXISTS strategy_paper_accounts (
 strategy TEXT PRIMARY KEY, initial_balance REAL NOT NULL DEFAULT 100,
 balance REAL NOT NULL DEFAULT 100, enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS strategy_paper_trades (
 id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT NOT NULL, event_id INTEGER NOT NULL,
 symbol TEXT NOT NULL, direction TEXT NOT NULL, entry_ts INTEGER NOT NULL,
 entry_price REAL NOT NULL, exit_ts INTEGER, exit_price REAL, size_usd REAL NOT NULL,
 pnl REAL, status TEXT NOT NULL DEFAULT 'OPEN', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(strategy,event_id,symbol)
);
CREATE INDEX IF NOT EXISTS idx_strategy_paper_open ON strategy_paper_trades(status,entry_ts);
"""

async def init_strategy_paper():
 async with get_db() as db:
  await db.executescript(SCHEMA)
  for name in STRATEGIES:
   await db.execute('INSERT OR IGNORE INTO strategy_paper_accounts(strategy,initial_balance,balance,enabled) VALUES (?,?,?,?)',(name,100.,100.,0 if name=='news_surprise' else 1))
  # News Surprise is intentionally disabled: Actual value publication timestamps are unverified.
  await db.execute("UPDATE strategy_paper_accounts SET enabled=0 WHERE strategy='news_surprise'")
  await db.commit()

async def run_strategy_paper():
 """Only process events close to their release time; no historical trade creation."""
 if _lock.locked(): return {'status':'busy'}
 async with _lock:
  now=int(time.time()*1000); opened=closed=0
  async with get_db() as db:
   # Close using last CONFIRMED candle available at or after the intended exit time.
   cur=await db.execute("SELECT * FROM strategy_paper_trades WHERE status='OPEN'")
   for t in await cur.fetchall():
    target=t['entry_ts']+HOLD_MIN*60000
    if now<target+60000: continue
    c=await db.execute('SELECT close,ts FROM prices WHERE symbol=? AND interval=? AND confirm=1 AND ts>=? AND ts<=? ORDER BY ts LIMIT 1',(t['symbol'],'1',target,now-60000))
    bar=await c.fetchone()
    if not bar: continue
    side=1 if t['direction']=='long' else -1
    slip=SLIPPAGE_BPS/10000; fee=FEE_BPS/10000
    exit_price=bar['close']*(1-slip*side)
    qty=t['size_usd']/t['entry_price']
    pnl=qty*(exit_price-t['entry_price'])*side - (t['size_usd']+qty*exit_price)*fee
    await db.execute("UPDATE strategy_paper_trades SET status='CLOSED',exit_ts=?,exit_price=?,pnl=? WHERE id=?",(bar['ts']+60000,exit_price,pnl,t['id']))
    await db.execute('UPDATE strategy_paper_accounts SET balance=balance+? WHERE strategy=?',(pnl,t['strategy']))
    closed+=1
   # Avoid retroactive signals: only recent events, at most 5 minutes old.
   cur=await db.execute("SELECT id,title,time_utc,forecast_value,actual_value FROM events WHERE importance='high' ORDER BY time_utc DESC LIMIT 100")
   events=await cur.fetchall()
   for ev in events:
    try:
     dt=datetime.fromisoformat(ev['time_utc'].replace('Z','+00:00'))
     if dt.tzinfo is None: continue
     t=int(dt.timestamp()*1000)
    except (ValueError,TypeError): continue
    if not (ENTRY_MIN*60000<=now-t<=5*60000) or t%60000: continue
    for symbol in SYMBOLS:
     c=await db.execute('SELECT ts,open,close,confirm FROM prices WHERE symbol=? AND interval=? AND ts>=? AND ts<=? AND confirm=1 ORDER BY ts',(symbol,'1',t-60000,now-60000))
     candles={int((r['ts']-t)//60000):dict(r) for r in await c.fetchall() if (r['ts']-t)%60000==0}
     if any(i not in candles for i in (-1,0,1)): continue
     # Execution at the close of the most recently completed bar; never use a future open.
     recent=max((r for r in candles.values() if r['ts']+60000<=now),key=lambda r:r['ts'],default=None)
     if not recent or recent['close']<=0: continue
     for strategy in ('momentum','reversal'):
      a=await db.execute('SELECT enabled,balance FROM strategy_paper_accounts WHERE strategy=?',(strategy,))
      account=await a.fetchone()
      if not account or not account['enabled'] or account['balance']<SIZE_USD: continue
      direction=signal(strategy,ev,candles,ENTRY_MIN)
      if direction!='long': continue # Spot simulation: no unbacked short sales.
      check=await db.execute('SELECT 1 FROM strategy_paper_trades WHERE strategy=? AND event_id=? AND symbol=?',(strategy,ev['id'],symbol))
      if await check.fetchone(): continue
      # One concurrent position per strategy/symbol, avoiding duplicate correlated releases.
      check=await db.execute("SELECT 1 FROM strategy_paper_trades WHERE strategy=? AND symbol=? AND status='OPEN'",(strategy,symbol))
      if await check.fetchone(): continue
      entry=recent['close']*(1+SLIPPAGE_BPS/10000)
      result=await db.execute('INSERT OR IGNORE INTO strategy_paper_trades(strategy,event_id,symbol,direction,entry_ts,entry_price,size_usd) VALUES (?,?,?,?,?,?,?)',(strategy,ev['id'],symbol,direction,recent['ts']+60000,entry,SIZE_USD))
      if result.rowcount: opened+=1
   await db.commit()
  return {'opened':opened,'closed':closed,'news_surprise':'disabled_unverified_actual_release_time'}

async def strategy_paper_summary():
 async with get_db() as db:
  cur=await db.execute('SELECT * FROM strategy_paper_accounts ORDER BY strategy')
  accounts=[dict(r) for r in await cur.fetchall()]
  cur=await db.execute('SELECT strategy,status,COUNT(*) AS trades,COALESCE(SUM(pnl),0) AS pnl FROM strategy_paper_trades GROUP BY strategy,status')
  stats=[dict(r) for r in await cur.fetchall()]
  cur=await db.execute('SELECT * FROM strategy_paper_trades ORDER BY id DESC LIMIT 100')
  trades=[dict(r) for r in await cur.fetchall()]
 return {'accounts':accounts,'stats':stats,'trades':trades,'live_execution':False,'notes':'News Surprise disabled until release timestamp verification; no retroactive paper trades.'}
