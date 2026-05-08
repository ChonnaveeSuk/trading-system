# QuantAI Operations Runbook

This runbook covers the day-to-day operations, incident response, and emergency procedures for the QuantAI paper trading system running on Google Cloud Platform (GCP).

## Daily Operations

### Checking Daily Job Success
The system is orchestrated by Cloud Scheduler triggering a Cloud Run Job.
1. Check Telegram: The `morning_report.py` sends a daily summary alert around 22:05 UTC.
2. Check Cloud Logging manually if no alert was received:
   ```bash
   gcloud logging read 'resource.type="cloud_run_job" resource.labels.job_name="quantai-daily-runner"' --project quantai-trading-paper --limit 20
   ```
3. A successful run ends with the log line: `Sending morning report (level=SUMMARY)…`

### Verifying Fills
Fills from Alpaca are reconciled locally and published to BigQuery.
1. Check the local Postgres database:
   ```bash
   export DATABASE_URL="postgresql://quantai:<password>@/quantai?host=/cloudsql/quantai-trading-paper:asia-southeast1:quantai-postgres"
   psql $DATABASE_URL -c "SELECT * FROM fills ORDER BY timestamp DESC LIMIT 5;"
   ```
2. Verify BigQuery streaming:
   ```bash
   bq query --use_legacy_sql=false "SELECT * FROM quantai_trading.trades ORDER BY timestamp DESC LIMIT 5"
   ```

### Checking Gate Progress
The 90-day paper trading gate limits are tracked daily.
1. View the **QuantAI Paper Trading** Grafana Dashboard at `http://localhost:3000`.
2. Check the "Live Paper Gate — 90-Day Tracking" panels.
3. Validate manually via DB:
   ```bash
   python3 scripts/gate_progress.py
   ```

### Reading the Morning Report
The morning report outlines:
- **Market Regime**: Determines if the system is allowed to BUY (blocked in BEAR).
- **Signals**: Identifies whether the strategy is firing or flat.
- **P&L Summary**: The trailing equity growth.
- **Gate Progress**: Sharpe ratio and MaxDD.

*Abnormal Pattern:* If Sharpe is N/A after 5+ trades, or MaxDD spikes > 8%, it requires immediate review.

---

## Incident Response Procedures

### Scenario 1: Daily Job Failed
**Detection:** No Telegram morning report at 22:05 UTC, or Cloud Run logs show a crash.
**Diagnosis:**
```bash
gcloud run jobs executions list --job quantai-daily-runner --project quantai-trading-paper
# Find the failed execution ID
gcloud logging read 'resource.type="cloud_run_job" AND labels."run.googleapis.com/execution_name"="<EXECUTION_ID>" severity>=WARNING' --project quantai-trading-paper
```
**Manual Trigger / Recovery:**
```bash
gcloud run jobs execute quantai-daily-runner --region asia-southeast1 --project quantai-trading-paper
```

### Scenario 2: Stop Loss Triggered Unexpectedly
**Detection:** A `CRITICAL` Telegram alert stating "Stop Loss Triggered".
**Diagnosis:**
1. Check the stock's chart to confirm the drop.
2. Verify the `DELETE` position request was successful by checking Alpaca directly or the Cloud Logging output.
**Action:** The system handles stop losses automatically. Unless the data feed was corrupt (false print), accept the loss. If it was a corrupt feed, verify the position was actually closed on Alpaca and re-open it manually.

### Scenario 3: Sector Concentration Warning
**Detection:** Morning report shows a sector exposure > 30% or a position cap warning (e.g., 3/3 for `big_tech`).
**Diagnosis:** This means the safety gates are working and blocking new BUYs for that sector.
**Action:** No immediate action required. The system will naturally hold until MA crossovers trigger SELLs to free up capacity. Do not manually intervene unless exposure somehow exceeds 40%.

### Scenario 4: Cloud SQL Costs Spike
**Detection:** GCP Billing alert fires for the `quantai-trading-paper` project.
**Diagnosis:**
1. Common cause: Unnecessary backups or oversized storage.
2. Review Cloud SQL storage utilization:
   ```bash
   gcloud sql instances describe quantai-postgres --project quantai-trading-paper
   ```
**Action:** If DB size is bloated, verify the `ohlcv` table partitioning and drop older records.

### Scenario 5: Gate Metrics Look Wrong
**Detection:** Sharpe drops from 1.5 to 0.1 instantly, or MaxDD displays 50% arbitrarily.
**Diagnosis:** Usually caused by synthetic fills (e.g., `test_alpaca_connection.py`) leaking into the database or a corrupted `daily_pnl` row.
**Action:**
1. Investigate the `daily_pnl` table for nulls or massive jumps.
   ```bash
   psql $DATABASE_URL -c "SELECT * FROM daily_pnl ORDER BY trading_date DESC LIMIT 5;"
   ```
2. Delete the offending row and rerun the `update_daily_pnl.py` script.

---

## Maintenance Procedures

### Deploying New Code
```bash
# Build the Docker image
gcloud auth configure-docker asia-southeast1-docker.pkg.dev
docker build -f Dockerfile.runner -t asia-southeast1-docker.pkg.dev/quantai-trading-paper/quantai/runner:latest .
# Push the image
docker push asia-southeast1-docker.pkg.dev/quantai-trading-paper/quantai/runner:latest
# The Cloud Run job uses the 'latest' tag. Next run automatically uses the new code.
```

### Reseeding Historical Data
```bash
# Cloud Run (via Alpaca):
gcloud run jobs execute quantai-daily-runner --command="python3 scripts/seed_alpaca.py --days 30"
# Local (via Yahoo Finance):
python3 scripts/seed_yfinance.py --days 600
```

### Rotating API Keys
1. Update GCP Secret Manager:
   ```bash
   echo -n "<NEW_KEY>" | gcloud secrets versions add alpaca-api-key --data-file=- --project=quantai-trading-paper
   ```
2. The system pulls the latest secret version dynamically on the next run. No restart is required.

---

## Emergency Procedures

### Stop All Trading Immediately
To kill the scheduled cron job:
```bash
gcloud scheduler jobs pause quantai-daily-runner-cron --location asia-southeast1 --project quantai-trading-paper
```

### Cancel Open Orders and Close Positions
If a market event requires immediate liquidation:
1. Log in to the Alpaca Paper Trading Dashboard (Web UI) and click "Close All Positions" and "Cancel All Open Orders".
2. Alternatively, use curl:
   ```bash
   curl -X DELETE "https://paper-api.alpaca.markets/v2/positions" \
     -H "APCA-API-KEY-ID: <KEY>" \
     -H "APCA-API-SECRET-KEY: <SECRET>"
   ```
3. Run the daily P&L and Reconcile scripts to resync the Postgres DB to an empty book.
