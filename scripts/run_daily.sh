#!/usr/bin/env bash
# trading-system/scripts/run_daily.sh
#
# Daily paper trading loop — runs once per trading day.
#
# Steps:
#   1. Fetch latest OHLCV — Alpaca if ALPACA_FETCHER=1 (Cloud Run), else yfinance (local)
#   2. Run strategy: backtest walk-forward + live signal → Rust OMS
#      If Rust engine is not running, falls back to backtest-only (no error)
#   3. Update daily_pnl table with today's P&L and trade count
#   4. Run PostgreSQL backup to GCS
#
# Cron (Monday–Friday at 22:00 UTC / 05:00 Thai time — after US market close):
#   0 22 * * 1-5 /home/chonsuk/trading-system/scripts/run_daily.sh >> /var/log/quantai-daily.log 2>&1
#
# Manual run:
#   bash scripts/run_daily.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
STRATEGY_DIR="${ROOT}/strategy"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

export DATABASE_URL="${DATABASE_URL:-postgres://quantai:quantai_dev_2026@localhost:5432/quantai}"
export TRADING_MODE="${TRADING_MODE:-paper}"
export GCP_PROJECT_ID="${GCP_PROJECT_ID:-quantai-trading-paper}"

log() { echo "[${TIMESTAMP}] $*"; }

# ── Cloud SQL lifecycle helpers ────────────────────────────────────────────────
# MANAGE_CLOUD_SQL=1: start instance before job, stop after via EXIT trap.
# Saves ~$7.55/month (Cloud Run Job runs ~10 min/day; SQL is paid per minute).
_sql_instance="quantai-postgres"
_sql_project="${GCP_PROJECT_ID:-quantai-trading-paper}"

_start_cloud_sql() {
    log "MANAGE_CLOUD_SQL: starting Cloud SQL instance ${_sql_instance}…"
    gcloud sql instances patch "${_sql_instance}" \
        --activation-policy ALWAYS \
        --project "${_sql_project}" \
        --quiet 2>&1 || true
    # Wait for instance to be RUNNABLE
    local deadline=$(( SECONDS + 600 ))
    while [[ $SECONDS -lt $deadline ]]; do
        local state
        state=$(gcloud sql instances describe "${_sql_instance}" \
            --project "${_sql_project}" \
            --format="value(state)" 2>/dev/null || echo "UNKNOWN")
        if [[ "${state}" == "RUNNABLE" ]]; then
            log "MANAGE_CLOUD_SQL: Cloud SQL is RUNNABLE — waiting for proxy socket…"
            break
        fi
        log "MANAGE_CLOUD_SQL: state=${state} — waiting 15s…"
        sleep 15
    done
    if [[ $SECONDS -ge $deadline ]]; then
        log "MANAGE_CLOUD_SQL: ERROR — Cloud SQL did not become RUNNABLE within 10 min"
        return 1
    fi
    # Wait for Cloud SQL Auth Proxy socket to accept connections (up to 2 min)
    local proxy_deadline=$(( SECONDS + 120 ))
    while [[ $SECONDS -lt $proxy_deadline ]]; do
        if python3 -c "
import psycopg2, os, sys
try:
    conn = psycopg2.connect(os.environ.get('DATABASE_URL', ''))
    conn.close()
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
            log "MANAGE_CLOUD_SQL: proxy socket is ready."
            return 0
        fi
        log "MANAGE_CLOUD_SQL: proxy socket not ready yet — waiting 10s…"
        sleep 10
    done
    log "MANAGE_CLOUD_SQL: ERROR — proxy socket not ready within 2 min after RUNNABLE"
    return 1
}

_stop_cloud_sql() {
    log "MANAGE_CLOUD_SQL: stopping Cloud SQL instance ${_sql_instance}…"
    gcloud sql instances patch "${_sql_instance}" \
        --activation-policy NEVER \
        --project "${_sql_project}" \
        --quiet 2>&1 || true
    log "MANAGE_CLOUD_SQL: stop command sent."
}

log "═══════════════════════════════════════════════════════"
log " QuantAI daily run — ${TIMESTAMP}"
log "═══════════════════════════════════════════════════════"

# ── Cloud SQL: start before any DB work, stop on exit ─────────────────────────
if [[ "${MANAGE_CLOUD_SQL:-0}" == "1" ]]; then
    _start_cloud_sql
    trap '_stop_cloud_sql' EXIT
fi

# ── Step 0: System health snapshot (pre-run) ──────────────────────────────────
log "Step 0: Logging system health snapshot (pre-run)…"
python3 "${SCRIPT_DIR}/log_system_health.py" --skip-alpaca || true
log "Step 0: Health snapshot done."

# ── Step 1: Fetch latest OHLCV + refresh planner stats ────────────────────────
# ALPACA_FETCHER=1  → use Alpaca Markets Data API (required on Cloud Run — Yahoo Finance blocks GCP IPs)
# ALPACA_FETCHER=0  → use yfinance (default for local WSL dev)
if [[ "${ALPACA_FETCHER:-0}" == "1" ]]; then
    log "Step 1/4: Fetching latest OHLCV from Alpaca Markets (last 7 days)…"
    # Non-fatal: individual symbol failures are logged but don't abort the run.
    # Strategy uses whatever data is already in the DB if a symbol fails.
    python3 "${SCRIPT_DIR}/seed_alpaca.py" --days 7 || \
        log "Step 1/4: OHLCV fetch had errors (non-fatal — using existing DB data)."
else
    log "Step 1/4: Fetching latest OHLCV from yfinance (last 5 days)…"
    python3 "${SCRIPT_DIR}/seed_yfinance.py" --days 5 || \
        log "Step 1/4: OHLCV fetch had errors (non-fatal — using existing DB data)."
fi
log "Step 1/4: OHLCV fetch complete."

# ── Step 1.5: Reconcile Alpaca fills from yesterday's orders ──────────────────
# Checks SUBMITTED orders against Alpaca, writes fills → PostgreSQL.
# Must run before update_daily_pnl.py so today's P&L includes overnight fills.
if [[ "${ALPACA_DIRECT:-0}" == "1" ]]; then
    log "Step 1.5/4: Reconciling Alpaca fills…"
    python3 "${SCRIPT_DIR}/reconcile_alpaca_fills.py" || true
    log "Step 1.5/4: Reconcile done."
fi

# ── Step 2: Run strategy ──────────────────────────────────────────────────────
log "Step 2/4: Running strategy (backtest + live signal)…"
cd "${STRATEGY_DIR}"

if [[ "${ALPACA_DIRECT:-0}" == "1" ]]; then
    # Cloud Run path: direct Alpaca REST (no Rust gRPC needed)
    python3 run_strategy.py --mode all
    log "Step 2/4: Strategy run complete (live + backtest via Alpaca direct)."
else
    # Local path: try gRPC → Rust OMS, fall back to backtest-only
    if python3 run_strategy.py --mode live 2>/dev/null; then
        python3 run_strategy.py --mode backtest
        log "Step 2/4: Strategy run complete (live + backtest)."
    else
        log "Step 2/4: Rust OMS not reachable — running backtest only (non-fatal)."
        python3 run_strategy.py --mode backtest
    fi
fi

cd "${ROOT}"

# ── Step 3: Update daily P&L ──────────────────────────────────────────────────
log "Step 3/4: Updating daily_pnl table…"
python3 "${SCRIPT_DIR}/update_daily_pnl.py"
log "Step 3/4: daily_pnl updated."

# ── Step 3.5: Send daily Telegram summary ─────────────────────────────────────
# Non-fatal: Telegram failure never aborts the daily run.
log "Step 3.5: Sending daily Telegram summary…"
python3 "${SCRIPT_DIR}/telegram_alert.py" --daily-summary || \
    log "Step 3.5: Telegram summary failed (non-fatal)."

# ── Step 4: PostgreSQL backup ─────────────────────────────────────────────────
log "Step 4/4: Running PostgreSQL backup to GCS…"
bash "${SCRIPT_DIR}/backup_postgres.sh"
log "Step 4/4: Backup complete."

# ── Step 5: System health snapshot (post-run) + cron marker ──────────────────
log "Step 5/5: Logging system health snapshot (post-run) and cron marker…"
python3 "${SCRIPT_DIR}/log_system_health.py" || true
log "Step 5/5: Health + cron marker done."

log "═══════════════════════════════════════════════════════"
log " Daily run complete."
log "═══════════════════════════════════════════════════════"
