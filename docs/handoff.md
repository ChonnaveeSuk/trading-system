# QuantAI Trading System — Machine Handoff
# Written: 2026-04-15 (WSL machine retirement)

## GCP Resources — Live and Autonomous

Everything runs on GCP. No local machine is required for the system to keep trading.

### Cloud Run Jobs (authoritative schedulers)

| Job | Schedule (UTC) | Purpose |
|-----|---------------|---------|
| `quantai-daily-runner` | Mon–Fri 22:00 | yfinance fetch → strategy → daily_pnl → backup |
| `quantai-backup` | Daily 02:00 | pg_dump → GCS |

Both jobs are in region `asia-southeast1`, project `quantai-trading-paper`.

Last confirmed execution: `quantai-daily-runner` ran successfully 2026-04-14T22:00 UTC, exit(0).

### Cloud SQL

- **Instance:** `quantai-trading-paper:asia-southeast1:quantai-postgres`
- **Tier:** `db-f1-micro` (~$9–10/month)
- **Public IP:** 35.198.246.139
- **State:** RUNNABLE
- **Auth:** Cloud SQL Auth Proxy (Unix socket in Cloud Run); password in Secret Manager `cloud-sql-quantai-password`

### GCS Backups

Bucket: `gs://quantai-backups-quantai-trading-paper/postgres/`

Latest backup confirmed: `2026-04-15.sql.gz` (runs daily at 02:00 UTC).

### Artifact Registry

- Repo: `quantai` (DOCKER) in `asia-southeast1`
- Image: `asia-southeast1-docker.pkg.dev/quantai-trading-paper/quantai/runner:latest`
- Auto-updated on push to `main` via GitHub Actions (`.github/workflows/deploy.yml`)

---

## GCP Secret Manager — Secret Names

All secrets are in project `quantai-trading-paper`. **Values are never stored here.**

| Secret Name | Purpose |
|-------------|---------|
| `alpaca-api-key` | Alpaca paper trading API key |
| `alpaca-secret-key` | Alpaca paper trading secret key |
| `alpaca-endpoint` | `https://paper-api.alpaca.markets/v2` |
| `trading-mode` | Must be `paper` (enforced at startup) |
| `cloud-sql-quantai-password` | Cloud SQL PostgreSQL password |
| `database-url` | Full PostgreSQL connection string for Cloud Run |
| `quantai-postgres-password` | Legacy local dev password |
| `ibkr-account-id` | IBKR paper account (inactive — Alpaca is live) |
| `ibkr-paper-host` | IBKR TWS host (inactive) |
| `ibkr-paper-port` | IBKR TWS port (inactive) |

---

## GCP Configuration

- **Project ID:** `quantai-trading-paper` (not `quantai-trading` — that ID was taken globally)
- **Billing account:** `01E85B-0882A0-5BA09B`
- **Region:** `asia-southeast1` (Singapore)
- **GCP account:** `chonnaveesukyao@gmail.com`
- **Terraform state:** local at `gcp/terraform/terraform.tfstate` — do not delete this file
- **ADC (Application Default Credentials):** must re-run `gcloud auth application-default login` on new machine

---

## Strategy — Final Metrics (2026-04-15)

| Metric | Value |
|--------|-------|
| Sharpe ratio | 1.61 |
| Max drawdown | 8.86% |
| Avg daily P&L (backtest) | ~992 THB/day |
| Total tests | 138/138 passing (46 Rust + 92 Python) |
| Symbols | 31 curated (gold/silver miners dominant) |
| Strategy | MA 5/15 + RSI 30/70 + ATR sizing 2.0x |
| Paper run gate | Sharpe > 1.0, MaxDD < 15% over 90 days |

---

## Resuming on a New Machine — Day 1 Commands

```bash
# ── 1. Prerequisites ──────────────────────────────────────────────────────────
# Install: rustup, python3, docker, protoc, gcloud CLI
# protoc must be at: /home/<user>/.local/bin/protoc  (or update PROTOC env var)

# ── 2. Clone repo ─────────────────────────────────────────────────────────────
git clone https://github.com/<your-org>/trading-system.git
cd trading-system

# ── 3. GCP auth ───────────────────────────────────────────────────────────────
gcloud auth login
gcloud auth application-default login
gcloud config set project quantai-trading-paper

# ── 4. Environment ────────────────────────────────────────────────────────────
cp .env.example .env
# Edit .env: set POSTGRES_PASSWORD, TRADING_MODE=paper, GCP_PROJECT_ID=quantai-trading-paper

# ── 5. Local dev infrastructure (optional — for local tests only) ─────────────
docker compose up -d          # postgres:16 + redis:7 + grafana
# Wait ~10s then verify:
docker compose ps             # all three should show "healthy"

# ── 6. Build + test ───────────────────────────────────────────────────────────
export PROTOC=/home/<user>/.local/bin/protoc
export DATABASE_URL=postgres://quantai:quantai_dev_2026@localhost:5432/quantai
cd core && cargo test         # must show 46 passed
cd ../strategy && python3 -m pytest tests/ -q  # must show 92 passed

# ── 7. Verify GCP is still running ────────────────────────────────────────────
gcloud run jobs list --region asia-southeast1 --project quantai-trading-paper
gcloud logging read 'resource.type="cloud_run_job" resource.labels.job_name="quantai-daily-runner"' \
  --limit 5 --project quantai-trading-paper --format="value(timestamp,textPayload)"

# ── 8. Alpaca pre-flight ──────────────────────────────────────────────────────
python3 scripts/test_alpaca_connection.py --skip-order   # verify account active

# ── 9. Check GCS backups ─────────────────────────────────────────────────────
gsutil ls "gs://quantai-backups-quantai-trading-paper/postgres/" | tail -5
```

---

## What Keeps Running Without This Machine

The following happen automatically via GCP Cloud Scheduler + Cloud Run Jobs:

1. **Mon–Fri 22:00 UTC** — `quantai-daily-runner` fires:
   - Downloads last 5 days of OHLCV via Alpaca (iex feed)
   - Runs momentum strategy across 31 symbols
   - Submits signals via gRPC → Alpaca paper orders
   - Updates `daily_pnl` table in Cloud SQL
   - Pushes fills to Pub/Sub → BigQuery

2. **Daily 02:00 UTC** — `quantai-backup` fires:
   - pg_dump of Cloud SQL → gzip → `gs://quantai-backups-quantai-trading-paper/postgres/YYYY-MM-DD.sql.gz`
   - 90-day retention lifecycle policy on GCS

3. **GitHub push to main** — GitHub Actions:
   - Builds Docker image → Artifact Registry
   - Updates both Cloud Run jobs to new image

---

## docker-compose.yml — Confirmed Working (2026-04-15)

Services (local dev only — NOT required for GCP operation):
- `postgres:16-alpine` on port 5432
- `redis:7-alpine` on port 6379
- `grafana:11.4.0` on port 3000 (admin / quantai_grafana)

---

## Terraform

State is stored locally at `gcp/terraform/terraform.tfstate`. If the new machine needs to
manage GCP infrastructure:

```bash
cd gcp/terraform
terraform init
terraform plan -var-file=paper.tfvars   # review before apply
```

Do NOT run `terraform apply` unless you need to change infrastructure — resources are live.
