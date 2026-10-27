# News Trading Bot — Research/Paper Safety Release (2026-10-27)

This package preserves the existing FastAPI dashboard, Telegram bot, calendar importer and database schema. It is **NOT** a certified live trading system.

## Changes
- Backtest hit rate uses only completed prior events (60-minute observation lag).
- Backtest minimum-move filter uses pre-entry candles instead of future `ret_15m`.
- Backtest entry/exit candles must exist at the exact requested minute; no nearest future candle.
- Paper entry no longer filters on future `ret_15m`, requires 5 prior completed events, and uses recent confirmed price at processing time.
- LIVE exchange order execution is **locked**. `/live/toggle` rejects enabling it; the engine enforces dry-run even if `LIVE_ENABLED=true` in `.env`.
- Web defaults to localhost; avoid exposing the dashboard without authentication and TLS.

## Install / run
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run_server.py
```
On Windows: `.venv\\Scripts\\activate` instead of `source ...`.

Start Telegram worker separately if desired: `python run_bot.py`. Configure `.env` based on `app/config.py` fields; never commit API keys.

## Important limitations
- The backtest is a research baseline, not a validated strategy: timestamp semantics of candle closes, news release availability and data quality must be independently verified.
- Paper and backtest use different entry mechanics and should not be compared as equivalent execution simulations.
- No broker-side protective orders, fill reconciliation, fee accounting by execution, restart recovery or end-to-end integration testing have been completed for live execution.
- The historical database included in the original archive is a sample, not evidence of profitability.
- `min_move_pct` in backtest now means pre-entry movement; past saved backtest results should be regenerated.
- Do not use real funds with this release.
