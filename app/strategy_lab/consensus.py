"""Forward-only paper news consensus. No exchange orders, no historical signal backfill."""
import asyncio
import json
import time
from datetime import datetime, timezone
from app.db import get_db
from app.impact.expected import RULES, _parse_number

SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
MODES = ('forecast', 'actual')
WINDOW_MS = 5 * 60_000
GRACE_MS = 30 * 60_000
HOLD_MS = 15 * 60_000
SIZE_USD = 10.0
FEE = 0.001
SLIP = 0.0005
_lock = asyncio.Lock()

SCHEMA = '''
CREATE TABLE IF NOT EXISTS consensus_accounts (
 mode TEXT PRIMARY KEY, initial_balance REAL NOT NULL DEFAULT 100,
 balance REAL NOT NULL DEFAULT 100, enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS consensus_trades (
 id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, group_key TEXT NOT NULL,
 event_ids TEXT NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL,
 votes_up INTEGER NOT NULL, votes_down INTEGER NOT NULL,
 entry_ts INTEGER NOT NULL, entry_price REAL NOT NULL, size_usd REAL NOT NULL,
 exit_ts INTEGER, exit_price REAL, pnl REAL, status TEXT NOT NULL DEFAULT 'OPEN',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(mode,group_key,symbol)
);
CREATE INDEX IF NOT EXISTS idx_consensus_open ON consensus_trades(status,entry_ts);
CREATE TABLE IF NOT EXISTS consensus_actual_seen (
 event_id INTEGER PRIMARY KEY, first_seen_ms INTEGER NOT NULL,
 actual_snapshot TEXT NOT NULL, forecast_snapshot TEXT
);
CREATE TABLE IF NOT EXISTS consensus_decisions (
 mode TEXT NOT NULL, group_key TEXT NOT NULL, symbol TEXT NOT NULL,
 status TEXT NOT NULL, reason TEXT NOT NULL, votes_up INTEGER NOT NULL DEFAULT 0,
 votes_down INTEGER NOT NULL DEFAULT 0, checked_at_ms INTEGER NOT NULL,
 PRIMARY KEY(mode,group_key,symbol)
);
'''

def ms(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp()*1000)
    except (ValueError, TypeError):
        return None

def direction(title, a, b):
    """Hypothesis: a compared to b, first matching macro rule; crypto direction is uncertain."""
    x, y = _parse_number(a), _parse_number(b)
    if x is None or y is None or x == y: return None
    title = (title or '').lower()
    for rule in RULES:
        if any(p in title for p in rule['pattern']):
            return rule['higher'] if x > y else rule['lower']
    return None

def vote(ev, mode, snapshots):
    if mode == 'forecast':
        # Expected direction: forecast relative to previous, NOT actual surprise.
        return direction(ev['title'], ev['forecast_value'], ev['previous_value'])
    snap = snapshots.get(ev['id'])
    if not snap: return None
    return direction(ev['title'], snap['actual_snapshot'], snap['forecast_snapshot'])

async def init():
    async with get_db() as db:
        await db.executescript(SCHEMA)
        for mode in MODES:
            await db.execute('INSERT OR IGNORE INTO consensus_accounts(mode,initial_balance,balance) VALUES (?,?,?)', (mode,100.,100.))
        await db.commit()

async def observe_actuals(db, now):
    """Record first time actual values are OBSERVED, never infer release timestamp from event time."""
    cur = await db.execute('''SELECT id,actual_value,forecast_value FROM events
        WHERE actual_value IS NOT NULL AND TRIM(actual_value) != ''
          AND datetime(time_utc) >= datetime(?)''', (datetime.fromtimestamp((now-2*86400000)/1000,timezone.utc).isoformat(),))
    rows = await cur.fetchall()
    for r in rows:
        await db.execute('''INSERT OR IGNORE INTO consensus_actual_seen
          (event_id,first_seen_ms,actual_snapshot,forecast_snapshot) VALUES (?,?,?,?)''',
          (r['id'],now,r['actual_value'],r['forecast_value']))

async def run():
    if _lock.locked(): return {'status':'busy'}
    async with _lock:
        now = int(time.time()*1000)
        opened=closed=0
        reasons={}
        async with get_db() as db:
            await observe_actuals(db,now)
            # Exit at first confirmed bar at or after target, never use future candles.
            cur=await db.execute("SELECT * FROM consensus_trades WHERE status='OPEN'")
            for trade in await cur.fetchall():
                target=trade['entry_ts']+HOLD_MS
                if now < target+60000: continue
                cur2=await db.execute('''SELECT ts,close FROM prices WHERE symbol=? AND interval='1'
                  AND confirm=1 AND ts>=? AND ts<=? ORDER BY ts LIMIT 1''',
                  (trade['symbol'],target,now-60000))
                bar=await cur2.fetchone()
                if not bar or not bar['close'] or bar['close']<=0: continue
                sign=1 if trade['direction']=='long' else -1
                price=bar['close']*(1-SLIP*sign)
                qty=trade['size_usd']/trade['entry_price']
                pnl=qty*(price-trade['entry_price'])*sign - FEE*(trade['size_usd']+qty*price)
                await db.execute('''UPDATE consensus_trades SET status='CLOSED',exit_ts=?,exit_price=?,pnl=? WHERE id=?''',
                                 (bar['ts']+60000,price,pnl,trade['id']))
                await db.execute('UPDATE consensus_accounts SET balance=balance+? WHERE mode=?',(pnl,trade['mode']))
                closed+=1
            # Group by 5-min release windows. Only consider groups that are past and recent.
            start=datetime.fromtimestamp((now-GRACE_MS-WINDOW_MS)/1000,timezone.utc).isoformat()
            end=datetime.fromtimestamp(now/1000,timezone.utc).isoformat()
            cur=await db.execute('''SELECT id,title,country,importance,time_utc,forecast_value,previous_value,actual_value
              FROM events WHERE datetime(time_utc) BETWEEN datetime(?) AND datetime(?) AND importance IN ('high','medium')
              ORDER BY time_utc,id''',(start,end))
            groups={}
            for r in await cur.fetchall():
                t=ms(r['time_utc'])
                if t is None: continue
                key=str(t//WINDOW_MS)
                groups.setdefault(key,[]).append(dict(r))
            cur=await db.execute('SELECT * FROM consensus_actual_seen')
            snapshots={r['event_id']:dict(r) for r in await cur.fetchall()}
            for key,group in groups.items():
                group_end=(int(key)+1)*WINDOW_MS
                if now < group_end+60000 or now > group_end+GRACE_MS: continue
                for mode in MODES:
                    cur=await db.execute('SELECT enabled,balance FROM consensus_accounts WHERE mode=?',(mode,))
                    acc=await cur.fetchone()
                    if not acc or not acc['enabled']: continue
                    # Actual data must have been observed after release and before entry.
                    eligible=[e for e in group if mode=='forecast' or
                        (e['id'] in snapshots and snapshots[e['id']]['first_seen_ms'] >= ms(e['time_utc']) and
                         snapshots[e['id']]['first_seen_ms'] <= now and
                         snapshots[e['id']]['first_seen_ms'] <= ms(e['time_utc']) + 10*60000)]
                    dirs=[vote(e,mode,snapshots) for e in eligible]
                    up=dirs.count('up'); down=dirs.count('down')
                    for symbol in SYMBOLS:
                        cur=await db.execute('SELECT 1 FROM consensus_trades WHERE mode=? AND group_key=? AND symbol=?',(mode,key,symbol))
                        if await cur.fetchone(): continue
                        if up==down: reason='tie_or_no_direction'
                        elif acc['balance'] < SIZE_USD: reason='insufficient_balance'
                        else:
                            cur=await db.execute('''SELECT 1 FROM consensus_trades WHERE mode=? AND symbol=? AND status='OPEN' ''',(mode,symbol))
                            if await cur.fetchone(): reason='position_already_open'
                            else:
                                # Last fully confirmed bar only; do not enter at historic release price.
                                cur=await db.execute('''SELECT ts,close FROM prices WHERE symbol=? AND interval='1'
                                  AND confirm=1 AND ts<=? ORDER BY ts DESC LIMIT 1''',(symbol,now-60000))
                                bar=await cur.fetchone()
                                if not bar or not bar['close'] or bar['close']<=0: reason='no_confirmed_price'
                                elif now-(bar['ts']+60000)>180000: reason='stale_price'
                                else:
                                    sign=1 if up>down else -1
                                    entry=bar['close']*(1+SLIP*sign)
                                    await db.execute('''INSERT OR IGNORE INTO consensus_trades
                                      (mode,group_key,event_ids,symbol,direction,votes_up,votes_down,entry_ts,entry_price,size_usd)
                                      VALUES (?,?,?,?,?,?,?,?,?,?)''',
                                      (mode,key,json.dumps([e['id'] for e in eligible]),symbol,
                                       'long' if sign==1 else 'short',up,down,bar['ts']+60000,entry,SIZE_USD))
                                    opened+=1
                                    reason='opened'
                        reasons[reason]=reasons.get(reason,0)+1
                        await db.execute('''INSERT INTO consensus_decisions(mode,group_key,symbol,status,reason,votes_up,votes_down,checked_at_ms)
                          VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(mode,group_key,symbol) DO UPDATE SET
                          status=excluded.status,reason=excluded.reason,votes_up=excluded.votes_up,
                          votes_down=excluded.votes_down,checked_at_ms=excluded.checked_at_ms''',
                          (mode,key,symbol,'OPENED' if reason=='opened' else 'SKIPPED',reason,up,down,now))
            await db.commit()
        return {'opened':opened,'closed':closed,'reasons':reasons,'paper_only':True}

async def summary():
    async with get_db() as db:
        accounts=[dict(r) for r in await (await db.execute('SELECT * FROM consensus_accounts ORDER BY mode')).fetchall()]
        trades=[dict(r) for r in await (await db.execute('SELECT * FROM consensus_trades ORDER BY id DESC LIMIT 150')).fetchall()]
        decisions=[dict(r) for r in await (await db.execute('SELECT * FROM consensus_decisions ORDER BY checked_at_ms DESC LIMIT 80')).fetchall()]
        event_count=(await (await db.execute('SELECT COUNT(*) AS n FROM events')).fetchone())['n']
        price_count=(await (await db.execute('SELECT COUNT(*) AS n FROM prices')).fetchone())['n']
    return {'accounts':accounts,'trades':trades,'decisions':decisions,'health':{'events':event_count,'prices':price_count},
            'paper_only':True,'notes':'Forecast: forecast vs previous. Actual: first observed actual vs forecast; no historical backfill.'}

async def research_backtest(mode='forecast', symbol='BTCUSDT', limit_groups=200):
    """Exploratory historical replay. Stored forecasts/actuals lack as-of timestamps.

    It must NOT be interpreted as a causally verified walk-forward backtest.
    """
    if mode not in MODES or symbol not in SYMBOLS or not 1 <= limit_groups <= 1000:
        raise ValueError('invalid mode/symbol/limit')
    async with get_db() as db:
        rows=await (await db.execute('''SELECT id,title,time_utc,forecast_value,previous_value,actual_value
          FROM events WHERE importance IN ('high','medium') ORDER BY time_utc DESC LIMIT 2000''')).fetchall()
        groups={}
        for r in rows:
            t=ms(r['time_utc'])
            if t is not None: groups.setdefault(t//WINDOW_MS,[]).append(dict(r))
        trades=[]; skipped={}
        for key, group in sorted(groups.items(),reverse=True)[:limit_groups]:
            votes=[]
            for ev in group:
                if mode=='forecast': votes.append(direction(ev['title'],ev['forecast_value'],ev['previous_value']))
                else: votes.append(direction(ev['title'],ev['actual_value'],ev['forecast_value']))
            up=votes.count('up'); down=votes.count('down')
            if up==down:
                skipped['tie_or_no_direction']=skipped.get('tie_or_no_direction',0)+1;continue
            # The first bar after the 5-min cluster closes, avoiding prices within the news group.
            entry_t=(key+1)*WINDOW_MS+60000
            exit_t=entry_t+HOLD_MS
            cur=await db.execute('''SELECT ts,open FROM prices WHERE symbol=? AND interval='1'
              AND confirm=1 AND ts IN (?,?) ORDER BY ts''',(symbol,entry_t,exit_t))
            bars={r['ts']:r['open'] for r in await cur.fetchall()}
            if len(bars)!=2 or min(bars.values())<=0:
                skipped['missing_prices']=skipped.get('missing_prices',0)+1;continue
            sign=1 if up>down else -1
            entry=bars[entry_t]*(1+SLIP*sign);exit_=bars[exit_t]*(1-SLIP*sign)
            qty=SIZE_USD/entry
            pnl=qty*(exit_-entry)*sign-FEE*(SIZE_USD+qty*exit_)
            trades.append({'group_key':str(key),'symbol':symbol,'mode':mode,'direction':'long' if sign==1 else 'short',
                           'votes_up':up,'votes_down':down,'entry_ts':entry_t,'exit_ts':exit_t,'pnl':round(pnl,5)})
    return {'mode':mode,'symbol':symbol,'exploratory_only':True,'causal_validation':False,
            'warning':'Stored macro values lack point-in-time snapshots; actual mode is oracle-biased.',
            'n':len(trades),'wins':sum(t['pnl']>0 for t in trades),'pnl':round(sum(t['pnl'] for t in trades),5),
            'skipped':skipped,'trades':trades}
