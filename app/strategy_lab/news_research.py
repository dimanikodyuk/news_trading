"""Read-only historical news reaction research. No live/paper orders or account mutations."""
import asyncio
import json
from collections import Counter
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime, timezone
from app.db import get_db

SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
FEE = 0.001
SLIP = 0.0005
SIZE = 10.0

class ResearchParams(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pre_minutes: int = Field(15, ge=14, le=60)
    reaction_minutes: int = Field(3, ge=1, le=10)
    confirmation_minutes: int = Field(6, ge=1, le=20)
    hold_minutes: int = Field(15, ge=1, le=120)
    min_volume_ratio: float = Field(0.0, ge=0, le=10)
    min_breakout_atr: float = Field(0.0, ge=0, le=5)
    trade_size_usd: float = Field(10.0, gt=0, le=10000)
    fee_per_side: float = Field(0.001, ge=0, le=0.02)
    slippage_per_side: float = Field(0.0005, ge=0, le=0.02)
    enable_reversal: bool = True

_lock = asyncio.Lock()
DDL = '''
CREATE TABLE IF NOT EXISTS news_research_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 settings_json TEXT NOT NULL,
 summary_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS news_research_trades (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 run_id INTEGER NOT NULL,
 event_id INTEGER NOT NULL,
 event_title TEXT NOT NULL,
 event_ts INTEGER NOT NULL,
 symbol TEXT NOT NULL,
 strategy TEXT NOT NULL,
 direction TEXT NOT NULL,
 entry_ts INTEGER NOT NULL,
 exit_ts INTEGER NOT NULL,
 entry_price REAL NOT NULL,
 exit_price REAL NOT NULL,
 gross_pnl REAL NOT NULL,
 net_pnl REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_news_research_run ON news_research_trades(run_id,strategy,symbol);
'''

def event_ms(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None

def simulate(direction, entry, exit_price, size=SIZE, fee=FEE, slip=SLIP):
    sign = 1 if direction == 'long' else -1
    gross = size * (exit_price / entry - 1) * sign
    entry_exec = entry * (1 + sign * slip)
    exit_exec = exit_price * (1 - sign * slip)
    qty = size / entry_exec
    net = qty * (exit_exec - entry_exec) * sign - fee * (size + qty * exit_exec)
    return gross, net

async def init(db):
    await db.executescript(DDL)
    await db.commit()

async def latest():
    async with get_db() as db:
        await init(db)
        row = await (await db.execute('SELECT * FROM news_research_runs ORDER BY id DESC LIMIT 1')).fetchone()
        if not row: return {'run': None, 'trades': []}
        trades = [dict(x) for x in await (await db.execute('SELECT * FROM news_research_trades WHERE run_id=? ORDER BY event_ts DESC,symbol,strategy LIMIT 500', (row['id'],))).fetchall()]
        return {'run': {'id':row['id'],'created_at':row['created_at'],'settings':json.loads(row['settings_json']),'summary':json.loads(row['summary_json'])},'trades':trades}

async def run(params: Optional[ResearchParams] = None):
    if _lock.locked(): return {'error':'research_already_running'}
    async with _lock:
        params = params or ResearchParams()
        settings = {**params.model_dump(), 'news':'USD high', 'symbols':list(SYMBOLS), 'timeframe':'1m', 'mode':'research_only', 'execution':'next_minute_open', 'exit':'fixed_hold_no_stop_or_take_profit'}
        counts=Counter(); rows=[]; totals={}
        async with get_db() as db:
            await init(db)
            events = [dict(e) for e in await (await db.execute("SELECT id,title,country,importance,time_utc FROM events WHERE lower(importance)='high' AND (upper(coalesce(country,'')) IN ('USD','US','USA','UNITED STATES') OR upper(coalesce(country,'')) LIKE '%USD%') ORDER BY time_utc,id")).fetchall()]
            counts['eligible_events']=len(events)
            for ev in events:
                ts=event_ms(ev['time_utc'])
                if ts is None: counts['invalid_event_time']+=1; continue
                # Event timestamps are aligned to minute. Avoid same-minute fills.
                base=(ts//60000)*60000
                for symbol in SYMBOLS:
                    # Fetch complete confirmed 1m bars only; gaps invalidate the scenario.
                    bars=[dict(b) for b in await (await db.execute("SELECT ts,open,high,low,close,volume FROM prices WHERE symbol=? AND interval='1' AND confirm=1 AND ts>=? AND ts<? ORDER BY ts", (symbol,base-params.pre_minutes*60000,base+(params.reaction_minutes+params.confirmation_minutes+params.hold_minutes+3)*60000))).fetchall()]
                    lookup={int(b['ts']):b for b in bars}
                    def seq(start,n):
                        result=[lookup.get(base+(start+i)*60000) for i in range(n)]
                        if any(b is None for b in result): return None
                        if any(any(b[k] is None or float(b[k])<=0 for k in ('open','high','low','close')) for b in result): return None
                        return result
                    pre=seq(-params.pre_minutes,params.pre_minutes)
                    reaction=seq(0,params.reaction_minutes)
                    post=seq(params.reaction_minutes,params.confirmation_minutes+1)
                    if not pre or not reaction or not post:
                        counts['missing_contiguous_bars']+=1; continue
                    # First 3 complete candles define reaction range; trade only after a CLOSED candle confirms.
                    rh=max(float(b['high']) for b in reaction); rl=min(float(b['low']) for b in reaction)
                    # ATR from the pre-news bars only, no future data.
                    trs=[]
                    for k,b in enumerate(pre):
                        previous=float(pre[k-1]['close']) if k else float(b['open'])
                        trs.append(max(float(b['high'])-float(b['low']), abs(float(b['high'])-previous), abs(float(b['low'])-previous)))
                    atr=sum(trs[-14:])/14
                    avg_vol=sum(float(b['volume'] or 0) for b in pre)/len(pre)
                    # Breakout: first confirmed close outside the reaction range, within next 6 bars.
                    breakout=None
                    for i in range(params.confirmation_minutes):
                        b=post[i]
                        if float(b['close'])>rh+params.min_breakout_atr*atr: candidate=('long',i+1)
                        elif float(b['close'])<rl-params.min_breakout_atr*atr: candidate=('short',i+1)
                        else: continue
                        if params.min_volume_ratio and (avg_vol <= 0 or float(b['volume'] or 0) < avg_vol*params.min_volume_ratio):
                            counts['volume_filter_rejected']+=1
                            continue
                        breakout=candidate; break
                    candidates=[]
                    if breakout:
                        candidates.append(('breakout',*breakout))
                        # Reversal: price closes back inside the initial range within remaining window.
                        bdir, bentry=breakout
                        for j in range(bentry,params.confirmation_minutes):
                            b=post[j]
                            if bdir=='long' and float(b['close'])<rh:
                                
                                if params.enable_reversal: candidates.append(('reversal','short',j+1))
                                break
                            if bdir=='short' and float(b['close'])>rl:
                                
                                if params.enable_reversal: candidates.append(('reversal','long',j+1))
                                break
                    else: counts['no_confirmed_breakout']+=1
                    for strategy,direction,entry_offset in candidates:
                        # post[0] = minute +3; signal closes at end of its candle; fill next minute open.
                        entry_min=params.reaction_minutes+entry_offset
                        entry=lookup.get(base+entry_min*60000)
                        exitbar=lookup.get(base+(entry_min+params.hold_minutes)*60000)
                        if not entry or not exitbar or not entry['open'] or not exitbar['open'] or entry['open']<=0 or exitbar['open']<=0:
                            counts['missing_entry_or_exit']+=1; continue
                        ep=float(entry['open']); xp=float(exitbar['open'])
                        gross,net=simulate(direction,ep,xp,params.trade_size_usd,params.fee_per_side,params.slippage_per_side)
                        rows.append((ev['id'],ev['title'],base,symbol,strategy,direction,int(entry['ts']),int(exitbar['ts']),ep,xp,gross,net))
                        key=f'{strategy}:{symbol}'
                        s=totals.setdefault(key,{'trades':0,'wins':0,'gross_pnl_usd':0.,'net_pnl_usd':0.})
                        s['trades']+=1; s['wins']+=int(net>0); s['gross_pnl_usd']+=gross; s['net_pnl_usd']+=net
            for s in totals.values():
                s['win_rate_pct']=round(100*s['wins']/s['trades'],2) if s['trades'] else 0
                s['gross_pnl_usd']=round(s['gross_pnl_usd'],5)
                s['net_pnl_usd']=round(s['net_pnl_usd'],5)
            summary={'events':len(events),'trades':len(rows),'by_strategy':totals,'skipped':dict(counts),'warning':'Exploratory only. No SL/TP: exits use next-minute open after fixed hold. Event timestamps may not equal actual publication times; historical fills are idealized. No out-of-sample validation. No paper/live accounts modified.'}
            cur=await db.execute('INSERT INTO news_research_runs(settings_json,summary_json) VALUES (?,?)',(json.dumps(settings),json.dumps(summary)))
            run_id=cur.lastrowid
            if rows:
                await db.executemany('INSERT INTO news_research_trades(run_id,event_id,event_title,event_ts,symbol,strategy,direction,entry_ts,exit_ts,entry_price,exit_price,gross_pnl,net_pnl) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',[(run_id,*r) for r in rows])
            await db.commit()
            return {'run_id':run_id,'summary':summary,'settings':settings}
