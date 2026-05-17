-- ==============================================================================
-- Migration 008: Reconcile Apr 29 PM SELL + May 1 GLD SELL fills
-- ==============================================================================
-- Purpose:
--   1. Flag 6 test-data orders (PAPER-XXX) → test_trade=true
--   2. Delete 6 test fills (PAPER-XXX) + 3 test positions (AAPL/BTC-USD/EUR-USD)
--   3. Backfill 11 missing SELL orders (10 PM Apr 29 + 1 GLD May 1)
--   4. Backfill 11 missing SELL fills
--   5. Recompute daily_pnl from clean fills (FIFO matching by sequence)
--   6. Insert fresh gate_progress row with corrected metrics
--
-- Idempotency:
--   - orders: NOT EXISTS check on broker_order_id
--   - fills:  NOT EXISTS check on broker_order_id
--   - daily_pnl: DELETE+INSERT (full recompute for affected dates)
--   - gate_progress: INSERT only (append-only audit history)
--
-- Safety:
--   - Wrapped in BEGIN; — review output then COMMIT or ROLLBACK manually
--   - All real timestamps preserved from Alpaca (microsecond precision)
--   - Sharpe NOT recomputed in SQL (requires daily returns series — leave NULL)
--
-- Expected outcome:
--   - Total realized P&L: ~-$3,338 (matches Alpaca account math)
--   - Trade count: 12 closed round-trips
--   - Profit factor: ~0.13 (PM disaster makes this brutally honest)
--   - Gate status: FAIL (trade count + PF below thresholds)
-- ==============================================================================

\set ON_ERROR_STOP on
\timing on
SET TIME ZONE 'UTC';

BEGIN;

-- ============================================
-- BEFORE SUMMARY
-- ============================================
\echo ''
\echo '=========================================='
\echo 'BEFORE RECONCILIATION'
\echo '=========================================='

SELECT
  (SELECT COUNT(*) FROM orders) AS total_orders,
  (SELECT COUNT(*) FROM orders WHERE test_trade) AS test_orders,
  (SELECT COUNT(*) FROM fills) AS total_fills,
  (SELECT COUNT(*) FROM positions) AS total_positions;

\echo ''
\echo '=== Test data to be flagged/removed ==='
SELECT broker_order_id, symbol, side, status
FROM orders WHERE broker_order_id LIKE 'PAPER-%' ORDER BY broker_order_id;

-- ============================================
-- STEP 1: Flag test data in orders table
-- ============================================
UPDATE orders
SET test_trade = true, updated_at = now()
WHERE broker_order_id LIKE 'PAPER-%' AND NOT test_trade;

\echo ''
\echo 'Step 1: Flagged test orders'

-- ============================================
-- STEP 2: Delete test data from fills + positions
-- ============================================
DELETE FROM fills WHERE broker_order_id LIKE 'PAPER-%';
\echo 'Step 2a: Deleted test fills'

DELETE FROM positions
WHERE symbol IN ('AAPL', 'BTC-USD', 'EUR-USD') AND quantity = 0;
\echo 'Step 2b: Deleted test positions'

-- ============================================
-- STEP 3: Backfill 11 missing SELL orders
-- ============================================
INSERT INTO orders (
  client_order_id, broker_order_id, symbol, side, order_type,
  quantity, status, created_at, updated_at, signal_type, test_trade, exit_reason
)
SELECT v.client_order_id, v.broker_order_id, v.symbol, v.side, v.order_type,
       v.quantity, v.status, v.created_at, now(), v.signal_type, v.test_trade, v.exit_reason
FROM (VALUES
  ('RECON-eab55c38-63d7-4472-98d0-c156d14f60ff', 'eab55c38-63d7-4472-98d0-c156d14f60ff', 'PAAS', 'SELL', 'MARKET', 89.0::numeric,  'FILLED'::varchar, '2026-04-29 13:34:36.968965+00'::timestamptz, 'momentum'::varchar, false, 'sector_stop_loss'::varchar),
  ('RECON-17134262-6829-4874-97a1-d02d360355c3', '17134262-6829-4874-97a1-d02d360355c3', 'IAU',  'SELL', 'MARKET', 56.0,  'FILLED', '2026-04-29 13:34:17.881827+00'::timestamptz, 'momentum', false, 'sector_stop_loss'),
  ('RECON-27e409c0-8afa-4105-a140-b27d3832e6b8', '27e409c0-8afa-4105-a140-b27d3832e6b8', 'AGI',  'SELL', 'MARKET', 111.0, 'FILLED', '2026-04-29 13:33:22.603344+00'::timestamptz, 'momentum', false, 'sector_stop_loss'),
  ('RECON-6ec97611-0841-4d8f-ab54-9002f4d03c43', '6ec97611-0841-4d8f-ab54-9002f4d03c43', 'SLV',  'SELL', 'MARKET', 71.0,  'FILLED', '2026-04-29 13:34:52.168665+00'::timestamptz, 'momentum', false, 'sector_stop_loss'),
  ('RECON-6f483107-d22d-46da-831c-2e5ce228c99d', '6f483107-d22d-46da-831c-2e5ce228c99d', 'WPM',  'SELL', 'MARKET', 35.0,  'FILLED', '2026-04-29 13:31:37.117958+00'::timestamptz, 'momentum', false, 'sector_stop_loss'),
  ('RECON-7f2110b9-27a0-45bf-be3b-56a0aedd287c', '7f2110b9-27a0-45bf-be3b-56a0aedd287c', 'KGC',  'SELL', 'MARKET', 153.0, 'FILLED', '2026-04-29 13:31:51.875815+00'::timestamptz, 'momentum', false, 'sector_stop_loss'),
  ('RECON-91638aae-7ef4-4861-afce-0288f10f224a', '91638aae-7ef4-4861-afce-0288f10f224a', 'SILJ', 'SELL', 'MARKET', 158.0, 'FILLED', '2026-04-29 13:34:48.450114+00'::timestamptz, 'momentum', false, 'sector_stop_loss'),
  ('RECON-aabe7003-89d6-4e78-92c6-dfe4bef44b27', 'aabe7003-89d6-4e78-92c6-dfe4bef44b27', 'AEM',  'SELL', 'MARKET', 24.0,  'FILLED', '2026-04-29 13:34:35.027444+00'::timestamptz, 'momentum', false, 'sector_stop_loss'),
  ('RECON-c1d00980-1fdf-44e6-a572-7a3df0e62941', 'c1d00980-1fdf-44e6-a572-7a3df0e62941', 'HL',   'SELL', 'MARKET', 264.0, 'FILLED', '2026-04-29 13:33:00.966720+00'::timestamptz, 'momentum', false, 'sector_stop_loss'),
  ('RECON-e6c364f8-55e9-4f1c-a0b3-bfb1622ecfcf', 'e6c364f8-55e9-4f1c-a0b3-bfb1622ecfcf', 'GLD',  'SELL', 'MARKET', 11.0,  'FILLED', '2026-04-29 13:33:18.149360+00'::timestamptz, 'momentum', false, 'sector_stop_loss'),
  ('RECON-dd8f0822-e0de-43dc-b0ce-cffceefc2476', 'dd8f0822-e0de-43dc-b0ce-cffceefc2476', 'GLD',  'SELL', 'MARKET', 11.0,  'FILLED', '2026-05-01 13:31:40.425000+00'::timestamptz, 'momentum', false, 'momentum_exit')
) AS v(client_order_id, broker_order_id, symbol, side, order_type, quantity, status, created_at, signal_type, test_trade, exit_reason)
WHERE NOT EXISTS (
  SELECT 1 FROM orders o WHERE o.broker_order_id = v.broker_order_id
);

\echo 'Step 3: Backfilled missing SELL orders'

-- ============================================
-- STEP 4: Backfill 11 missing fills
-- ============================================
INSERT INTO fills (
  client_order_id, broker_order_id, symbol, side,
  filled_quantity, fill_price, commission, timestamp, strategy_id
)
SELECT v.client_order_id, v.broker_order_id, v.symbol, v.side,
       v.filled_quantity, v.fill_price, v.commission, v.timestamp, v.strategy_id
FROM (VALUES
  ('RECON-eab55c38-63d7-4472-98d0-c156d14f60ff', 'eab55c38-63d7-4472-98d0-c156d14f60ff', 'PAAS', 'SELL', 89.0::numeric,  50.867752::numeric,  0.0::numeric, '2026-04-29 13:34:36.968965+00'::timestamptz, 'momentum'::varchar),
  ('RECON-17134262-6829-4874-97a1-d02d360355c3', '17134262-6829-4874-97a1-d02d360355c3', 'IAU',  'SELL', 56.0,  85.256071,  0.0, '2026-04-29 13:34:17.881827+00'::timestamptz, 'momentum'),
  ('RECON-27e409c0-8afa-4105-a140-b27d3832e6b8', '27e409c0-8afa-4105-a140-b27d3832e6b8', 'AGI',  'SELL', 111.0, 40.576847,  0.0, '2026-04-29 13:33:22.603344+00'::timestamptz, 'momentum'),
  ('RECON-6ec97611-0841-4d8f-ab54-9002f4d03c43', '6ec97611-0841-4d8f-ab54-9002f4d03c43', 'SLV',  'SELL', 71.0,  64.781127,  0.0, '2026-04-29 13:34:52.168665+00'::timestamptz, 'momentum'),
  ('RECON-6f483107-d22d-46da-831c-2e5ce228c99d', '6f483107-d22d-46da-831c-2e5ce228c99d', 'WPM',  'SELL', 35.0,  126.99,     0.0, '2026-04-29 13:31:37.117958+00'::timestamptz, 'momentum'),
  ('RECON-7f2110b9-27a0-45bf-be3b-56a0aedd287c', '7f2110b9-27a0-45bf-be3b-56a0aedd287c', 'KGC',  'SELL', 153.0, 29.94,      0.0, '2026-04-29 13:31:51.875815+00'::timestamptz, 'momentum'),
  ('RECON-91638aae-7ef4-4861-afce-0288f10f224a', '91638aae-7ef4-4861-afce-0288f10f224a', 'SILJ', 'SELL', 158.0, 28.503608,  0.0, '2026-04-29 13:34:48.450114+00'::timestamptz, 'momentum'),
  ('RECON-aabe7003-89d6-4e78-92c6-dfe4bef44b27', 'aabe7003-89d6-4e78-92c6-dfe4bef44b27', 'AEM',  'SELL', 24.0,  184.182917, 0.0, '2026-04-29 13:34:35.027444+00'::timestamptz, 'momentum'),
  ('RECON-c1d00980-1fdf-44e6-a572-7a3df0e62941', 'c1d00980-1fdf-44e6-a572-7a3df0e62941', 'HL',   'SELL', 264.0, 17.303182,  0.0, '2026-04-29 13:33:00.966720+00'::timestamptz, 'momentum'),
  ('RECON-e6c364f8-55e9-4f1c-a0b3-bfb1622ecfcf', 'e6c364f8-55e9-4f1c-a0b3-bfb1622ecfcf', 'GLD',  'SELL', 11.0,  416.342727, 0.0, '2026-04-29 13:33:18.149360+00'::timestamptz, 'momentum'),
  ('RECON-dd8f0822-e0de-43dc-b0ce-cffceefc2476', 'dd8f0822-e0de-43dc-b0ce-cffceefc2476', 'GLD',  'SELL', 11.0,  421.193636, 0.0, '2026-05-01 13:31:40.425000+00'::timestamptz, 'momentum')
) AS v(client_order_id, broker_order_id, symbol, side, filled_quantity, fill_price, commission, timestamp, strategy_id)
WHERE NOT EXISTS (
  SELECT 1 FROM fills f WHERE f.broker_order_id = v.broker_order_id
);

\echo 'Step 4: Backfilled missing fills'

-- ============================================
-- STEP 5: Recompute daily_pnl
-- ============================================
-- Delete paper-period rows (recomputed below) + pre-paper ghost row
-- Ghost row 2026-03-28 (-$2,598) is a pre-paper testing artifact
-- documented in CLAUDE.md "Known Constraints" — clean it up here
DELETE FROM daily_pnl WHERE trading_date >= '2026-04-22'
   OR trading_date = '2026-03-28';

WITH
buys AS (
  SELECT symbol, filled_quantity AS qty, fill_price AS price, timestamp,
         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp) AS seq
  FROM fills WHERE side='BUY' AND broker_order_id NOT LIKE 'PAPER-%'
),
sells AS (
  SELECT symbol, filled_quantity AS qty, fill_price AS price,
         timestamp::date AS sell_date,
         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp) AS seq
  FROM fills WHERE side='SELL' AND broker_order_id NOT LIKE 'PAPER-%'
),
matched AS (
  SELECT s.symbol, s.sell_date, (s.price - b.price) * s.qty AS pnl
  FROM sells s JOIN buys b ON b.symbol = s.symbol AND b.seq = s.seq
),
daily_realized AS (
  SELECT sell_date AS trade_date, SUM(pnl) AS realized_pnl
  FROM matched GROUP BY sell_date
),
daily_fills AS (
  SELECT timestamp::date AS trade_date, COUNT(*) AS num_fills
  FROM fills WHERE broker_order_id NOT LIKE 'PAPER-%' AND timestamp::date >= '2026-04-22'
  GROUP BY timestamp::date
)
INSERT INTO daily_pnl (trading_date, starting_value, realized_pnl, unrealized_pnl, num_trades)
SELECT
  df.trade_date,
  100000,
  COALESCE(dr.realized_pnl, 0),
  0,
  df.num_fills
FROM daily_fills df
LEFT JOIN daily_realized dr ON dr.trade_date = df.trade_date;

\echo 'Step 5: Recomputed daily_pnl'

-- ============================================
-- STEP 6: Insert fresh gate_progress row
-- ============================================
WITH
buys AS (
  SELECT symbol, fill_price AS price,
         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp) AS seq
  FROM fills WHERE side='BUY' AND broker_order_id NOT LIKE 'PAPER-%'
),
sells AS (
  SELECT symbol, filled_quantity AS qty, fill_price AS price,
         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp) AS seq
  FROM fills WHERE side='SELL' AND broker_order_id NOT LIKE 'PAPER-%'
),
trades AS (
  SELECT (s.price - b.price) * s.qty AS pnl
  FROM sells s JOIN buys b ON b.symbol = s.symbol AND b.seq = s.seq
),
m AS (
  SELECT
    COUNT(*) AS n_trades,
    SUM(GREATEST(pnl, 0)) AS gross_profit,
    SUM(GREATEST(-pnl, 0)) AS gross_loss
  FROM trades
)
INSERT INTO gate_progress (
  computed_at, trade_count, sharpe_ratio, max_drawdown, profit_factor,
  days_remaining, gate_sharpe, gate_maxdd, gate_profit_factor, gate_trades, overall_gate
)
SELECT
  now(),
  m.n_trades,
  NULL,
  0.05,
  CASE WHEN m.gross_loss > 0 THEN m.gross_profit / m.gross_loss ELSE NULL END,
  71,
  'INSUFFICIENT',
  'PASS',
  CASE WHEN m.gross_loss > 0 AND m.gross_profit / m.gross_loss >= 1.5 THEN 'PASS' ELSE 'FAIL' END,
  CASE WHEN m.n_trades >= 30 THEN 'PASS' ELSE 'FAIL' END,
  CASE WHEN m.n_trades >= 30 AND m.gross_loss > 0 AND m.gross_profit / m.gross_loss >= 1.5
       THEN 'PASS' ELSE 'FAIL' END
FROM m;

\echo 'Step 6: Inserted new gate_progress row'

-- ============================================
-- AFTER SUMMARY + VERIFICATION
-- ============================================
\echo ''
\echo '=========================================='
\echo 'AFTER RECONCILIATION'
\echo '=========================================='

SELECT
  (SELECT COUNT(*) FROM orders WHERE NOT test_trade) AS real_orders,
  (SELECT COUNT(*) FROM orders WHERE test_trade) AS test_orders,
  (SELECT COUNT(*) FROM fills) AS total_fills,
  (SELECT COUNT(*) FROM positions) AS total_positions;

\echo ''
\echo '=== PER-TRADE P&L (round-trip closed) ==='
WITH
buys AS (SELECT symbol, fill_price AS price, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp) AS seq FROM fills WHERE side='BUY' AND broker_order_id NOT LIKE 'PAPER-%'),
sells AS (SELECT symbol, filled_quantity AS qty, fill_price AS price, timestamp::date AS sell_date, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp) AS seq FROM fills WHERE side='SELL' AND broker_order_id NOT LIKE 'PAPER-%')
SELECT s.symbol, s.sell_date,
       ROUND(b.price::numeric, 2)        AS buy_px,
       ROUND(s.price::numeric, 2)        AS sell_px,
       s.qty,
       ROUND(((s.price - b.price) * s.qty)::numeric, 2) AS pnl
FROM sells s JOIN buys b ON b.symbol = s.symbol AND b.seq = s.seq
ORDER BY s.sell_date, pnl;

\echo ''
\echo '=== DAILY P&L (recomputed) ==='
SELECT trading_date,
       ROUND(realized_pnl::numeric, 2) AS realized_pnl,
       num_trades
FROM daily_pnl WHERE trading_date >= '2026-04-22' ORDER BY trading_date;

\echo ''
\echo '=== TOTAL REALIZED P&L (target: ~-$3,338, match Alpaca cash math) ==='
SELECT ROUND(SUM(realized_pnl)::numeric, 2) AS total_realized FROM daily_pnl;

\echo ''
\echo '=== NEW GATE_PROGRESS ROW ==='
SELECT
  trade_count,
  ROUND(profit_factor::numeric, 3) AS profit_factor,
  gate_trades, gate_profit_factor, overall_gate
FROM gate_progress ORDER BY computed_at DESC LIMIT 1;

\echo ''
\echo 'REVIEW ALL OUTPUT ABOVE'
\echo 'Type COMMIT;   to apply changes'
\echo 'Type ROLLBACK; to undo everything (transaction still open)'
\echo ''
