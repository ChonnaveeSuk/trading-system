-- trading-system/infra/postgres/migration_002_system_metrics.sql
--
-- Standalone migration for deployments already running (container already initialised).
-- Run once: psql $DATABASE_URL -f infra/postgres/migration_002_system_metrics.sql

CREATE TABLE IF NOT EXISTS system_metrics (
    metric_id    BIGSERIAL        PRIMARY KEY,
    metric_name  VARCHAR(50)      NOT NULL,
    metric_value DOUBLE PRECISION NOT NULL,
    labels       JSONB            NOT NULL DEFAULT '{}',
    recorded_at  TIMESTAMPTZ      NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_system_metrics_name_time
    ON system_metrics (metric_name, recorded_at DESC);
