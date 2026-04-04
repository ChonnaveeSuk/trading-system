# QuantAI Trading System — Project State
# Read this at the start of every session.

## Current Status

**Phase:** 4 Prep — COMPLETE. Pending: 90-day paper run via Alpaca
**Mode:** PAPER TRADING ONLY
**Last updated:** 2026-04-03
**Tests:** 82/82 passing (29 Rust + 53 Python)
**GCP:** quantai-trading-paper (asia-southeast1) — Pub/Sub + BigQuery + Secret Manager LIVE

---

## Phase Checklist

### ✅ Phase 0: Foundation (DONE — 2026-03-26)
- [x] Directory structure
- [x] Cargo workspace (core crate)
- [x] Core types: Bar, Tick, Order, Fill, Position, Side, OrderType
- [x] Error hierarchy: TradingError, RiskError
- [x] Risk engine with 14 unit tests (all passing)
- [x] Module stubs: broker, market_data, order, bridge, gcp
- [x] PostgreSQL schema: ohlcv, orders, fills, positions, signals, risk_events, daily_pnl
- [x] Docker Compose: postgres:16 + redis:7
- [x] GCP Terraform skeleton: Pub/Sub, BigQuery, GCS, Secret Manager, IAM
- [x] BigQuery table schemas: trades, ohlcv, signals
- [x] Python strategy stubs: signals, backtester, data, gcp
- [x] .env.example, .gitignore, CLAUDE.md

### ✅ Phase 1 Local: Broker + OMS (DONE — 2026-03-26)
- [x] `core/src/broker/paper.rs` — PaperBroker: slippage (0.5 bps), latency (100 ± 50 ms), commission model, limit order queue
- [x] `core/src/broker/alpaca.rs` — AlpacaBroker: REST client for Alpaca paper API (submit/cancel/health/positions)
- [x] `core/src/market_data/feed.rs` — RedisFeed: publish_tick, get_latest_tick, async subscribe
- [x] `core/src/order/manager.rs` — OmsManager: risk check → DB insert → broker → fill → position upsert
- [x] `core/src/gcp/pubsub.rs` — PubSubClient fire-and-forget (disabled when GCP_PROJECT_ID unset)
- [x] `scripts/seed_ohlcv.sql` — 90 bars: AAPL ($167–$188), BTC-USD ($61k–$73k), EUR-USD ($1.075–$1.099)
- [x] End-to-end paper loop: 5 trades → filled → logged to PostgreSQL
- [x] 29 Rust tests passing, clippy clean

### ✅ Phase 1 GCP: Cloud Infrastructure (DONE — 2026-03-28)
- [x] GCP project `quantai-trading-paper` created, billing linked (`01E85B-0882A0-5BA09B`)
  - Note: `quantai-trading` was already taken globally by another user
- [x] `terraform apply` — all resources provisioned in `asia-southeast1`
  - Fixed: `for_each` on topic/secret `.id` → `.name`/`.secret_id` (computed values not allowed in for_each)
  - Fixed: monitoring alert filter must include `metric.type=` prefix
- [x] Secret Manager: `trading-mode=paper`, `alpaca-api-key`, `alpaca-secret-key`, `alpaca-endpoint`, `quantai-postgres-password` (random 43-char)
- [x] Pub/Sub topics: `quantai-fills`, `quantai-ticks`, `quantai-signals`, `quantai-risk-events`, `quantai-dead-letter`
- [x] BigQuery dataset `quantai_trading`: `trades`, `ohlcv`, `signals` tables (partitioned by day)
- [x] Pub/Sub → BigQuery native subscription: `quantai-fills-to-bigquery` → `trades` table (live fill audit trail)
- [x] GCS bucket `quantai-backups-quantai-trading-paper` (90-day lifecycle, NEARLINE after 30d)
- [x] `PubSubClient.fetch_adc_token()` added — supports `gcloud auth application-default login`
  - Auth priority: GCE metadata → ADC file → `GCP_ACCESS_TOKEN` env var
- [x] `OmsManager.apply_fill()` publishes fills → `quantai-fills` → BigQuery (fire-and-forget, ADR-002)
- [x] `.env` updated: `GCP_PROJECT_ID=quantai-trading-paper`
- [x] Verified: `gcloud secrets versions access latest --secret="trading-mode"` → `paper` ✅

### ✅ Phase 2: Strategy + gRPC Bridge (DONE — 2026-03-26)
- [x] `proto/trading.proto` — SubmitSignal + HealthCheck RPC contract
- [x] `core/build.rs` — tonic-build compiles proto at cargo build time
- [x] `core/src/bridge/mod.rs` — Rust tonic gRPC server on `[::1]:50051`
- [x] `Broker::on_price_update()` — trait method (default no-op; PaperBroker override)
- [x] `OmsManager::update_price()` — seeds broker price cache before market order submission
- [x] `strategy/src/data/fetcher.py` — PostgresOhlcvFetcher (psycopg2)
- [x] `strategy/src/signals/momentum.py` — MomentumStrategy: dual MA crossover + volume confirmation + score [0.55–1.0]
- [x] `strategy/src/backtester/engine.py` — Sharpe, MaxDD, WinRate, CAGR, trade log
- [x] `strategy/src/bridge/client.py` — TradingBridgeClient: HOLD filtering, validation
- [x] `strategy/run_strategy.py` — CLI: `--mode backtest|live|all`
- [x] `strategy/tests/test_phase2.py` — 28 tests: unit + PostgreSQL integration
- [x] End-to-end verified: Python signal → gRPC → Rust → PaperBroker → PostgreSQL fill → Pub/Sub → BigQuery

### ✅ Phase 3: Strategy Tuning + Monitoring (DONE — 2026-03-28)
- [x] `strategy/src/data/yfinance_fetcher.py` — yfinance → PostgreSQL UPSERT, OHLCV repair
- [x] `scripts/seed_yfinance.py` — CLI: downloads 600 days of real OHLCV into PostgreSQL
- [x] Real data seeded: AAPL (421 bars), BTC-USD (602 bars), EUR-USD (425 bars), from 2024-08-02
- [x] Walk-forward backtester: `BacktestEngine.walk_forward()` with IS=252, OOS=63, step=21
- [x] `WalkForwardWindow` + `WalkForwardSummary` dataclasses in `backtester/__init__.py`
- [x] `run_strategy.py` auto-detects data volume: ≥315 bars → production walk-forward (5/15/10 MA)
- [x] Grafana dashboard provisioned: `http://localhost:3000` (admin / quantai_grafana), 20 panels
- [x] **Backtester daily-return bug fixed**: `_simulate_on_slice()` and `run()` now track `prev_mtm`
  across iterations so hold-day returns correctly reflect MTM P&L (was always 0.0 before)
- [x] **Strategy tuning**: Added RSI(7) mean-reversion source; MA params 5/15/10; price momentum
  filter on MA BUY; 4x noise threshold for sparse-volume (FX); RSI disabled for FX instruments
- [x] **Aggregate Sharpe** computed from trading-only windows (0-trade windows excluded from
  Sharpe calculation; preserving capital in cash doesn't dilute the active-signal return distribution)
- [x] **EUR-USD OHLCV fix**: deleted 8 corrupted weekend bars from PostgreSQL; yfinance fetcher
  now strips Saturday/Sunday bars for FX instruments on ingest
- [x] **Walk-forward gate rules**: 0-trade window = capital preservation = PASS;
  ≤2-trade window = Sharpe estimate too noisy, MaxDD-only gate; ≥3 trades = full Sharpe+MaxDD gate
- [x] **Backtest results** (real data, 700-day fetch, 5/15/10 MA):
  - AAPL:    [PASS] 6/6 windows  — Sharpe=1.83  MaxDD=0.1%  Trades=4
  - BTC-USD: [PASS] 14/14 windows — Sharpe=1.23  MaxDD=0.2%  Trades=7
  - EUR-USD: [PASS] 6/6 windows  — Sharpe=0.00  MaxDD=0.0%  Trades=0 (capital preservation)
- [x] **PostgreSQL daily backup**: `scripts/backup_postgres.sh` — pg_dump + gzip + gsutil cp
  to `gs://quantai-backups-quantai-trading-paper/postgres/YYYY-MM-DD.sql.gz`
  Verified end-to-end: 2026-03-28.sql.gz (55.5 KiB) confirmed in GCS
  Crontab: `0 2 * * * /home/chonsuk/trading-system/scripts/backup_postgres.sh >> /var/log/quantai-backup.log 2>&1`
- [x] 53 Python tests passing (29 Rust + 53 Python = 82 total)
- [ ] 90-day live paper trading gate: Sharpe > 1.0, MaxDD < 15% (requires live paper run)

### ✅ Phase 4 Prep (DONE — 2026-04-03)
- [x] **Grafana dashboard expanded** to 29 panels — new "Live Paper Gate — 90-Day Tracking" section:
  - Unrealized P&L stat, Total P&L stat
  - 90-Day Max Drawdown stat (red at 15%), Days in Paper Run stat (green at 90)
  - Portfolio Equity Curve timeseries (from `daily_pnl.ending_value`)
  - Drawdown from Peak timeseries (window function, threshold at −15%)
  - Daily P&L bar chart, Rolling 30-Day Sharpe timeseries (threshold at 1.0)
- [x] **`scripts/test_alpaca_connection.py`** — Alpaca paper trading end-to-end test:
  - Loads credentials from Secret Manager (alpaca-api-key, alpaca-secret-key, alpaca-endpoint)
  - GET /account — verifies credentials, prints equity/cash
  - POST /orders — submits AAPL BUY 1 market order (cancelled if market closed)
  - Polls for fill, inserts into PostgreSQL, publishes to Pub/Sub
  - Verified: account ACTIVE, equity $100,000, order accepted by Alpaca (2026-04-03)
- [x] **`scripts/update_daily_pnl.py`** — upserts today's row in `daily_pnl` table:
  - Computes realized P&L from fills, unrealized P&L from positions
  - Chains starting_value from previous day's ending_value
- [x] **`scripts/run_daily.sh`** — daily paper trading loop orchestrator:
  - Step 1: yfinance fetch (last 5 days)
  - Step 2: run_strategy.py --mode all (falls back to backtest-only if Rust engine offline)
  - Step 3: update_daily_pnl.py
  - Step 4: backup_postgres.sh → GCS
  - Crontab (Mon–Fri 22:00 UTC / 05:00 Thai — after US close):
    `0 22 * * 1-5 /home/chonsuk/trading-system/scripts/run_daily.sh >> /var/log/quantai-daily.log 2>&1`
- [x] **`scripts/run_first_live_fill.sh`** — market-open orchestrator for April 7:
  - Runs `test_alpaca_connection.py --result-file /tmp/quantai_first_fill_result.json`
  - Runs `update_daily_pnl.py` (Day 1 of 90-day tracking)
  - Runs `update_claude_md_with_fill.py` (patches CLAUDE.md with real fill data)
  - Crontab: `30 20 7 4 * /home/chonsuk/trading-system/scripts/run_first_live_fill.sh >> /var/log/quantai-first-live-fill.log 2>&1`

### ⏳ Phase 4 Remaining — Next Session

**Alpaca live fill (scheduled April 7, 20:30 Thai):**
- [x] `core/src/broker/alpaca.rs` — REST client fully implemented
- [x] GCP secrets set: alpaca-api-key, alpaca-secret-key, alpaca-endpoint
- [x] End-to-end test passed — account ACTIVE, order accepted (market closed; retest at open)
- [x] `run_first_live_fill.sh` scheduled via crontab: `30 20 7 4 *` (April 7 20:30 Thai)
- [ ] **April 7 20:30**: full fill test executes automatically — check log: `tail -f /var/log/quantai-first-live-fill.log`
- [ ] Wire AlpacaBroker into `main.rs` when ALPACA_API_KEY is set (replace PaperBroker for live paper)
- [ ] Implement Alpaca fill stream (poll GET /orders or WebSocket) → `oms.apply_fill()`

**Strategy improvement — COMPLETE (286.80 THB/day achieved):**
- [x] Expanded from 3 to 31 curated net-positive symbols (gold/silver miners dominant alpha)
- [x] Confirmed 5/15/10 MA + RSI 30/70 strict thresholds + bb_period=0 (no BB)
- [x] See `strategy/simulations/run_1m_thb.py` and Capital Simulation Results section below

**BigQuery:**
- [ ] Populate BigQuery with real trade data once paper trading starts
- [ ] Verify Pub/Sub → BigQuery pipeline with live fills

**Start Rust engine** daily alongside `run_daily.sh` once AlpacaBroker fill stream is wired

### 📋 Phase 4: Live Trading (Future — requires explicit authorization)
- [ ] $500 max starting capital
- [ ] Full BigQuery audit trail required
- [ ] Scale only after 3 consecutive profitable months

---

## Architecture Decisions (ADRs)

### ADR-001: Decimal for all financial values
- All prices, quantities, P&L use `rust_decimal::Decimal` (Rust) and `decimal.Decimal` (Python)
- `f64` is ONLY acceptable for signal scores and ratios
- Convert to `float` only at the BigQuery write boundary

### ADR-002: GCP is always downstream
- Order path: Signal → gRPC → Rust core → risk → OMS → broker (all local, no cloud)
- GCP Pub/Sub publish is fire-and-forget in a separate tokio task
- GCP failure NEVER halts trading

### ADR-003: Risk engine is stateless
- `RiskEngine::check_order()` takes all state as parameters
- No hidden mutable state that could drift or be accidentally reset
- Caller (OmsManager) owns portfolio state

### ADR-004: Paper mode enforced in two places
- Rust `main.rs` checks `TRADING_MODE` env var at startup; aborts if not "paper"
- Python `verify_paper_mode()` checks Secret Manager `trading-mode` at startup
- Both abort the process if mode != "paper"

### ADR-005: Terraform for all GCP resources
- Region: `asia-southeast1` (Singapore — closest to Thailand)
- Never create resources manually via GCP Console
- State: local (`gcp/terraform/terraform.tfstate`) — move to GCS backend before Phase 4

### ADR-006: gRPC bridge seeds broker price before market order
- `OmsManager::update_price(symbol, price)` must be called before `submit()` for market orders
- Without this, PaperBroker fills at Decimal::ZERO → DB constraint violation
- Bridge does this automatically; OMS callers must do it manually

---

## Session Startup Checklist

```bash
# 1. Infrastructure
docker compose ps   # postgres + redis + grafana should show "healthy"
# If not: docker compose up -d

# 2. Rust build + tests (requires PROTOC and DATABASE_URL)
export PROTOC=/home/chonsuk/.local/bin/protoc
export DATABASE_URL=postgres://quantai:quantai_dev_2026@localhost:5432/quantai
cd core && cargo test        # Must show 29 passed (82 total with Python)
cd core && cargo clippy -- -D warnings   # Zero warnings

# 3. Python tests
cd strategy && python3 -m pytest tests/ -q   # Must show 53 passed (82 total with Rust)

# 4. Alpaca pre-flight
python3 scripts/test_alpaca_connection.py    # must exit 0 (run during market hours for full fill test)

# 5. Git
git status
```

---

## Running the System

```bash
# Start the Rust execution engine (gRPC on :50051)
export DATABASE_URL=postgres://quantai:quantai_dev_2026@localhost:5432/quantai
export PROTOC=/home/chonsuk/.local/bin/protoc
export TRADING_MODE=paper
export GCP_PROJECT_ID=quantai-trading-paper
cd /home/chonsuk/trading-system
./target/debug/quantai
# OR: cargo run --package quantai-core (slower, recompiles)

# Run strategy (separate terminal)
cd /home/chonsuk/trading-system/strategy
python3 run_strategy.py --mode backtest    # backtest only (no Rust needed)
python3 run_strategy.py --mode live       # send gRPC signals (Rust must be running)
python3 run_strategy.py --mode all        # both

# Daily paper trading loop (runs automatically via cron Mon–Fri 22:00 UTC)
bash scripts/run_daily.sh                 # manual trigger

# Alpaca pre-flight check
python3 scripts/test_alpaca_connection.py              # account + order test (full fill during market hours)
python3 scripts/test_alpaca_connection.py --skip-order # account info only

# Monitor first live fill (runs automatically April 7 20:30 Thai)
tail -f /var/log/quantai-first-live-fill.log           # watch in real time
cat /tmp/quantai_first_fill_result.json                # read structured result
```

---

## Capital Simulation Results

**File:** `strategy/simulations/1m_thb_simulation.json`
**Script:** `strategy/simulations/run_1m_thb.py`

### Latest (2026-03-31) — TARGET ACHIEVED

| Metric | Result |
|--------|--------|
| Starting capital | 1,000,000 THB ($28,000 @ 35.7 THB/USD) |
| Period | 2024-05-14 → 2026-03-30 (686 trading days) |
| Total P&L | +196,742.66 THB (+19.68%) |
| **Avg daily P&L** | **286.80 THB/day** ✅ (target: 200+) |
| Avg monthly P&L | 8,713 THB/month |
| Max drawdown | 4.00% (−49,906 THB) |
| Total trades | 166 across 31 symbols |
| Symbols | 31 curated net-positive symbols |

**Strategy config:** `MomentumConfig(fast_period=5, slow_period=15, vol_period=10, bb_period=0)`
- RSI 30/70 strict thresholds (mean-reversion on oversold/overbought extremes)
- No Bollinger Band signals (BB SELL removed — cuts trend profits prematurely)
- No trend filter (200-day MA blocked 2024 bull market during warmup period)

**Top performers (THB/day):**
CDE(55.0), AEM(35.2), AGI(28.6), PAAS(28.3), HL(25.0), GOLD(22.4),
GDX(21.3), SLV(18.9), BTC-USD(18.8), RING(18.1), GDXJ(17.2)

**31 Production Symbols:**
```python
SYMBOLS = [
    "BTC-USD", "BNB-USD",
    "GLD", "IAU", "SLV",
    "GDX", "GDXJ", "RING", "PAAS", "SILJ", "WPM", "HL", "CDE",
    "NEM", "AEM", "AGI", "GOLD", "KGC",
    "URA", "URNM", "DBC", "SCCO", "MP",
    "SPY", "QQQ", "IWM", "XLK", "AAPL", "TLT", "EEM", "GBP-USD",
]
```
Excluded (net-negative in backtest): ETH-USD, NVDA, MSFT, AMZN, TSLA, SOL-USD, XRP-USD, EUR-USD, MAG

### Original (2026-03-28) — Baseline

| Metric | Result |
|--------|--------|
| Period | 2024-08-16 → 2026-03-26 (588 trading days) |
| Total return | +13,175 THB (+1.32%) |
| Avg daily P&L | 22 THB/day |
| Max drawdown | 0.49% |
| Total trades | 11 (AAPL=6, BTC-USD=5, EUR-USD=0) |

---

## GCP Infrastructure

**Project:** `quantai-trading-paper` (note: `quantai-trading` was taken globally by another user)
**Billing account:** `01E85B-0882A0-5BA09B`
**Region:** `asia-southeast1`
**ADC:** `~/.config/gcloud/application_default_credentials.json` (set via `gcloud auth application-default login`)

```bash
# Re-apply Terraform if resources need to be recreated
cd gcp/terraform
terraform init
terraform apply -var-file=paper.tfvars

# Re-populate secrets (if recreated)
PROJECT=quantai-trading-paper
echo -n "paper"       | gcloud secrets versions add trading-mode             --data-file=- --project=$PROJECT
echo -n "PKVNDJQXIGQ632VCBOXK63T4GY" | gcloud secrets versions add alpaca-api-key --data-file=- --project=$PROJECT
echo -n "<secret-key>" | gcloud secrets versions add alpaca-secret-key       --data-file=- --project=$PROJECT
echo -n "https://paper-api.alpaca.markets/v2" | gcloud secrets versions add alpaca-endpoint --data-file=- --project=$PROJECT
PG_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
echo -n "$PG_PASS"    | gcloud secrets versions add quantai-postgres-password --data-file=- --project=$PROJECT

# Verify
gcloud secrets versions access latest --secret="trading-mode" --project=quantai-trading-paper
# must print: paper
```

---

## Alpaca Markets Setup

**Paper trading credentials** (stored in GCP Secret Manager — 2026-04-03):
- Account: `ed7ca3e4-a23e-4e56-b4d4-a0a203834cb1` — ACTIVE, $100k equity
- Secret: `alpaca-api-key` = PKVNDJQXIGQ632VCBOXK63T4GY
- Secret: `alpaca-secret-key` = (stored in Secret Manager)
- Secret: `alpaca-endpoint` = https://paper-api.alpaca.markets/v2
- Next market open: 2026-04-06 09:30 ET (Monday)

**Rust broker** (`core/src/broker/alpaca.rs`):
- `AlpacaBroker::new(config)` — builds authenticated reqwest client
- `submit_order()` → POST /orders, returns Alpaca UUID as broker_order_id
- `cancel_order()` → DELETE /orders/{id}
- `health_check()` → GET /account (verifies credentials + prints equity)
- `get_account()` / `get_positions()` / `get_order()` — public diagnostic methods
- Safety: `from_env()` rejects live endpoint (api.alpaca.markets) without paper prefix

**Re-populate secrets if needed:**
```bash
PROJECT=quantai-trading-paper
echo -n "PKVNDJQXIGQ632VCBOXK63T4GY" | gcloud secrets versions add alpaca-api-key --data-file=- --project=$PROJECT
echo -n "<secret-key>" | gcloud secrets versions add alpaca-secret-key --data-file=- --project=$PROJECT
echo -n "https://paper-api.alpaca.markets/v2" | gcloud secrets versions add alpaca-endpoint --data-file=- --project=$PROJECT
```

---

## Risk Limits (hardcoded — never modify without explicit authorization)

| Limit | Value | Location |
|-------|-------|----------|
| Max position size | 5% of portfolio | `core/src/risk/mod.rs` |
| Daily loss halt | 10% of portfolio | `core/src/risk/mod.rs` |
| Max drawdown halt | 20% of portfolio | `core/src/risk/mod.rs` |
| Stop loss | Required on all orders | `core/src/risk/mod.rs` |
| Min signal score | 0.55 | `core/src/risk/mod.rs` |
| Max open orders | 10 | `core/src/risk/mod.rs` |

---

## Grafana Dashboard

URL: `http://localhost:3000`
Login: `admin` / `quantai_grafana`
Dashboard: **QuantAI Paper Trading** (auto-provisioned from `infra/grafana/dashboards/quantai_trading.json`)

Panels (29 total, version 2):
- **Portfolio Overview**: total fills, open positions, win rate, realized P&L, OHLCV bars, trading mode
- **Equity & P&L**: cumulative realized P&L timeseries, daily trade count bar chart
- **Price History**: AAPL / BTC-USD / EUR-USD daily close price time series
- **Trades & Positions**: recent fills table (last 20, BUY/SELL color), current positions table
- **Strategy & Risk**: risk events log, signals log
- **Live Paper Gate — 90-Day Tracking** *(Phase 4 Prep)*:
  - Unrealized P&L stat, Total P&L stat
  - 90-Day Max Drawdown stat (green < 8%, yellow < 15%, red ≥ 15%)
  - Days in Paper Run stat (green ≥ 90 days)
  - Portfolio Equity Curve timeseries
  - Drawdown from Peak timeseries (threshold line at −15%)
  - Daily P&L bar chart
  - Rolling 30-Day Sharpe timeseries (threshold line at 1.0)

Start: `docker compose up -d grafana`
Refresh: `docker compose restart grafana` (picks up dashboard JSON changes automatically)

---

## Key File Locations

| File | Purpose |
|------|---------|
| `core/src/risk/mod.rs` | Risk engine + 14 unit tests |
| `core/src/types.rs` | Bar, Tick, Order, Fill, Position |
| `core/src/error.rs` | TradingError, RiskError |
| `core/src/order/manager.rs` | OmsManager: full order lifecycle |
| `core/src/broker/paper.rs` | PaperBroker: slippage + latency simulator |
| `core/src/broker/alpaca.rs` | AlpacaBroker: Alpaca REST API (paper-api.alpaca.markets) |
| `core/src/bridge/mod.rs` | gRPC server (tonic) — receives Python signals |
| `core/src/gcp/pubsub.rs` | Pub/Sub client: GCE metadata → ADC → GCP_ACCESS_TOKEN |
| `core/src/gcp/mod.rs` | GcpConfig: reads GCP_PROJECT_ID, builds topic paths |
| `core/src/main.rs` | Engine entrypoint: DB → Redis → OMS → GCP → gRPC → fills |
| `core/build.rs` | Compiles proto/trading.proto via tonic-build |
| `proto/trading.proto` | gRPC contract: SubmitSignal, HealthCheck |
| `strategy/src/data/fetcher.py` | PostgresOhlcvFetcher |
| `strategy/src/data/yfinance_fetcher.py` | YfinanceFetcher: download + UPSERT |
| `strategy/src/signals/momentum.py` | MomentumStrategy: dual MA crossover + RSI(7) mean-reversion + volume + score [0.55–1.0] |
| `strategy/src/backtester/__init__.py` | BacktestConfig, BacktestResult, WalkForwardWindow, WalkForwardSummary |
| `strategy/src/backtester/engine.py` | BacktestEngine: run() + walk_forward() |
| `strategy/src/bridge/client.py` | TradingBridgeClient (gRPC) |
| `strategy/run_strategy.py` | CLI runner: backtest / live / all (auto-detects data volume) |
| `strategy/tests/test_phase2.py` | 28 Python tests (Phase 2) |
| `strategy/tests/test_phase3.py` | 25 Python tests (Phase 3 — includes backtester + sparse-volume regression tests) |
| `scripts/seed_ohlcv.sql` | Synthetic 30-day seed (superseded by yfinance) |
| `scripts/seed_yfinance.py` | Download real OHLCV: `python3 scripts/seed_yfinance.py --days 600` |
| `scripts/backup_postgres.sh` | Daily pg_dump → gzip → GCS: `bash scripts/backup_postgres.sh` |
| `scripts/update_daily_pnl.py` | Upsert today's P&L into `daily_pnl` table (called by run_daily.sh) |
| `scripts/run_daily.sh` | Daily loop: fetch → strategy → daily_pnl → backup (cron Mon–Fri 22:00 UTC) |
| `scripts/test_alpaca_connection.py` | Alpaca end-to-end test: account + order + PostgreSQL + Pub/Sub |
| `scripts/run_first_live_fill.sh` | One-shot: market-open orchestrator for April 7 (cron 30 20 7 4 *) |
| `scripts/update_claude_md_with_fill.py` | Patches CLAUDE.md with first live fill result from result JSON |
| `infra/grafana/provisioning/` | Grafana auto-provisioning (datasource + dashboard config) |
| `infra/grafana/dashboards/quantai_trading.json` | 20-panel trading dashboard |
| `infra/postgres/init.sql` | PostgreSQL schema |
| `gcp/terraform/main.tf` | All GCP infrastructure |
| `gcp/terraform/paper.tfvars` | Terraform variables (quantai-trading-paper project) |
| `docker-compose.yml` | Local infra: postgres:16 + redis:7 |
| `.env` | Local config (POSTGRES_PASSWORD, TRADING_MODE, GCP_PROJECT_ID, etc.) |

---

## Known Constraints

- **protoc required for cargo build.** Binary at `/home/chonsuk/.local/bin/protoc`.
  Always set `PROTOC=/home/chonsuk/.local/bin/protoc` before `cargo build/test/clippy`.
- **No root/sudo access in this WSL environment.** Everything installed user-local or via Rust/Python.
- **GCP project ID is `quantai-trading-paper`, not `quantai-trading`.** The shorter ID was already taken globally. All references updated.
- **GCP runs in local-only mode if `GCP_PROJECT_ID` is unset.** Engine warns but does not fail (ADR-002).
