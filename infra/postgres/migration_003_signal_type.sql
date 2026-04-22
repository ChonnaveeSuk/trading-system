-- trading-system/infra/postgres/migration_003_signal_type.sql
--
-- A/B attribution layer: persist signal_type in orders + positions.
--
-- Also fixes a latent bug: client_order_id was declared UUID but
-- alpaca_direct.py stores "quantai-{type}-{uuid}" strings (not valid UUIDs),
-- causing silent INSERT failures.
--
-- Safe to run multiple times (idempotent).

BEGIN;

-- Fix client_order_id type across orders + fills: UUID → VARCHAR(100).
-- Requires dropping and re-adding the FK since both columns must match.
-- No data loss: orders/fills are empty during paper run (inserts were failing).
DO $$
DECLARE
    orders_type text;
BEGIN
    SELECT data_type INTO orders_type
    FROM information_schema.columns
    WHERE table_name = 'orders' AND column_name = 'client_order_id';

    IF orders_type = 'uuid' THEN
        -- Drop FK constraint on fills first
        ALTER TABLE fills DROP CONSTRAINT IF EXISTS fills_client_order_id_fkey;
        -- Alter both columns
        ALTER TABLE orders ALTER COLUMN client_order_id TYPE VARCHAR(100);
        ALTER TABLE fills  ALTER COLUMN client_order_id TYPE VARCHAR(100);
        -- Re-add FK
        ALTER TABLE fills ADD CONSTRAINT fills_client_order_id_fkey
            FOREIGN KEY (client_order_id) REFERENCES orders(client_order_id);
    END IF;
END $$;

-- orders: signal_type + exit_reason
ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS signal_type VARCHAR(32) NOT NULL DEFAULT 'momentum',
    ADD COLUMN IF NOT EXISTS exit_reason VARCHAR(64);

-- positions: signal_type
ALTER TABLE positions
    ADD COLUMN IF NOT EXISTS signal_type VARCHAR(32) NOT NULL DEFAULT 'momentum';

-- Indexes for attribution queries
CREATE INDEX IF NOT EXISTS idx_orders_signal_type    ON orders   (signal_type);
CREATE INDEX IF NOT EXISTS idx_positions_signal_type ON positions (signal_type);

COMMIT;
