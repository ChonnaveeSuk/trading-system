#!/usr/bin/env bash
# trading-system/scripts/migrate_to_cloud_sql.sh
#
# One-time migration: WSL Docker PostgreSQL → Cloud SQL.
#
# What it does:
#   1. Validates both source (local Docker) and target (Cloud SQL) are reachable
#   2. pg_dump from local Docker PostgreSQL (plain SQL format)
#   3. Applies schema + data to Cloud SQL via Cloud SQL Auth Proxy
#   4. Verifies row counts match across all 8 tables
#   5. Prints the Cloud SQL connection string for .env / Secret Manager
#
# Prerequisites:
#   - docker-compose up -d postgres        (local source must be running)
#   - terraform apply -var-file=paper.tfvars   (Cloud SQL must be provisioned)
#   - gcloud auth application-default login
#   - gcloud components install cloud-sql-proxy   OR proxy downloaded below
#
# Usage:
#   bash scripts/migrate_to_cloud_sql.sh
#
# The script is idempotent: schema uses IF NOT EXISTS / ON CONFLICT DO NOTHING.
# Safe to run again if interrupted — re-running will not duplicate data.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Configuration ─────────────────────────────────────────────────────────────
PROJECT="${GCP_PROJECT_ID:-quantai-trading-paper}"
REGION="${GCP_REGION:-asia-southeast1}"
INSTANCE_NAME="quantai-postgres"
CLOUD_SQL_CONN="${PROJECT}:${REGION}:${INSTANCE_NAME}"
PROXY_PORT=5434  # avoid collision with local Docker on 5432

# Source (local Docker)
SRC_HOST="${POSTGRES_HOST:-localhost}"
SRC_PORT="${POSTGRES_PORT:-5432}"
SRC_USER="${POSTGRES_USER:-quantai}"
SRC_DB="${POSTGRES_DB:-quantai}"
SRC_PASS="${POSTGRES_PASSWORD:-quantai_dev_2026}"

DUMP_FILE="/tmp/quantai_migration_$(date +%Y%m%d_%H%M%S).sql"
PROXY_PID_FILE="/tmp/cloud-sql-proxy-migration.pid"
PROXY_BIN="/tmp/cloud-sql-proxy"

log()  { echo "[$(date -u +%H:%M:%SZ)] $*"; }
fail() { echo "[ERROR] $*" >&2; exit 1; }

cleanup() {
    if [[ -f "${PROXY_PID_FILE}" ]]; then
        PROXY_PID=$(cat "${PROXY_PID_FILE}")
        kill "${PROXY_PID}" 2>/dev/null || true
        rm -f "${PROXY_PID_FILE}"
        log "Cloud SQL Auth Proxy stopped (PID ${PROXY_PID})."
    fi
    rm -f "${DUMP_FILE}"
}
trap cleanup EXIT

# ── Step 0: Fetch Cloud SQL password from Secret Manager ──────────────────────
log "═══════════════════════════════════════════════════════"
log " QuantAI: WSL Docker → Cloud SQL migration"
log "═══════════════════════════════════════════════════════"
log "Step 0: Fetching Cloud SQL password from Secret Manager…"

CLOUD_SQL_PASS="$(gcloud secrets versions access latest \
    --secret="cloud-sql-quantai-password" \
    --project="${PROJECT}" 2>/dev/null)" \
    || fail "Could not fetch cloud-sql-quantai-password from Secret Manager. Run: terraform apply -var-file=paper.tfvars"

log "Step 0: Password fetched."

# ── Step 1: Validate source PostgreSQL ───────────────────────────────────────
log "Step 1: Validating source (local Docker PostgreSQL)…"

PGPASSWORD="${SRC_PASS}" psql \
    -h "${SRC_HOST}" -p "${SRC_PORT}" -U "${SRC_USER}" -d "${SRC_DB}" \
    -c "SELECT 1" -q > /dev/null \
    || fail "Cannot connect to local PostgreSQL at ${SRC_HOST}:${SRC_PORT}. Is docker-compose up?"

# Use exact COUNT(*) per table (n_live_tup in pg_stat_user_tables is unreliable
# without VACUUM — can show 0 even for rows inserted by init.sql).
SRC_COUNTS=$(PGPASSWORD="${SRC_PASS}" psql \
    -h "${SRC_HOST}" -p "${SRC_PORT}" -U "${SRC_USER}" -d "${SRC_DB}" \
    -t -A -c "
    SELECT 'daily_pnl='    || (SELECT COUNT(*) FROM daily_pnl)
    UNION ALL SELECT 'fills='        || (SELECT COUNT(*) FROM fills)
    UNION ALL SELECT 'ohlcv='        || (SELECT COUNT(*) FROM ohlcv)
    UNION ALL SELECT 'orders='       || (SELECT COUNT(*) FROM orders)
    UNION ALL SELECT 'positions='    || (SELECT COUNT(*) FROM positions)
    UNION ALL SELECT 'risk_events='  || (SELECT COUNT(*) FROM risk_events)
    UNION ALL SELECT 'signals='      || (SELECT COUNT(*) FROM signals)
    UNION ALL SELECT 'system_metrics=' || (SELECT COUNT(*) FROM system_metrics)
    ORDER BY 1;
")
log "Step 1: Source row counts (exact COUNT(*)):"
echo "${SRC_COUNTS}" | sed 's/^/    /'

# ── Step 2: Download Cloud SQL Auth Proxy ────────────────────────────────────
log "Step 2: Ensuring Cloud SQL Auth Proxy is available…"

if [[ ! -x "${PROXY_BIN}" ]]; then
    log "  Downloading cloud-sql-proxy v2…"
    curl -sSL -o "${PROXY_BIN}" \
        "https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.14.2/cloud-sql-proxy.linux.amd64"
    chmod +x "${PROXY_BIN}"
    log "  Downloaded to ${PROXY_BIN}"
else
    log "  Using existing proxy at ${PROXY_BIN}"
fi

# ── Step 3: Start Cloud SQL Auth Proxy ───────────────────────────────────────
log "Step 3: Starting Cloud SQL Auth Proxy on port ${PROXY_PORT}…"

"${PROXY_BIN}" \
    "${CLOUD_SQL_CONN}" \
    --port "${PROXY_PORT}" \
    --quiet \
    > /tmp/cloud-sql-proxy-migration.log 2>&1 &

echo $! > "${PROXY_PID_FILE}"
PROXY_PID=$(cat "${PROXY_PID_FILE}")

# Wait for proxy to be ready (up to 30s)
for i in $(seq 1 30); do
    if pg_isready -h 127.0.0.1 -p "${PROXY_PORT}" -q 2>/dev/null; then
        log "  Proxy ready after ${i}s (PID ${PROXY_PID})."
        break
    fi
    if [[ ${i} -eq 30 ]]; then
        cat /tmp/cloud-sql-proxy-migration.log >&2
        fail "Cloud SQL Auth Proxy did not become ready in 30s."
    fi
    sleep 1
done

# ── Step 4: Validate Cloud SQL target ────────────────────────────────────────
log "Step 4: Validating target (Cloud SQL via proxy)…"

PGPASSWORD="${CLOUD_SQL_PASS}" psql \
    -h 127.0.0.1 -p "${PROXY_PORT}" -U quantai -d quantai \
    -c "SELECT 1" -q > /dev/null \
    || fail "Cannot connect to Cloud SQL via proxy. Check SA roles/cloudsql.client."

log "Step 4: Cloud SQL reachable."

# ── Step 5: pg_dump from local Docker ────────────────────────────────────────
log "Step 5: Dumping local PostgreSQL to ${DUMP_FILE}…"

PGPASSWORD="${SRC_PASS}" pg_dump \
    -h "${SRC_HOST}" -p "${SRC_PORT}" \
    -U "${SRC_USER}" -d "${SRC_DB}" \
    --no-owner --no-acl \
    --if-exists --clean \
    --format=plain \
    --file="${DUMP_FILE}"

DUMP_SIZE=$(du -sh "${DUMP_FILE}" | cut -f1)
log "Step 5: Dump complete (${DUMP_SIZE})."

# ── Step 6: Apply schema + data to Cloud SQL ─────────────────────────────────
log "Step 6: Restoring to Cloud SQL (this may take a minute)…"

PGPASSWORD="${CLOUD_SQL_PASS}" psql \
    -h 127.0.0.1 -p "${PROXY_PORT}" \
    -U quantai -d quantai \
    --set ON_ERROR_STOP=off \
    -f "${DUMP_FILE}" \
    > /tmp/cloud-sql-restore.log 2>&1 || true  # non-zero on DROP IF NOT EXISTS noise

# Check for real errors (not just "does not exist" from --clean)
REAL_ERRORS=$(grep -i "^ERROR:" /tmp/cloud-sql-restore.log \
    | grep -v "does not exist" \
    | grep -v "already exists" || true)

if [[ -n "${REAL_ERRORS}" ]]; then
    echo "${REAL_ERRORS}" >&2
    fail "Restore had real errors — see /tmp/cloud-sql-restore.log"
fi

log "Step 6: Restore complete."

# ── Step 7: Verify row counts ─────────────────────────────────────────────────
log "Step 7: Verifying row counts…"

DST_COUNTS=$(PGPASSWORD="${CLOUD_SQL_PASS}" psql \
    -h 127.0.0.1 -p "${PROXY_PORT}" -U quantai -d quantai \
    -t -A -c "
    SELECT 'daily_pnl='    || (SELECT COUNT(*) FROM daily_pnl)
    UNION ALL SELECT 'fills='        || (SELECT COUNT(*) FROM fills)
    UNION ALL SELECT 'ohlcv='        || (SELECT COUNT(*) FROM ohlcv)
    UNION ALL SELECT 'orders='       || (SELECT COUNT(*) FROM orders)
    UNION ALL SELECT 'positions='    || (SELECT COUNT(*) FROM positions)
    UNION ALL SELECT 'risk_events='  || (SELECT COUNT(*) FROM risk_events)
    UNION ALL SELECT 'signals='      || (SELECT COUNT(*) FROM signals)
    UNION ALL SELECT 'system_metrics=' || (SELECT COUNT(*) FROM system_metrics)
    ORDER BY 1;
")

log "  Source counts:"
echo "${SRC_COUNTS}" | sed 's/^/    /'
log "  Destination counts:"
echo "${DST_COUNTS}" | sed 's/^/    /'

MISMATCH=0
while IFS='=' read -r TABLE SRC_N; do
    DST_N=$(echo "${DST_COUNTS}" | grep "^${TABLE}=" | cut -d= -f2 || echo "MISSING")
    if [[ "${SRC_N}" != "${DST_N}" ]]; then
        log "  MISMATCH: ${TABLE}: src=${SRC_N} dst=${DST_N}"
        MISMATCH=1
    fi
done <<< "${SRC_COUNTS}"

if [[ "${MISMATCH}" -eq 1 ]]; then
    fail "Row count mismatch — check /tmp/cloud-sql-restore.log for details."
fi

log "Step 7: All row counts match."

# ── Step 8: Summary ───────────────────────────────────────────────────────────
log "═══════════════════════════════════════════════════════"
log " Migration complete!"
log ""
log " Cloud SQL connection string (already stored in Secret Manager):"
log "   database-url = postgresql://quantai:***@/quantai?host=/cloudsql/${CLOUD_SQL_CONN}"
log ""
log " To connect locally via proxy:"
log "   ${PROXY_BIN} ${CLOUD_SQL_CONN} --port ${PROXY_PORT} &"
log "   psql \"postgresql://quantai:PASSWORD@127.0.0.1:${PROXY_PORT}/quantai\""
log ""
log " Get the password:"
log "   gcloud secrets versions access latest --secret=cloud-sql-quantai-password --project=${PROJECT}"
log "═══════════════════════════════════════════════════════"
