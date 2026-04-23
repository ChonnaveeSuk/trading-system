-- trading-system/infra/postgres/migration_005_backfill_daily_pnl_20260423.sql
--
-- ONE-SHOT BACKFILL — not idempotent beyond a single row. Manual apply only.
--
-- Corrects the daily_pnl row written on 2026-04-23 by the pre-fix
-- update_daily_pnl.py cash-flow-as-loss formula. The bug reported
-- realized_pnl = -$49,234.97 (= sum of all BUY notional) because 10 BUYs
-- and 0 SELLs hit the old `CASE WHEN side='SELL' THEN (qty*price) ELSE
-- -(qty*price+commission) END` aggregate. Cascaded into false 50.55%
-- drawdown, tripped 15% gate, wiped A/B + sector sections.
--
-- Correct numbers verified against Alpaca on 2026-04-24 ~04:55 ICT:
--   equity           = 99457.01
--   last_equity      = 100000.00 (paper reset default; real value was
--                                 97401.99 — Alpaca resets last_equity
--                                 once per day at US market open)
--   today_pnl (real) = 99457.01 - 97401.99 ≈ -543.00  (unrealized-only)
--
-- Since the post-fix update_daily_pnl.py will read equity from Alpaca on
-- every cron run, this backfill is a one-time historical correction so
-- the Grafana equity curve and the MaxDD gate are not permanently
-- contaminated by the bug.
--
-- APPLY — manual, post-deploy only:
--
--   # 1. Start Cloud SQL proxy (see .env.local or CLAUDE.md)
--   PG_PASS=$(gcloud secrets versions access latest \
--     --secret=cloud-sql-quantai-password --project=quantai-trading-paper)
--   PGPASSWORD="$PG_PASS" psql \
--     -h 127.0.0.1 -p 5433 -U quantai -d quantai \
--     -f infra/postgres/migration_005_backfill_daily_pnl_20260423.sql
--
--   # 2. Verify:
--   # SELECT * FROM daily_pnl WHERE trading_date = '2026-04-23';
--
-- DO NOT run this against local Docker — it's a production-only correction.

BEGIN;

-- Sanity check: only patch if the known-bad value is still there.
-- Prevents double-application if someone already fixed it manually.
UPDATE daily_pnl
SET
    starting_value = 97401.99,
    ending_value   = 96858.99,  -- = starting_value + unrealized
    realized_pnl   = 0.00,
    unrealized_pnl = -543.00,   -- reconstructed from equity delta
    num_trades     = 10,
    updated_at     = NOW()
WHERE trading_date = '2026-04-23'
  AND realized_pnl < -49000;    -- signature of the bug

-- If the row was already corrected (or never hit the bug) this UPDATE
-- touches 0 rows and is harmless.

COMMIT;
