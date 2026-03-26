# QuantAI Trading System — Project State
# Read this at the start of every session.

## Current Status

**Phase:** 0 — Foundation (COMPLETE)
**Mode:** PAPER TRADING ONLY
**Last updated:** 2026-03-26

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

### 🔄 Phase 1: Broker + GCP Foundation (NEXT)
Local execution:
- [ ] `core/src/broker/paper.rs` — Paper trading simulator with slippage + latency model
- [ ] `core/src/broker/ibkr.rs` — IBKR TWS connection on port 7497
- [ ] `core/src/market_data/feed.rs` — Real-time tick feed → Redis
- [ ] `core/src/order/manager.rs` — Full order lifecycle + PostgreSQL logging

GCP setup:
- [ ] Create GCP project, run `terraform apply -var-file=paper.tfvars`
- [ ] Populate secrets in Secret Manager (see "First Run" below)
- [ ] `core/src/gcp/pubsub.rs` — Async Pub/Sub publisher (fills + ticks)
- [ ] `strategy/src/gcp/bigquery.py` — Stream trades to BigQuery

### 📋 Phase 2: Strategy + Vertex AI
- [ ] Python IBKR historical data fetcher
- [ ] Feature engineering pipeline (momentum, volatility, volume)
- [ ] Momentum strategy backtest (walk-forward, 252-day minimum)
- [ ] gRPC bridge proto + tonic server
- [ ] Vertex AI first ML training pipeline
- [ ] Cloud Run: deploy Python strategy service

### 📋 Phase 3: Monitoring + 90-Day Validation
- [ ] Full paper trading loop (signal → risk → order → fill → log)
- [ ] Grafana local dashboard (PostgreSQL datasource)
- [ ] Looker Studio dashboard (BigQuery datasource)
- [ ] GCS automated PostgreSQL daily backup
- [ ] 90-day paper trading gate: Sharpe > 1.0, MaxDD < 15%

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
- Order path: IBKR → Rust core → risk → OMS → broker (all local, no cloud)
- GCP Pub/Sub publish is fire-and-forget in a separate tokio task
- GCP failure NEVER halts trading

### ADR-003: Risk engine is stateless
- `RiskEngine::check_order()` takes all state as parameters
- No hidden mutable state that could drift or be accidentally reset
- Caller (OrderManager) owns portfolio state

### ADR-004: Paper mode enforced in two places
- Rust `main.rs` checks `TRADING_MODE` env var / Secret Manager at startup
- Python `verify_paper_mode()` checks Secret Manager `trading-mode` at startup
- Both abort the process if mode != "paper"

### ADR-005: Terraform for all GCP resources
- Region: `asia-southeast1` (Singapore — closest to Thailand)
- Never create resources manually via GCP Console
- State: local for now, move to GCS backend before Phase 3

---

## Session Startup Checklist

```bash
# 1. Verify paper mode (after GCP is set up in Phase 1)
gcloud secrets versions access latest --secret="trading-mode" --project=$GCP_PROJECT_ID

# 2. Verify IBKR port
gcloud secrets versions access latest --secret="ibkr-paper-port" --project=$GCP_PROJECT_ID

# 3. Infrastructure
docker-compose ps   # postgres + redis should show "healthy"

# 4. Rust checks
cd core && cargo test        # All risk engine tests must pass
cd core && cargo clippy      # Zero warnings policy

# 5. Git
git status
```

---

## First Run (Phase 1 — GCP Setup)

```bash
# 1. Create GCP project and authenticate
gcloud auth login
gcloud auth application-default login

# 2. Copy and fill terraform vars
cp gcp/terraform/paper.tfvars.example gcp/terraform/paper.tfvars
# Edit paper.tfvars with your project_id, email, etc.

# 3. Apply infrastructure
cd gcp/terraform
terraform init
terraform apply -var-file=paper.tfvars

# 4. Populate secrets (run once)
echo -n "paper" | gcloud secrets versions add trading-mode --data-file=- --project=$GCP_PROJECT_ID
echo -n "7497"  | gcloud secrets versions add ibkr-paper-port --data-file=- --project=$GCP_PROJECT_ID
# Add postgres-password and ibkr-account-id similarly

# 5. Start local infrastructure
cp .env.example .env
# Edit .env — set POSTGRES_PASSWORD
docker-compose up -d
docker-compose ps   # Both services should be "healthy"

# 6. Build Rust core
cargo build
cargo test   # Should show 14 passing tests
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

## Key File Locations

| File | Purpose |
|------|---------|
| `core/src/risk/mod.rs` | Risk engine + all unit tests |
| `core/src/types.rs` | Bar, Tick, Order, Fill, Position |
| `core/src/error.rs` | TradingError, RiskError |
| `infra/postgres/init.sql` | Hot DB schema |
| `gcp/terraform/main.tf` | All GCP infrastructure |
| `gcp/bigquery/schema/` | BigQuery table schemas |
| `strategy/src/signals/__init__.py` | Signal output type |
| `strategy/src/gcp/__init__.py` | Secret Manager + paper mode check |
| `docker-compose.yml` | Local infra (postgres + redis) |
| `.env.example` | Config template |
