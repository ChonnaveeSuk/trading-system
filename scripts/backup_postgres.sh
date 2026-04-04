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

PGHOST="${POSTGRES_HOST:-localhost}"
PGPORT="${POSTGRES_PORT:-5432}"
PGDATABASE="${POSTGRES_DB:-quantai}"
PGUSER="${POSTGRES_USER:-quantai}"
export PGPASSWORD="${POSTGRES_PASSWORD:-quantai_dev_2026}"

TMPFILE=$(mktemp /tmp/quantai-backup-XXXXXX.sql.gz)

log() {
    echo "[${TIMESTAMP}] $*"
}

cleanup() {
    rm -f "${TMPFILE}"
}
trap cleanup EXIT

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
