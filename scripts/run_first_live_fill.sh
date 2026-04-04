#!/bin/bash
# trading-system/scripts/run_first_live_fill.sh
#
# One-shot: runs at market open on April 7, 2026 (20:30 Thai / 09:30 ET).
# Crontab entry (added 2026-04-03):
#   30 20 7 4 * /home/chonsuk/trading-system/scripts/run_first_live_fill.sh >> /var/log/quantai-first-live-fill.log 2>&1
#
# Steps:
#   1. Verify Alpaca credentials + market is open
#   2. Submit AAPL BUY 1 share — wait for fill
#   3. Insert fill into PostgreSQL, publish to Pub/Sub → BigQuery
#   4. Update daily_pnl table (day-1 of 90-day paper tracking)
#   5. Write structured result to /tmp/quantai_first_fill_result.json
#      (read by CronCreate agent at 21:31 to update CLAUDE.md)

set -euo pipefail

REPO="/home/chonsuk/trading-system"
LOG_FILE="/var/log/quantai-first-live-fill.log"
RESULT_FILE="/tmp/quantai_first_fill_result.json"

export DATABASE_URL="postgres://quantai:quantai_dev_2026@localhost:5432/quantai"
export GCP_PROJECT_ID="quantai-trading-paper"

echo ""
echo "════════════════════════════════════════════════════════════════"
echo " QuantAI — First Live Fill — $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "════════════════════════════════════════════════════════════════"
echo ""

cd "$REPO"

# ── Step 1-3: Alpaca end-to-end test (fill + PostgreSQL + Pub/Sub) ─────────
echo "[$(date '+%H:%M:%S')] Running Alpaca connection test…"
python3 scripts/test_alpaca_connection.py \
    --symbol AAPL \
    --qty 1 \
    --result-file "$RESULT_FILE"

ALPACA_EXIT=$?
if [ $ALPACA_EXIT -ne 0 ]; then
    echo "[$(date '+%H:%M:%S')] ERROR: test_alpaca_connection.py exited $ALPACA_EXIT"
    exit $ALPACA_EXIT
fi

echo ""
echo "[$(date '+%H:%M:%S')] Alpaca test PASSED"

# ── Step 4: Update daily P&L (Day 1 of 90-day paper run) ───────────────────
echo "[$(date '+%H:%M:%S')] Updating daily_pnl table…"
python3 scripts/update_daily_pnl.py
echo "[$(date '+%H:%M:%S')] daily_pnl updated"

# ── Step 5: Print result summary ────────────────────────────────────────────
echo ""
echo "[$(date '+%H:%M:%S')] Result file: $RESULT_FILE"
if command -v python3 &>/dev/null && [ -f "$RESULT_FILE" ]; then
    python3 -c "
import json, sys
with open('$RESULT_FILE') as f:
    r = json.load(f)
print(f\"  success:          {r.get('success')}\")
print(f\"  market_was_open:  {r.get('market_was_open')}\")
print(f\"  symbol:           {r.get('symbol')}\")
print(f\"  fill_price:       {r.get('fill_price')}\")
print(f\"  filled_qty:       {r.get('filled_qty')}\")
print(f\"  alpaca_order_id:  {r.get('alpaca_order_id')}\")
print(f\"  fill_id:          {r.get('fill_id')}\")
print(f\"  pg_inserted:      {r.get('pg_inserted')}\")
print(f\"  pubsub_published: {r.get('pubsub_published')}\")
print(f\"  timestamp:        {r.get('timestamp')}\")
"
fi

# ── Step 6: Update CLAUDE.md with first live fill result ────────────────────
echo "[$(date '+%H:%M:%S')] Updating CLAUDE.md…"
python3 scripts/update_claude_md_with_fill.py
echo "[$(date '+%H:%M:%S')] CLAUDE.md updated"

echo ""
echo "[$(date '+%H:%M:%S')] run_first_live_fill.sh COMPLETE"
echo "════════════════════════════════════════════════════════════════"
echo ""
