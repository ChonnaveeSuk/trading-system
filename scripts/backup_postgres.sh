#!/usr/bin/env bash
# trading-system/scripts/backup_postgres.sh
#
# Daily PostgreSQL backup to GCS.
#
# - Dumps the full `quantai` database with pg_dump
# - Compresses with gzip
# - Uploads to gs://quantai-backups-quantai-trading-paper/postgres/YYYY-MM-DD.sql.gz
# - Verifies the upload succeeded before deleting the local temp file
# - Exits non-zero on any error (safe to wire into cron alerting)
#
# Prerequisites:
#   - pg_dump accessible (postgres:16 container or host install)
#   - gsutil authenticated (gcloud auth application-default login)
#   - GCP_PROJECT_ID set (or defaults to quantai-trading-paper)
#   - DATABASE_URL set, or POSTGRES_* vars individually
#
# Usage:
#   bash scripts/backup_postgres.sh
#
# Cron (daily at 02:00 UTC):
#   0 2 * * * /home/chonsuk/trading-system/scripts/backup_postgres.sh >> /var/log/quantai-backup.log 2>&1

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
BUCKET="${GCS_BACKUP_BUCKET:-quantai-backups-quantai-trading-paper}"
GCS_PREFIX="postgres"
DATE=$(date -u +%Y-%m-%d)
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
GCP_PROJECT_ID="${GCP_PROJECT_ID:-quantai-trading-paper}"
_SQL_INSTANCE="quantai-postgres"

# Parse DATABASE_URL when set (Cloud Run / Cloud SQL socket format):
#   postgresql://user:pass@/db?host=/cloudsql/PROJECT:REGION:INSTANCE
#   postgresql://user:pass@host:port/db
# Falls back to individual POSTGRES_* env vars for local Docker dev.
if [[ -n "${DATABASE_URL:-}" ]]; then
    # Pass DATABASE_URL via env (never inline via shell expansion in heredoc) +
    # shell-safe quoting. Previous version leaked full URL (incl. password) to
    # PGDATABASE when parser silently returned the literal "${DATABASE_URL}".
    eval "$(DATABASE_URL="$DATABASE_URL" python3 - <<'PYEOF'
import os, shlex, urllib.parse
url = urllib.parse.urlparse(os.environ.get("DATABASE_URL", ""))
params = dict(urllib.parse.parse_qsl(url.query))
host = params.get("host") or url.hostname or "localhost"
port = url.port or 5432
user = url.username or "quantai"
password = urllib.parse.unquote(url.password or "")
db   = url.path.lstrip("/") or "quantai"
print(f"PGHOST={shlex.quote(host)}")
print(f"PGPORT={shlex.quote(str(port))}")
print(f"PGUSER={shlex.quote(user)}")
print(f"PGPASSWORD={shlex.quote(password)}")
print(f"PGDATABASE={shlex.quote(db)}")
PYEOF
    )"
else
    PGHOST="${POSTGRES_HOST:-localhost}"
    PGPORT="${POSTGRES_PORT:-5432}"
    PGDATABASE="${POSTGRES_DB:-quantai}"
    PGUSER="${POSTGRES_USER:-quantai}"
    PGPASSWORD="${POSTGRES_PASSWORD:-quantai_dev_2026}"
fi
export PGPASSWORD

TMPFILE=$(mktemp /tmp/quantai-backup-XXXXXX.sql.gz)
_WE_STARTED_SQL=0

log() {
    echo "[${TIMESTAMP}] $*"
}

# ── Cloud SQL lifecycle helpers ────────────────────────────────────────────────
_start_cloud_sql_backup() {
    log "MANAGE_CLOUD_SQL: starting Cloud SQL instance ${_SQL_INSTANCE}…"
    gcloud sql instances patch "${_SQL_INSTANCE}" \
        --activation-policy ALWAYS \
        --project "${GCP_PROJECT_ID}" \
        --quiet 2>&1 || true
    # Wait for instance to be RUNNABLE
    local deadline=$(( SECONDS + 600 ))
    while [[ $SECONDS -lt $deadline ]]; do
        local state
        state=$(gcloud sql instances describe "${_SQL_INSTANCE}" \
            --project "${GCP_PROJECT_ID}" \
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

_stop_cloud_sql_backup() {
    log "MANAGE_CLOUD_SQL: stopping Cloud SQL instance ${_SQL_INSTANCE}…"
    gcloud sql instances patch "${_SQL_INSTANCE}" \
        --activation-policy NEVER \
        --project "${GCP_PROJECT_ID}" \
        --quiet 2>&1 || true
    log "MANAGE_CLOUD_SQL: stop command sent."
}

cleanup() {
    rm -f "${TMPFILE}"
    if [[ "${_WE_STARTED_SQL}" -eq 1 ]]; then
        _stop_cloud_sql_backup
    fi
}
trap cleanup EXIT

# ── Cloud SQL: start if needed ─────────────────────────────────────────────────
if [[ "${MANAGE_CLOUD_SQL:-0}" == "1" ]]; then
    _state=$(gcloud sql instances describe "${_SQL_INSTANCE}" \
        --project "${GCP_PROJECT_ID}" \
        --format="value(state)" 2>/dev/null || echo "UNKNOWN")
    if [[ "${_state}" != "RUNNABLE" ]]; then
        _start_cloud_sql_backup
        _WE_STARTED_SQL=1
    else
        log "MANAGE_CLOUD_SQL: Cloud SQL already RUNNABLE — no start needed."
    fi
fi

# ── Dump ──────────────────────────────────────────────────────────────────────
log "Starting PostgreSQL backup: ${PGDATABASE}@${PGHOST}:${PGPORT}"

pg_dump \
    --host="${PGHOST}" \
    --port="${PGPORT}" \
    --username="${PGUSER}" \
    --dbname="${PGDATABASE}" \
    --no-password \
    --format=plain \
    --no-owner \
    --no-privileges \
    | gzip -9 > "${TMPFILE}"

DUMP_SIZE=$(du -sh "${TMPFILE}" | cut -f1)
log "Dump complete: ${DUMP_SIZE} compressed"

# ── Upload ────────────────────────────────────────────────────────────────────
GCS_PATH="gs://${BUCKET}/${GCS_PREFIX}/${DATE}.sql.gz"
log "Uploading to ${GCS_PATH}"

gsutil -q cp "${TMPFILE}" "${GCS_PATH}"

# Verify the object exists and is non-empty
REMOTE_SIZE=$(gsutil stat "${GCS_PATH}" 2>/dev/null | awk '/Content-Length:/{print $2}')
if [[ -z "${REMOTE_SIZE}" || "${REMOTE_SIZE}" -eq 0 ]]; then
    log "ERROR: Upload verification failed — remote object missing or empty"
    exit 1
fi

log "Backup verified at ${GCS_PATH} (${REMOTE_SIZE} bytes)"

# ── Retention cleanup (GCS lifecycle handles this, but log for visibility) ────
log "Backup complete. GCS lifecycle rule removes backups older than 90 days."
