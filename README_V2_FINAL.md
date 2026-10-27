# News Trading Bot v2 — Research Dashboard release

## What is implemented
- Existing FastAPI dashboard, Telegram integrations and historical modules retained.
- Strategy Lab tab with read-only comparison of three strategies.
- `/api/v2/research/compare` endpoint compares three strategies with 70/30 chronological split.
- Costs (fees/slippage) included in backtest; Spot long-only enforced.
- All intervening minute candles required to avoid hidden gaps.
- No automated v2 paper trading, strategy promotion, or live trading added. The three strategies are research-only.

## Important caveats
- News Surprise assumes actual data at release without verified availability timestamp: **oracle / exploratory only**.
- Backtests use 1-minute OHLCV and cannot model intraminute execution, spread spikes or liquidity precisely.
- Repeated selection on holdout data invalidates its independence.
- A historical test is not proof of positive expected return.
- Existing legacy LIVE routes remain in the application, but the release is intended for research only; do not provide trading API keys or enable LIVE.

## Raspberry Pi 4 upgrade (preserve DB and .env)
1. Stop existing service/server.
2. Backup: `cp -a ~/news_trading ~/news_trading_backup_$(date +%Y%m%d_%H%M%S)`.
3. Unpack ZIP on your computer. Copy **contents of `news_trading_v2_complete/`** over `~/news_trading/` (not the parent folder), excluding local `.env`, `.venv`, SQLite files.
4. `cd ~/news_trading && source .venv/bin/activate && pip install -r requirements.txt`.
5. `python -m compileall -q app && python -c "from app.strategy_lab.engine import STRATEGIES; print(STRATEGIES)"`.
6. `python run_server.py`; open `http://<PI_IP>:5085/` and select Strategy Lab v2.

**Security:** LAN access only, do not expose port 5085 to the public internet. Keep Bybit keys removed from research instance.
