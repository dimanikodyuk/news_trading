## Safety patch
- Removed future 15-minute return gating from paper and backtest.
- Excluded later events from historical hit-rate calculations.
- Prevented future-candle nearest-neighbor lookup in backtest.
- Disabled LIVE mode via API and execution engine.
- Changed default web bind to loopback.

This is a targeted safety release, not a full feature-complete implementation of all three proposed research strategies.
