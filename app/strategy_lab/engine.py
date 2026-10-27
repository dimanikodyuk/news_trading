"""Causal research-only event-driven backtest. No exchange order placement."""
import math
import re
from datetime import datetime, timezone
from collections import defaultdict
from app.db import get_db

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
STRATEGIES = ("news_surprise", "momentum", "reversal")

def parse_number(value):
    """Parse comparable macro values; reject non-numeric, mixed or ambiguous units."""
    if value is None: return None
    s = str(value).strip().replace(',', '')
    m = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([%KMB]?)", s, re.I)
    if not m: return None
    n=float(m.group(1)); unit=m.group(2).upper()
    return n * {'':1,'%':1,'K':1e3,'M':1e6,'B':1e9}[unit]

def event_ms(s):
    dt=datetime.fromisoformat(s.replace('Z','+00:00'))
    if dt.tzinfo is None: raise ValueError('event timestamps must be timezone aware')
    return int(dt.timestamp()*1000)

def signal(strategy, event, candles, entry_min):
    """All candles indexed by their OPEN timestamp; use only fully closed bars."""
    pre=candles.get(-1)
    first=candles.get(0)
    if not pre or not first: return None
    baseline=pre['close']
    if baseline <= 0: return None
    move=(first['close']/baseline-1)*100
    if strategy=='momentum':
        if abs(move)<0.20: return None
        return 'long' if move>0 else 'short'
    if strategy=='reversal':
        second=candles.get(1)
        if entry_min<2 or not second or abs(move)<0.40: return None
        retrace=(second['close']/first['close']-1)*100
        if move*retrace>=0 or abs(retrace)<0.10: return None
        return 'long' if move<0 else 'short'
    if strategy=='news_surprise':
        actual=parse_number(event['actual_value']); forecast=parse_number(event['forecast_value'])
        if actual is None or forecast is None or forecast==0: return None
        # Macro mapping is a hypothesis, not universal economic truth.
        title=event['title'].lower()
        negative=('cpi','inflation','pce','non-farm','nonfarm','nfp','unemployment','jobless','interest rate','fed funds')
        positive=('gdp','retail sales','adp employment','consumer confidence')
        if not any(x in title for x in negative+positive): return None
        delta=(actual-forecast)/abs(forecast)
        if abs(delta)<0.01: return None
        sign = -1 if any(x in title for x in negative) else 1
        return 'long' if delta*sign>0 else 'short'
    raise ValueError('Unknown strategy')

def metrics(trades):
    if not trades: return {'trades':0,'win_rate_pct':None,'net_pnl_usd':0,'profit_factor':None,'max_drawdown_usd':0,'avg_pnl_usd':None}
    pnls=[t['pnl_usd'] for t in trades]; wins=sum(p>0 for p in pnls)
    pos=sum(p for p in pnls if p>0); neg=-sum(p for p in pnls if p<0)
    equity=peak=dd=0.0
    for p in pnls:
        equity+=p; peak=max(peak,equity); dd=max(dd,peak-equity)
    return {'trades':len(pnls),'win_rate_pct':round(100*wins/len(pnls),2), 'net_pnl_usd':round(sum(pnls),4), 'profit_factor':round(pos/neg,3) if neg else None, 'max_drawdown_usd':round(dd,4),'avg_pnl_usd':round(sum(pnls)/len(pnls),4)}

async def research_backtest(strategy='momentum', symbol='BTCUSDT', entry_min=1, hold_min=15, size_usd=10.0, fee_bps=10.0, slippage_bps=5.0, long_only=True, limit_events=5000):
    if strategy not in STRATEGIES: raise ValueError('invalid strategy')
    if symbol not in SYMBOLS: raise ValueError('invalid symbol')
    if not 1<=entry_min<=10 or not 1<=hold_min<=120 or not 0<size_usd<=10000 or not 0<=fee_bps<=100 or not 0<=slippage_bps<=100 or not 1<=limit_events<=10000: raise ValueError('invalid parameter')
    if strategy=='reversal' and entry_min<2: raise ValueError('reversal needs entry_min >= 2')
    # An entry at minute N uses candle N-1 (which has closed); execution at OPEN of candle N.
    # Calendar actual values have no verified release timestamp: surprise is an ORACLE / exploratory test only.
    exploratory=strategy=='news_surprise'
    trades=[]; skipped=defaultdict(int)
    async with get_db() as db:
        cur=await db.execute('SELECT id,title,time_utc,actual_value,forecast_value FROM events WHERE importance = ? ORDER BY time_utc LIMIT ?',('high',limit_events))
        events=await cur.fetchall()
        for ev in events:
            try: t=event_ms(ev['time_utc'])
            except (ValueError,TypeError): skipped['invalid_time']+=1; continue
            # Restrict to exact minute boundaries; 1m bars have OPEN timestamps.
            if t%60000: skipped['non_minute_event']+=1; continue
            start=t-60000; end=t+(entry_min+hold_min)*60000
            cur=await db.execute('SELECT ts,open,close,confirm FROM prices WHERE symbol=? AND interval=? AND ts BETWEEN ? AND ? AND confirm=1 ORDER BY ts',(symbol,'1',start,end))
            candles={int((r['ts']-t)//60000):dict(r) for r in await cur.fetchall() if (r['ts']-t)%60000==0}
            if any(i not in candles for i in (-1, *range(entry_min),entry_min,entry_min+hold_min)):
                skipped['missing_closed_candles']+=1; continue
            direction=signal(strategy,ev,candles,entry_min)
            if not direction: skipped['no_signal']+=1; continue
            if long_only and direction=='short': skipped['spot_long_only']+=1; continue
            entry=candles[entry_min]['open']; exit_=candles[entry_min+hold_min]['open']
            if not entry or not exit_ or entry<=0 or exit_<=0: skipped['bad_price']+=1; continue
            slip=slippage_bps/10000; fee=fee_bps/10000
            buy=entry*(1+slip) if direction=='long' else entry*(1-slip)
            sell=exit_*(1-slip) if direction=='long' else exit_*(1+slip)
            qty=size_usd/buy
            gross=qty*(sell-buy)*(1 if direction=='long' else -1)
            cost=size_usd*fee+qty*sell*fee
            pnl=gross-cost
            trades.append({'event_id':ev['id'],'event':ev['title'],'time_utc':ev['time_utc'],'symbol':symbol,'strategy':strategy,'direction':direction,'entry_ts':t+entry_min*60000,'exit_ts':t+(entry_min+hold_min)*60000,'entry_price':round(buy,8),'exit_price':round(sell,8),'pnl_usd':round(pnl,6)})
    # Split by distinct event timestamps, never random rows. No parameter optimization on test partition.
    timestamps=sorted(set(x['time_utc'] for x in trades)); cutoff=int(len(timestamps)*0.7)
    train_set=set(timestamps[:cutoff]); train=[x for x in trades if x['time_utc'] in train_set]; test=[x for x in trades if x['time_utc'] not in train_set]
    return {'strategy':strategy,'symbol':symbol,'research_only':True,'exploratory_oracle_actual':exploratory,'execution_disabled':True,'assumptions':{'entry':'open of minute entry_min; signals from prior closed bars','exit':'open after hold_min minutes','fee_bps_per_side':fee_bps,'slippage_bps_per_side':slippage_bps,'news_actual_availability':'UNVERIFIED: news_surprise must NOT be treated as executable'},'overall':metrics(trades),'train_70pct':metrics(train),'holdout_30pct':metrics(test),'skipped':dict(skipped),'trades':trades}
