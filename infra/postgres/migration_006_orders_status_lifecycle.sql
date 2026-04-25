-- trading-system/infra/postgres/migration_006_orders_status_lifecycle.sql
--
-- Extend orders.status CHECK constraint to cover the full Alpaca order
-- lifecycle vocabulary.
--
-- Trigger: Sat 2026-04-25 cron execution `pghfd` crashed at
-- reconcile_alpaca_fills.py:294 with psycopg2.errors.CheckViolation while
-- updating order quantai-tr-5c71f516-c22a-40a4-bca4-d016add9216f (KGC BUY 153,
-- the duplicate cancelled via REST on 2026-04-23 17:52 UTC). Alpaca returned
-- status="canceled" → the script uppercased it to "CANCELED" → the original
-- constraint only allowed British-spelled "CANCELLED" → INSERT rejected →
-- exception escaped the reconciliation loop → _sync_positions_from_alpaca()
-- never ran → positions table left empty → 2026-04-25 morning report shipped
-- with 0 active positions, $0 momentum P&L, $0 trend-ride P&L.
--
-- Fix: extend the vocabulary to every status Alpaca can return, plus keep
-- the legacy British-spelled values so existing rows remain valid.
--   New:  ACCEPTED, NEW, PARTIAL_FILL, CANCELED, EXPIRED, REPLACED,
--         PENDING_CANCEL, PENDING_REPLACE
--   Kept: PENDING, SUBMITTED, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED
--
-- Idempotent — DROP IF EXISTS guards re-application.

BEGIN;

ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_status_check;

ALTER TABLE orders ADD CONSTRAINT orders_status_check
    CHECK (status IN (
        -- Internal lifecycle (set by Rust OMS / alpaca_direct.py)
        'PENDING',
        'SUBMITTED',
        -- Alpaca lifecycle (returned by GET /v2/orders/{id}.status, uppercased)
        'ACCEPTED',
        'NEW',
        'PARTIAL_FILL',
        'PARTIALLY_FILLED',   -- legacy spelling, retained for existing rows
        'FILLED',
        'CANCELED',           -- Alpaca's American spelling
        'CANCELLED',          -- legacy British spelling, retained for existing rows
        'EXPIRED',
        'REJECTED',
        'REPLACED',
        'PENDING_CANCEL',
        'PENDING_REPLACE'
    ));

COMMIT;
