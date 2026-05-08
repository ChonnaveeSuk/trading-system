# QuantAI Codebase Audit

**Date:** 2026-05-15
**Audit Scope:** 
- `strategy/src/` (Python strategy layer)
- `scripts/` (Operational scripts and utilities)
- `core/src/` (Rust execution engine)
- `gcp/terraform/` (Infrastructure as Code)

---

## 1. Strategy Layer (`strategy/src/`)

This directory contains the Python-based signal generation, backtesting, and data fetching logic.

### `signals/momentum.py`
- **Purpose:** Primary signal generation logic using Dual Moving Average crossover, RSI mean-reversion, and Bollinger Bands.
- **Key Functions/Classes:** `MomentumStrategy`, `MomentumConfig`, `MomentumFeatures`, `sector_for`.
- **Dependencies:** `pandas`, `numpy`, `EconomicCalendar`, `EarningsCalendar`.
- **Potential Issues:** 30 days of data is flagged as insufficient for production (requires 252 days). 
- **Technical Debt:** Hardcoded `SYMBOL_TO_SECTOR` mapping needs frequent manual updates if the trading universe changes.

### `bridge/alpaca_direct.py`
- **Purpose:** Direct REST-based order submission to Alpaca, bypassing the Rust engine for Cloud Run Jobs.
- **Key Functions/Classes:** `AlpacaDirectClient`, `check_and_trigger_stops`, `submit_signal`.
- **Dependencies:** `requests`, `psycopg2`, `MomentumStrategy`.
- **Potential Issues:** Bypasses the central Rust risk engine, requiring duplication of risk logic in Python.
- **Technical Debt:** Duplicate risk limits (matching Rust `core/src/risk/mod.rs`) increase the risk of logic drift.

### `backtester/engine.py`
- **Purpose:** Simulates the strategy on historical data with realistic transaction costs and walk-forward validation.
- **Key Functions/Classes:** `BacktestEngine`, `walk_forward`, `_simulate_on_slice`.
- **Dependencies:** `pandas`, `numpy`, `MomentumStrategy`.
- **Potential Issues:** Slippage and commission models are estimates; actual performance may vary.
- **Technical Debt:** The vectorized `generate_signals_series` and iterative `_simulate_on_slice` have slight logic duplication for efficiency.

### `bridge/client.py`
- **Purpose:** gRPC client for communicating with the Rust execution engine.
- **Key Functions/Classes:** `TradingBridgeClient`, `submit_signal`, `health_check`.
- **Dependencies:** `grpc`, `trading_pb2`, `trading_pb2_grpc`.
- **Potential Issues:** Network latency and timeouts in gRPC calls.
- **Technical Debt:** Only supports insecure channels; needs TLS for non-localhost communication.

### `data/alpaca_fetcher.py`
- **Purpose:** Fetches daily OHLCV bars from Alpaca Data API and upserts into Postgres.
- **Key Functions/Classes:** `AlpacaFetcher`, `fetch_and_store_all`.
- **Dependencies:** `requests`, `psycopg2`, `GCP Secret Manager`.
- **Potential Issues:** Rate limiting (200 req/min on paper).
- **Technical Debt:** Symbol mapping between yfinance and Alpaca is hand-maintained.

### `data/fetcher.py`
- **Purpose:** Primary data source for the strategy layer, fetching bars from Postgres.
- **Key Functions/Classes:** `PostgresOhlcvFetcher`.
- **Dependencies:** `psycopg2`, `pandas`.
- **Potential Issues:** Synchronous DB calls may block if many symbols are fetched sequentially.
- **Technical Debt:** Lacks caching; repeated fetches for the same symbol/date range hit the DB every time.

### `data/yfinance_fetcher.py`
- **Purpose:** Data ingestion from Yahoo Finance, primarily for backtesting.
- **Key Functions/Classes:** `YfinanceFetcher`.
- **Dependencies:** `yfinance`, `psycopg2`, `pandas`.
- **Potential Issues:** Yahoo Finance often blocks GCP IPs; survivorship bias in data.
- **Technical Debt:** Includes a large hardcoded ticker map.

### `filters/economic_calendar.py`
- **Purpose:** Hardcoded macroeconomic and earnings event calendars for signal blackout windows.
- **Key Functions/Classes:** `EconomicCalendar`, `EarningsCalendar`.
- **Dependencies:** `datetime`.
- **Potential Issues:** Requires manual updates for 2027+ FOMC dates.
- **Technical Debt:** Hardcoded dates instead of an external API feed (intentional for reliability but high maintenance).

### `db.py`
- **Purpose:** Utility to retrieve the `DATABASE_URL` from environment variables.
- **Key Functions/Classes:** `database_url`.
- **Technical Debt:** Extremely minimal; could be merged into a more general config module.

### `gcp/__init__.py`
- **Purpose:** GCP client initialization and secret management.
- **Key Functions/Classes:** `get_secret`, `verify_paper_mode`.
- **Dependencies:** `google-cloud-secretmanager`.
- **Technical Debt:** `verify_paper_mode` is a hard stop; might need more flexibility for dev environments.

### `__init__.py`, `backtester/__init__.py`, `bridge/__init__.py`, `data/__init__.py`, `filters/__init__.py`, `signals/__init__.py`
- **Purpose:** Package initialization and type exports.
- **Key Functions/Classes:** `BacktestConfig`, `BacktestResult`, `validate_ohlcv`, `Direction`, `SignalResult`.
- **Technical Debt:** `data/validate_ohlcv` is a critical integrity check that should perhaps reside in a dedicated validation module.

### `bridge/trading_pb2.py` & `bridge/trading_pb2_grpc.py`
- **Purpose:** Auto-generated code from `trading.proto` for gRPC communication.

---

## 2. Operational Scripts (`scripts/`)

This directory contains utilities for maintenance, reporting, and system verification.

### `morning_report.py`
- **Purpose:** Generates and sends a comprehensive daily status report to Telegram and Obsidian.
- **Key Functions/Classes:** `ReportData`, `build_report`, `save_to_obsidian`.
- **Dependencies:** `telegram_alert`, `psycopg2`, `MomentumStrategy`.
- **Potential Issues:** Large message size might hit Telegram limits (has truncation logic).
- **Technical Debt:** Heavy reliance on specific DB table structures; brittle if schema changes.

### `gate_progress.py`
- **Purpose:** Audits the 90-day paper trading gate metrics.
- **Key Functions/Classes:** `GateMetrics`, `compute_metrics`, `evaluate_gate`.
- **Dependencies:** `psycopg2`.
- **Potential Issues:** Uses a coarse win/loss approximation based on daily P&L signs.
- **Technical Debt:** Table `gate_progress` schema is defined as a string literal in code.

### `reconcile_alpaca_fills.py`
- **Purpose:** Matches submitted orders with Alpaca fill events and updates local DB.
- **Key Functions/Classes:** `reconcile`, `_sync_positions_from_alpaca`.
- **Dependencies:** `requests`, `psycopg2`.
- **Potential Issues:** Polling-based; may miss fills if run infrequently (though usually run daily).
- **Technical Debt:** Duplicate symbol translation logic from `alpaca_direct.py`.

### `log_system_health.py`
- **Purpose:** Periodic collection of system metrics (Redis, Postgres, Docker, Alpaca).
- **Key Functions/Classes:** `collect_pg_connections`, `collect_redis_stats`, `collect_docker_health`.
- **Dependencies:** `psycopg2`, `subprocess`.
- **Technical Debt:** Uses `urllib.request` for some pings instead of the project-standard `requests`.

### `test_alpaca_connection.py`
- **Purpose:** E2E smoke test for Alpaca API, including a small test order.
- **Dependencies:** `requests`, `psycopg2`, `gcloud CLI`.
- **Potential Issues:** Submits a real (paper) order; costs nothing but adds noise to account history.

### `update_daily_pnl.py`
- **Purpose:** Daily computation of realized/unrealized P&L from Alpaca equity.
- **Dependencies:** `requests`, `psycopg2`.
- **Potential Issues:** High sensitivity to Alpaca API availability during the end-of-day run.
- **Technical Debt:** Hardcoded `$100,000` starting capital fallback.

### `seed_alpaca.py` & `seed_yfinance.py`
- **Purpose:** Initial population of the `ohlcv` table.
- **Dependencies:** `AlpacaFetcher` / `YfinanceFetcher`.
- **Technical Debt:** `DEFAULT_SYMBOLS` list is duplicated between these two files.

### `telegram_alert.py`
- **Purpose:** Wrapper for sending Telegram messages using a bot.
- **Dependencies:** `requests`, `GCP Secret Manager`.
- **Technical Debt:** Credential loading logic is repeated in several places.

### `error_report.py`
- **Purpose:** Integration with Google Cloud Error Reporting.
- **Dependencies:** `google-cloud-error-reporting`.

### `_db.py`
- **Purpose:** Shared DB connection helper for scripts.
- **Technical Debt:** Duplicates `strategy/src/db.py`.

### `debug_info.py`
- **Purpose:** Minimal script to inspect recent DB orders.

### `test_ibkr_connection.py`
- **Purpose:** Reachability check for IB Gateway (Phase 4 preparation).

### `update_claude_md_with_fill.py`
- **Purpose:** Automated project documentation update after the first live fill.

### `calendar_filter_backtest_compare.py`, `earnings_filter_backtest_compare.py`, `vix_filter_backtest_compare.py`, `vix_threshold_sweep.py`
- **Purpose:** Research and optimization scripts for strategy filters.
- **Dependencies:** `BacktestEngine`, `PostgresOhlcvFetcher`.
- **Technical Debt:** These scripts share significant boilerplate for setting up backtests.

---

## 3. Core Execution Engine (`core/src/`)

This directory contains the high-performance Rust execution engine and risk management logic.

### `main.rs`
- **Purpose:** Entrypoint for the execution engine binary. Initializes DB, Redis, Broker, OMS, and Bridge.
- **Potential Issues:** `TRADING_MODE=paper` is strictly enforced; live mode requires code-level changes (intentional safety gate).
- **Technical Debt:** `DATABASE_URL` parsing has fallback logic that could be simplified.

### `risk/mod.rs`
- **Purpose:** The "Sacred" risk engine. Enforces hardcoded limits on positions, loss, and drawdown.
- **Key Functions/Classes:** `RiskEngine`, `check_order`, `size_from_atr`, `TrailingStopState`.
- **Potential Issues:** Hardcoded limits cannot be changed without recompilation (intentional).
- **Technical Debt:** ATR calculation is simple rolling mean; could support Wilder's EMA for better accuracy.

### `order/manager.rs`
- **Purpose:** Implements the OMS, managing the order lifecycle and DB persistence.
- **Key Functions/Classes:** `OmsManager`, `submit`, `apply_fill`.
- **Dependencies:** `sqlx`, `RwLock`, `Broker`.
- **Potential Issues:** Heavy use of `RwLock` could lead to contention under extremely high throughput (not an issue for this scale).
- **Technical Debt:** `reload_from_db` only loads active positions; doesn't restore full order history to memory.

### `broker/alpaca.rs`
- **Purpose:** REST-based Alpaca paper broker implementation.
- **Key Functions/Classes:** `AlpacaBroker`.
- **Dependencies:** `reqwest`.
- **Potential Issues:** Asynchronous fills are not handled natively in the Rust broker; relies on external scripts or future websocket implementation.

### `broker/paper.rs`
- **Purpose:** Realistic local paper trading simulator with simulated latency and slippage.
- **Key Functions/Classes:** `PaperBroker`.
- **Potential Issues:** Latency jitter is uniform; does not model complex network tails.

### `bridge/mod.rs`
- **Purpose:** gRPC server implementation for receiving Python signals.
- **Key Functions/Classes:** `BridgeService`.
- **Dependencies:** `tonic`.
- **Technical Debt:** HOLD signals are rejected at this layer; should perhaps be ignored silently to reduce log noise.

### `gcp/pubsub.rs`
- **Purpose:** Fire-and-forget publishing to GCP Pub/Sub.
- **Key Functions/Classes:** `PubSubClient`, `publish_fill_bq`.
- **Dependencies:** `reqwest`, `BASE64`.
- **Potential Issues:** Token refresh logic is custom-built; could use `google-cloud-rust` crates if they stabilize.

### `market_data/feed.rs`
- **Purpose:** Redis-backed tick storage and real-time pub/sub delivery.
- **Key Functions/Classes:** `RedisFeed`, `subscribe`.
- **Dependencies:** `redis-rs`.

### `types.rs`
- **Purpose:** Canonical domain types.
- **Key Functions/Classes:** `Order`, `Fill`, `Position`, `Side`, `Tick`.
- **Potential Issues:** Uses `rust_decimal`, which is excellent for accuracy but requires care when interfacing with external floating-point APIs.

### `error.rs`, `lib.rs`, `broker/mod.rs`, `gcp/mod.rs`, `market_data/mod.rs`, `order/mod.rs`
- **Purpose:** Module definitions, traits, and error hierarchy.

---

## 4. Infrastructure (`gcp/terraform/`)

Infrastructure as Code for provisioning the GCP environment.

### `main.tf`
- **Purpose:** Main provider setup, API enablement, Service Account, and central Pub/Sub/BigQuery resources.
- **Technical Debt:** Terraform state is currently local; needs GCS backend configuration.

### `cloud_sql.tf`
- **Purpose:** Provisions the managed PostgreSQL instance.
- **Potential Issues:** `deletion_protection = false` is risky for anything beyond paper trading.
- **Technical Debt:** `database-url` secret version creation uses `ignore_changes` to prevent re-generation, which is a bit of a hack.

### `cloud_run_jobs.tf`
- **Purpose:** Configures the daily trading runner and backup jobs.
- **Potential Issues:** `MANAGE_CLOUD_SQL=1` relies on the runner SA having `cloudsql.admin` rights.
- **Technical Debt:** Hardcoded memory limits (512Mi) might need adjustment for larger universes.

### `monitoring.tf`
- **Purpose:** Alert policies for job failures, stop-losses, and exceptions.
- **Key Resources:** Webhook notification channel for Telegram.
- **Potential Issues:** Missed-schedule alert is an approximation.

### `variables.tf` & `outputs.tf`
- **Purpose:** Configuration inputs and helpful output commands.
- **Technical Debt:** `project_id` default should be removed to force explicit setting.

---

## Overall Observations

### Strengths
- **Safety First:** Rust risk engine and `TRADING_MODE=paper` guards provide strong protection.
- **Decoupled Architecture:** Clean separation between strategy (Python) and execution (Rust).
- **Robust Reporting:** Integrated Telegram alerts and Obsidian sync provide high visibility.
- **Observability:** Strong logging and GCP Monitoring integration.

### Identified Technical Debt
1. **Logic Duplication:** Risk limits and symbol translations are duplicated between Python and Rust.
2. **Manual Maintenance:** Economic/Earnings calendars and Sector maps require manual updates.
3. **Data Inefficiency:** Lack of caching in data fetchers.
4. **Environment Drift:** Duplicate `.db.py` logic and hardcoded values across scripts.

### Recommendations
1. **Consolidate Constants:** Move all shared constants (risk limits, sectors) to a single config source (e.g., a YAML file or shared DB table).
2. **Automate Calendars:** Switch to an external API (like FRED or Earnings Calendar providers) for event data.
3. **Rust WebSocket:** Implement a native Rust Alpaca WebSocket consumer for real-time fills and ticks.
4. **Centralize Config:** Refactor the numerous `__init__.py` and utility scripts to use a unified configuration management approach.
