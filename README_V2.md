# News Trading Bot v2.0 — Strategy Lab (research preview)

The existing dashboard, Telegram bot and data collection remain. Added `/api/v2/strategies` and `/api/v2/research/backtest` (GET).

Start: `pip install -r requirements.txt` then `python run_server.py`. Open `/docs` for interactive v2 API.

Examples: `/api/v2/research/backtest?strategy=momentum&symbol=BTCUSDT` or `?strategy=reversal&entry_min=2&symbol=SOLUSDT`.

**Safety:** v2 module never submits orders and never writes to SQLite. Legacy LIVE remains disabled as in safety release. Do not enable legacy LIVE.

**Method:** confirmed 1-minute bars; a signal from completed bars, execution at next bar open; no nearest future candle interpolation. Fees and slippage on both sides. 70/30 chronological holdout by event time. No parameter optimization. A missing candle skips the trade.

**Caveats:** ForexFactory actual release timestamps not recorded, so News Surprise is **exploratory oracle analysis only**, NOT a live-tradable strategy. The historical database may be too sparse to evaluate anything. Candle timestamp convention must be validated against Bybit ingestion. The 70/30 split is a diagnostic, not a substitute for walk-forward validation. Price-only backtests cannot reproduce order-book spread, latency, gaps or stop-loss fills. There is no v2 live execution, no v2 autonomous paper scheduler and no new UI tab (API is available via `/docs`).

**Next steps:** collect verified release timestamps and actual/forecast as-of records, larger historical dataset, integrate paper signal scheduling, implement independent walk-forward evaluation and exchange-side safety controls before any LIVE.
