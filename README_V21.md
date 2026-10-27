# News Trading Bot v2.1 — Research Safety Update

This is an incremental update to the existing Raspberry Pi application. It **does not** implement automated v2 paper portfolios or strategy auto-selection. LIVE remains outside the scope of this update; do not enable it.

## Changes
- Backtest mode query parameter: `research` (no historical hit-rate gate), `walk_forward` (prior history only), `validated` (at least 5 prior HIT/MISS observations and configured threshold).
- Read-only `GET /api/v2.1/diagnostics` and `GET /api/v2.1/clusters`.
- No database migrations. Existing DB is not included.

## Install
Stop the server; back up the entire project including the DB. Copy archive contents into `~/news_trading` (do not delete `.env`, `.venv` or `data/`). Run `python -m compileall -q app` then `python run_server.py`.

## Examples
- `/backtest/run?mode=research&min_move_pct=0&symbols=SOLUSDT,BTCUSDT,ETHUSDT` (POST)
- `/backtest/run?mode=walk_forward&min_events=1` (POST)
- `/backtest/run?mode=validated&min_events=5` (POST)

**Caveats:** Legacy backtest retains historical event-direction assumptions, spot-short simulation controls and candle timing limitations; treat outputs as exploratory, not executable results. The new Strategy Lab also remains research-only. No evidence of profitability has been established. Do not enable real orders.
