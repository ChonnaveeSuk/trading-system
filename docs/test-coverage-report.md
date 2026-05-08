# QuantAI Test Coverage Report

## Strategy Tests (`strategy/tests/`)
These tests cover the core momentum strategy, indicators, and the Alpaca execution bridge.

- **`test_alpaca_direct.py`**
  - **Covers**: AlpacaDirectClient end-to-end functionality. Tests symbol translation, health checks, signal limits (score, qty, positions), session and cross-invocation deduplication, BUY/SELL order submission, sector concentration caps, and stop loss triggers.
  - **Gaps**: Does not test the exact fallback path when `psycopg2` fails to persist an order (writing to the JSONL log).
  - **Risk Level**: HIGH

- **`test_earnings_calendar.py` & `test_economic_calendar.py`**
  - **Covers**: Blackout logic for macro events and per-symbol earnings. Ensures BUY orders are blocked on/before events and SELL orders pass through.
  - **Gaps**: Interaction between overlapping blackouts (e.g., FOMC and Earnings on the same day).
  - **Risk Level**: LOW

- **`test_kgc_dedup.py`**
  - **Covers**: Dedup logic blocking double-submission of BUY/SELL within the same session or across invocations.
  - **Gaps**: None. Comprehensive coverage.
  - **Risk Level**: LOW

- **`test_phase2.py`, `test_phase3.py`, `test_regime_filter.py`, `test_vix_filter.py`, `test_rsi_atr.py`, `test_trend_ride.py`, `test_trailing_stop.py`**
  - **Covers**: Walk-forward backtester, signal generation, VIX/SPY regime filters, trailing stops, ATR sizing, and RSI thresholds.
  - **Gaps**: Malformed price data handling (NaNs in raw price feeds).
  - **Risk Level**: MEDIUM

## Scripts Tests (`scripts/tests/`)
These tests cover the operational scripts, cron jobs, DB reconciliation, and monitoring.

- **`test_cron_flow_e2e.py`**
  - **Covers**: Complete daily cron execution, empty positions delta math, high concentration warnings.
  - **Gaps**: Handling of concurrent cron runs.
  - **Risk Level**: LOW

- **`test_error_report.py`**
  - **Covers**: Parsing of traceback files and GCP Secret Manager fallback for the error reporter.
  - **Gaps**: None.
  - **Risk Level**: LOW

- **`test_gate_progress.py`**
  - **Covers**: Sharpe, Max Drawdown, and Profit Factor calculations for the 90-day paper trading gate.
  - **Gaps**: Edge case precision errors in Sharpe calc for very small return variations.
  - **Risk Level**: LOW

- **`test_monitoring.py` & `test_obsidian_sync.py`**
  - **Covers**: Morning report query logic (stop loss risk, sector concentration) and writing to Obsidian.
  - **Gaps**: None.
  - **Risk Level**: LOW

- **`test_reconcile_populates_positions.py` & `test_reconcile_resilience.py` & `test_update_daily_pnl_equity_delta.py`**
  - **Covers**: Robustness against constraint violations, populating positions from fills, and equity delta math.
  - **Gaps**: None.
  - **Risk Level**: LOW

## Highest-Risk Uncovered Scenarios
1. Database connection failure during live order persistence (JSONL fallback behavior).
2. Partial API failure during a multi-symbol stop-loss liquidation.
3. Malformed API payload handling in sector concentration logic.
4. Unparseable/Missing `unrealized_plpc` causing the stop-loss loop to crash.
5. Precision rounding checks for Crypto vs Equity quantities in the bridge.

5 NEW tests covering these gaps have been added to `scripts/tests/test_risk_gaps.py`.
