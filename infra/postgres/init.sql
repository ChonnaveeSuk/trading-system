-- trading-system/infra/postgres/init.sql
--
-- QuantAI hot database schema (PostgreSQL 16).
-- This is LOCAL storage for execution-critical data.
-- Historical archive → BigQuery (see gcp/bigquery/schema/).
--
-- All financial values use NUMERIC(18,8) to match rust_decimal precision.
-- Never use FLOAT for prices, quantities, or P&L.

-- ── Extensions ────────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm"; -- for symbol search

-- ── OHLCV ─────────────────────────────────────────────────────────────────────
-- Hot store: last 30 days of bars per symbol.
-- Older data is archived to BigQuery via daily GCS backup.
CREATE TABLE IF NOT EXISTS ohlcv (
    symbol      VARCHAR(20)    NOT NULL,
    timestamp   TIMESTAMPTZ    NOT NULL,
    open        NUMERIC(18,8)  NOT NULL,
    high        NUMERIC(18,8)  NOT NULL,
    low         NUMERIC(18,8)  NOT NULL,
    close       NUMERIC(18,8)  NOT NULL,
    volume      NUMERIC(18,4)  NOT NULL,
    vwap        NUMERIC(18,8),
    inserted_at TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_time ON ohlcv (symbol, timestamp DESC);

-- ── Orders ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
    client_order_id  UUID           PRIMARY KEY,
    broker_order_id  VARCHAR(100),
    symbol           VARCHAR(20)    NOT NULL,
    side             VARCHAR(4)     NOT NULL CHECK (side IN ('BUY', 'SELL')),
    order_type       VARCHAR(20)    NOT NULL,
    quantity         NUMERIC(18,8)  NOT NULL CHECK (quantity > 0),
    limit_price      NUMERIC(18,8),
    stop_price       NUMERIC(18,8),
    stop_loss        NUMERIC(18,8),              -- Required by risk engine
    signal_score     DOUBLE PRECISION,           -- [0.0, 1.0]
    strategy_id      VARCHAR(100),
    status           VARCHAR(20)    NOT NULL DEFAULT 'PENDING'
                         CHECK (status IN ('PENDING','SUBMITTED','PARTIALLY_FILLED',
                                           'FILLED','CANCELLED','REJECTED')),
    created_at       TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_orders_symbol     ON orders (symbol);
CREATE INDEX IF NOT EXISTS idx_orders_status     ON orders (status) WHERE status NOT IN ('FILLED','CANCELLED','REJECTED');
CREATE INDEX IF NOT EXISTS idx_orders_strategy   ON orders (strategy_id);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders (created_at DESC);

-- Auto-update updated_at on any row change
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END; $$;

CREATE TRIGGER trg_orders_updated_at
    BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ── Fills ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fills (
    fill_id          UUID           PRIMARY KEY DEFAULT uuid_generate_v4(),
    client_order_id  UUID           NOT NULL REFERENCES orders(client_order_id),
    broker_order_id  VARCHAR(100),
    symbol           VARCHAR(20)    NOT NULL,
    side             VARCHAR(4)     NOT NULL CHECK (side IN ('BUY', 'SELL')),
    filled_quantity  NUMERIC(18,8)  NOT NULL CHECK (filled_quantity > 0),
    fill_price       NUMERIC(18,8)  NOT NULL CHECK (fill_price > 0),
    commission       NUMERIC(18,8)  NOT NULL DEFAULT 0 CHECK (commission >= 0),
    gross_value      NUMERIC(18,8)  GENERATED ALWAYS AS (filled_quantity * fill_price) STORED,
    timestamp        TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    strategy_id      VARCHAR(100)   -- Denormalized for faster analytics queries
);

CREATE INDEX IF NOT EXISTS idx_fills_order     ON fills (client_order_id);
CREATE INDEX IF NOT EXISTS idx_fills_symbol    ON fills (symbol, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_fills_strategy  ON fills (strategy_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_fills_timestamp ON fills (timestamp DESC);

-- ── Positions ─────────────────────────────────────────────────────────────────
-- Current open positions (one row per symbol).
-- Closed positions (quantity = 0) are retained for audit.
CREATE TABLE IF NOT EXISTS positions (
    symbol           VARCHAR(20)    PRIMARY KEY,
    quantity         NUMERIC(18,8)  NOT NULL DEFAULT 0,  -- signed: + long, - short
    average_cost     NUMERIC(18,8)  NOT NULL DEFAULT 0 CHECK (average_cost >= 0),
    realized_pnl     NUMERIC(18,8)  NOT NULL DEFAULT 0,
    unrealized_pnl   NUMERIC(18,8)  NOT NULL DEFAULT 0,
    stop_loss        NUMERIC(18,8),
    opened_at        TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE TRIGGER trg_positions_updated_at
    BEFORE UPDATE ON positions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ── Signals ───────────────────────────────────────────────────────────────────
-- Every signal received from the Python strategy layer is logged here.
-- acted_on = TRUE means an order was submitted from this signal.
CREATE TABLE IF NOT EXISTS signals (
    signal_id    UUID             PRIMARY KEY DEFAULT uuid_generate_v4(),
    symbol       VARCHAR(20)      NOT NULL,
    strategy_id  VARCHAR(100)     NOT NULL,
    score        DOUBLE PRECISION NOT NULL CHECK (score BETWEEN 0.0 AND 1.0),
    direction    VARCHAR(4)       NOT NULL CHECK (direction IN ('BUY', 'SELL', 'HOLD')),
    features     JSONB,                       -- Feature vector for audit/research
    acted_on     BOOLEAN          NOT NULL DEFAULT FALSE,
    reject_reason VARCHAR(200),              -- Why risk engine rejected (if applicable)
    created_at   TIMESTAMPTZ      NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol     ON signals (symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_strategy   ON signals (strategy_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_acted_on   ON signals (acted_on, created_at DESC);

-- ── Risk Events ───────────────────────────────────────────────────────────────
-- Immutable audit log of all risk engine actions.
-- HALT events mean trading was stopped and require manual reset.
CREATE TABLE IF NOT EXISTS risk_events (
    event_id    UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    event_type  VARCHAR(50)  NOT NULL,   -- e.g. 'DAILY_LOSS_HALT', 'POSITION_REJECTED'
    severity    VARCHAR(10)  NOT NULL CHECK (severity IN ('INFO', 'WARN', 'HALT')),
    symbol      VARCHAR(20),
    order_id    UUID,
    details     JSONB        NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_risk_events_severity ON risk_events (severity, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_risk_events_symbol   ON risk_events (symbol, created_at DESC);

-- ── Daily P&L Summary ─────────────────────────────────────────────────────────
-- Materialized per-day P&L for Grafana dashboard + halt logic.
CREATE TABLE IF NOT EXISTS daily_pnl (
    trading_date    DATE           PRIMARY KEY,
    starting_value  NUMERIC(18,8)  NOT NULL,
    ending_value    NUMERIC(18,8),
    realized_pnl    NUMERIC(18,8)  NOT NULL DEFAULT 0,
    unrealized_pnl  NUMERIC(18,8)  NOT NULL DEFAULT 0,
    total_pnl       NUMERIC(18,8)  GENERATED ALWAYS AS (realized_pnl + unrealized_pnl) STORED,
    num_trades      INTEGER        NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

-- ── System Metrics ────────────────────────────────────────────────────────────
-- Operational health metrics written by scripts/log_system_health.py.
-- Rows are append-only; queries always take the latest per metric_name.
CREATE TABLE IF NOT EXISTS system_metrics (
    metric_id    BIGSERIAL       PRIMARY KEY,
    metric_name  VARCHAR(50)     NOT NULL,  -- 'pg_connections', 'redis_hit_rate', 'alpaca_latency_ms', etc.
    metric_value DOUBLE PRECISION NOT NULL,
    labels       JSONB           NOT NULL DEFAULT '{}',
    recorded_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_system_metrics_name_time
    ON system_metrics (metric_name, recorded_at DESC);

-- ── Seed: initial portfolio value ────────────────────────────────────────────
-- Insert today's row when the system first starts.
-- Replace with actual starting capital before paper trading begins.
INSERT INTO daily_pnl (trading_date, starting_value, realized_pnl, unrealized_pnl, num_trades)
VALUES (CURRENT_DATE, 100000.00, 0, 0, 0)
ON CONFLICT (trading_date) DO NOTHING;
