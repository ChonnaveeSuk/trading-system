-- trading-system/infra/postgres/migration_007_orders_test_trade_flag.sql
--
-- Add test_trade flag to orders table to allow gate_progress.py to filter out
-- synthetic test trades without hardcoding dates or symbols.
--
-- Default is FALSE (production trades).

BEGIN;

ALTER TABLE orders ADD COLUMN IF NOT EXISTS test_trade BOOLEAN DEFAULT FALSE;

COMMIT;
