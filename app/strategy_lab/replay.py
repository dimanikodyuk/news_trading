"""Historical exploratory News Consensus replay; isolated from all live/paper accounts."""
import asyncio
import json
from datetime import datetime, timezone
from app.db import get_db
from app.strategy_lab.consensus import direction, ms, SYMBOLS, MODES, WINDOW_MS, HOLD_MS, SIZE_USD, FEE, SLIP

_lock = asyncio.Lock()
DDL = '''
CREATE TABLE IF NOT EXISTS consensus_replay_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 settings_json TEXT NOT NULL, summary_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS consensus_replay_trades (
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES consensus_replay_runs(id),
 mode TEXT NOT NULL, symbol TEXT NOT NULL, group_key TEXT NOT NULL, event_ids TEXT NOT NULL,
 direction TEXT NOT NULL, votes_up INTEGER NOT NULL, votes_down INTEGER NOT NULL,
 entry_ts INTEGER NOT NULL, exit_ts INTEGER NOT NULL, entry_price REAL NOT NULL,
 exit_price REAL NOT NULL, size_usd REAL NOT NULL, pnl REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_consensus_replay_run ON consensus_replay_trades(run_id,mode,symbol);
'''

async def init():
    async with get_db() as db:
        await db.executescript(DDL)
        await db.commit()

async def latest():
    await init()
    async with get_db() as db:
        r = await (await db.execute('SELECT * FROM consensus_replay_runs ORDER BY id DESC LIMIT 1')).fetchone()
        if not r: return {'run':None,'trades':[]}
        trades = [dict(x) for x in await (await db.execute('SELECT * FROM consensus_replay_trades WHERE run_id=? ORDER BY entry_ts DESC LIMIT 500',(r['id'],))).fetchall()]
        return {'run':{'id':r['id'],'created_at':r['created_at'],'settings':json.loads(r['settings_json']),'summary':json.loads(r['summary_json'])},'trades':trades}

async def run():
    if _lock.locked(): return {'error':'replay already running'}
    async with _lock:
        await init()
        settings={'period':'all_available','hold_minutes':15,'min_consensus':'strict_majority','symbols':list(SYMBOLS),'modes':list(MODES),'initial_balance_usd':100,'trade_size_usd':SIZE_USD,'fee_per_side':FEE,'slippage_per_side':SLIP}
        async with get_db() as db:
            events=[dict(r) for r in await (await db.execute("SELECT id,title,time_utc,forecast_value,previous_value,actual_value FROM events WHERE lower(importance) IN ('high','medium') ORDER BY time_utc,id")).fetchall()]
            groups={}
            for ev in events:
                t=ms(ev['time_utc'])
                if t is not None: groups.setdefault(t//WINDOW_MS,[]).append(ev)
            stats={}; records=[]
            for mode in MODES:
                for symbol in SYMBOLS:
                    balance=100.; equity_peak=100.; max_dd=0.; wins=0; losses=0; skipped={}; last_exit=-1
                    for key,group in sorted(groups.items()):
                        votes=[direction(e['title'],e['forecast_value'],e['previous_value']) if mode=='forecast' else direction(e['title'],e['actual_value'],e['forecast_value']) for e in group]
                        up=votes.count('up'); down=votes.count('down')
                        if up==down:
                            reason='tie_or_no_direction'
                        else:
                            entry_target=(key+1)*WINDOW_MS+60000
                            exit_target=entry_target+HOLD_MS
                            if entry_target<last_exit: reason='position_overlap'
                            else:
                                entry_bar=await (await db.execute("SELECT ts,open FROM prices WHERE symbol=? AND interval='1' AND confirm=1 AND ts>=? AND ts<=? ORDER BY ts LIMIT 1",(symbol,entry_target,entry_target+120000))).fetchone()
                                exit_bar=await (await db.execute("SELECT ts,open FROM prices WHERE symbol=? AND interval='1' AND confirm=1 AND ts>=? AND ts<=? ORDER BY ts LIMIT 1",(symbol,exit_target,exit_target+120000))).fetchone()
                                if not entry_bar or not exit_bar or not entry_bar['open'] or not exit_bar['open'] or entry_bar['open']<=0 or exit_bar['open']<=0: reason='missing_confirmed_prices'
                                elif entry_bar['ts']>=exit_bar['ts']: reason='invalid_bar_order'
                                else:
                                    sign=1 if up>down else -1
                                    entry=float(entry_bar['open'])*(1+SLIP*sign)
                                    exit_price=float(exit_bar['open'])*(1-SLIP*sign)
                                    qty=SIZE_USD/entry
                                    pnl=qty*(exit_price-entry)*sign-FEE*(SIZE_USD+qty*exit_price)
                                    balance+=pnl; equity_peak=max(equity_peak,balance)
                                    max_dd=max(max_dd, (equity_peak-balance)/equity_peak*100)
                                    wins+=pnl>0; losses+=pnl<=0
                                    last_exit=int(exit_bar['ts'])
                                    records.append((mode,symbol,str(key),json.dumps([e['id'] for e in group]),'long' if sign==1 else 'short',up,down,int(entry_bar['ts']),int(exit_bar['ts']),entry,exit_price,SIZE_USD,pnl))
                                    continue
                        skipped[reason]=skipped.get(reason,0)+1
                    stats[f'{mode}:{symbol}']={'trades':wins+losses,'wins':wins,'losses':losses,'win_rate_pct':round(wins/(wins+losses)*100,2) if wins+losses else 0,'pnl_usd':round(balance-100,5),'ending_balance_usd':round(balance,5),'max_drawdown_pct':round(max_dd,3),'skipped':skipped}
            summary={'groups':len(groups),'events':len(events),'trades':len(records),'by_strategy':stats,'exploratory_only':True,'causal_validation':False,'warning':'Historical forecast/actual values are latest stored values, not timestamped snapshots. Actual replay may have look-ahead bias; never treat as validated performance. No live paper balances modified.'}
            cursor=await db.execute('INSERT INTO consensus_replay_runs(settings_json,summary_json) VALUES (?,?)',(json.dumps(settings),json.dumps(summary)))
            run_id=cursor.lastrowid
            if records:
                await db.executemany('''INSERT INTO consensus_replay_trades(run_id,mode,symbol,group_key,event_ids,direction,votes_up,votes_down,entry_ts,exit_ts,entry_price,exit_price,size_usd,pnl) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',[(run_id,*r) for r in records])
            await db.commit()
            return {'run_id':run_id,'settings':settings,'summary':summary}
