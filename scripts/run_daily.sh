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

log "═══════════════════════════════════════════════════════"
log " QuantAI daily run — ${TIMESTAMP}"
log "═══════════════════════════════════════════════════════"

# ── Step 0: System health snapshot (pre-run) ──────────────────────────────────
log "Step 0: Logging system health snapshot (pre-run)…"
python3 "${SCRIPT_DIR}/log_system_health.py" --skip-alpaca || true
log "Step 0: Health snapshot done."

# ── Step 1: Fetch latest OHLCV + refresh planner stats ────────────────────────
# ALPACA_FETCHER=1  → use Alpaca Markets Data API (required on Cloud Run — Yahoo Finance blocks GCP IPs)
# ALPACA_FETCHER=0  → use yfinance (default for local WSL dev)
if [[ "${ALPACA_FETCHER:-0}" == "1" ]]; then
    log "Step 1/4: Fetching latest OHLCV from Alpaca Markets (last 7 days)…"
    python3 "${SCRIPT_DIR}/seed_alpaca.py" --days 7
else
    log "Step 1/4: Fetching latest OHLCV from yfinance (last 5 days)…"
    python3 "${SCRIPT_DIR}/seed_yfinance.py" --days 5
fi
PGPASSWORD=quantai_dev_2026 psql -h localhost -U quantai -d quantai -q \
    -c "ANALYZE ohlcv;" 2>/dev/null || true
log "Step 1/4: OHLCV fetch complete."

# ── Step 2: Run strategy ──────────────────────────────────────────────────────
log "Step 2/4: Running strategy (backtest + live signal)…"
cd "${STRATEGY_DIR}"

# Try --mode live (requires Rust OMS on :50051); fall back to backtest-only.
if python3 run_strategy.py --mode live 2>/dev/null; then
    python3 run_strategy.py --mode backtest
    log "Step 2/4: Strategy run complete (live + backtest)."
else
    log "Step 2/4: Rust OMS not reachable — running backtest only (non-fatal)."
    python3 run_strategy.py --mode backtest
fi

cd "${ROOT}"

# ── Step 3: Update daily P&L ──────────────────────────────────────────────────
log "Step 3/4: Updating daily_pnl table…"
python3 "${SCRIPT_DIR}/update_daily_pnl.py"
log "Step 3/4: daily_pnl updated."

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
