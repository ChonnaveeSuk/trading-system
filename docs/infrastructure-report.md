# QuantAI: Infrastructure Cost Optimization Report

## Current Architecture Diagram

```text
                                +-----------------------+
                                |                       |
                                |  GitHub Actions CI/CD |
                                |  (Workload Identity)  |
                                |                       |
                                +-----------+-----------+
                                            | (Docker Push)
                                            v
                                +-----------------------+
                                |   Artifact Registry   |
                                |   (Docker Images)     |
                                +-----------+-----------+
                                            |
+-------------------+                       v                      +-----------------+
|                   |        +-----------------------------+       |                 |
|  Cloud Scheduler  |--trigger->  Cloud Run Job            |       |  Secret Manager |
|  (22:00 UTC)      |        |  (quantai-daily-runner)     |<------|  (API Keys, DB) |
|                   |        +-----------------------------+       |                 |
+-------------------+          |    |                   |          +-----------------+
                               |    |                   |
        +----------------------+    |                   +-------------------------+
        |                           |                                             |
        v                           v                                             v
+-------------------+      +-------------------+                          +-----------------+
|                   |      |                   |                          |                 |
|   Alpaca API      |      |   Telegram API    |                          |    Cloud SQL    |
|   (REST)          |      |   (Reporting)     |                          |   PostgreSQL    |
|                   |      |                   |                          |  (db-f1-micro)  |
+-------------------+      +-------------------+                          +-----------------+
                                                                                  |
                                                                                  |
                                                                                  v
+-------------------+      +-------------------+                          +-----------------+
|                   |      |                   |                          |                 |
|     BigQuery      |<-------    Pub/Sub       |<-------------------------| Cloud Scheduler |
|  (Audit Trails)   |      |  (Fills, Risk)    |                          | (02:00 Backup)  |
|                   |      |                   |                          |                 |
+-------------------+      +-------------------+                          +-----------------+
                                                                                  | (Dump)
                                                                                  v
                                                                          +-----------------+
                                                                          |                 |
                                                                          | Cloud Storage   |
                                                                          | (90-day gzip)   |
                                                                          +-----------------+
```

## Cost Analysis

### Current Base: ~$11/month
The system achieves extreme efficiency by operating purely serverless, with the only persistent compute cost being the relational database.
- **Cloud SQL (`db-f1-micro` + 10GB SSD):** ~$9.47/month (The largest cost center).
- **Artifact Registry:** ~$0.50/month (Storage for Docker images).
- **Cloud Run Jobs:** ~$0.00/month (Falls entirely within the free tier of 2M requests/month).
- **Pub/Sub & BigQuery:** ~$0.00/month (Volume is well under the 10GB / 1TB free tiers).
- **Secret Manager & Cloud Scheduler:** ~$0.00/month (Within free tier).
- **Cloud Storage (Backups):** ~$0.10/month.

### Cost Projections at Scale
- **At 50 symbols:** ~$11/month. Serverless execution time increases from 2 minutes to 4 minutes per day. Cloud Run costs remain in the free tier. Database storage grows slightly faster but stays under 10GB.
- **At 200 symbols:** ~$12/month. Cloud Run execution might require a memory bump (512MB $\rightarrow$ 1GB) to handle parallel Pandas operations, costing pennies more per month. Database disk IO increases.
- **At real money ($10k):** ~$11/month. Infrastructure does not differentiate between paper and live trading. However, a paid data feed (e.g., Alpaca SIP) might be required, adding external non-GCP costs.
- **At real money ($100k):** ~$40/month. At this tier, the `db-f1-micro` should be upgraded to a `db-g1-small` or standard tier to ensure maximum reliability and lower query latency during critical Stop-Loss liquidation loops. 

---

## Cost Optimization Opportunities

Even at $11/month, further optimizations are possible for a "zero-cost" side project:

1. **Cloud SQL Start/Stop Automation (Implemented):** 
   The `MANAGE_CLOUD_SQL=1` environment variable currently toggles the `activation_policy` of Cloud SQL via the gcloud CLI. It spins up the database 1 minute before the Cloud Run Job fires and spins it down immediately after. This saves ~$7.55/month (~70% of the SQL cost), bringing the real active cost down to under **$3/month**.
2. **Migrate to Serverless Postgres:** 
   Replace Google Cloud SQL entirely with Neon, Supabase, or Xata. These providers offer true scale-to-zero serverless PostgreSQL with generous free tiers, bringing the database cost to exactly $0.00/month.
3. **SQLite on Cloud Storage (Litestream):**
   Since the QuantAI architecture is a single-writer, once-a-day batch job, a full Postgres instance is architectural overkill. Migrating to SQLite and replicating to GCS via Litestream would reduce database costs to $0.00/month and simplify the Terraform stack.
4. **Aggressive Artifact Registry Retention:**
   Docker images cost $0.10/GB/month. A heavily updated CI/CD pipeline will accumulate gigabytes quickly. Implement a Terraform lifecycle policy to retain only the 3 most recent images and delete untagged layers immediately.
5. **Consolidate Backup and Runner Jobs:**
   Currently, `quantai-daily-runner` (22:00) and `quantai-backup` (02:00) are separate jobs. Combining them into a single sequential script (`run_daily.sh && backup_postgres.sh`) eliminates the need to spin up the Cloud SQL instance a second time, saving fractional compute and simplifying the Cloud Scheduler configuration.

---

## Reliability Analysis

### Single Points of Failure (SPOFs)
1. **Cloud SQL Availability:**
   - *Failure:* If Cloud SQL fails to start or crashes during the job, the system cannot fetch historical OHLCV data or record orders.
   - *Impact:* The script crashes, no trades are evaluated, and the system stays flat for the day. (Fails safe).
   - *Mitigation:* The `_db.py` fallback logs failed orders to a JSONL file, but the job itself has `max_retries=1` configured in Terraform to attempt a retry.
2. **Alpaca API Outage:**
   - *Failure:* The REST API returns 500s or timeouts.
   - *Impact:* `test_alpaca_connection.py` fails during the daily pre-flight check. Orders are not submitted.
   - *Mitigation:* The Python script evaluates positions using the last known database state, but actual liquidation (stop-losses) cannot occur. This is an unmitigable risk for any external broker dependency.
3. **Cloud Run Job Timeout:**
   - *Failure:* The job exceeds the 3600s (1 hour) timeout due to Pandas hanging or an infinite retry loop on network calls.
   - *Impact:* The job is killed abruptly. Database transactions might be left hanging if not properly managed.
   - *Mitigation:* Explicit `conn.rollback()` rescue logic exists for database constraints, but the job's Python code should enforce a strict `socket.setdefaulttimeout()` for all outgoing HTTP requests.

### Disaster Recovery Procedures
1. **Total Database Loss:**
   - Cloud SQL `quantai-postgres` is accidentally deleted.
   - *Recovery:* Reapply `terraform apply` to recreate the instance. Fetch the latest `YYYY-MM-DD.sql.gz` from the `quantai-backups` GCS bucket. Import the dump. Run `seed_yfinance.py` to backfill any missing days since the backup. Time to recovery: ~15 minutes.
2. **Broker Desync (Ghost Positions):**
   - *Recovery:* Manually liquidate all positions via the Alpaca web dashboard. Truncate the local PostgreSQL `positions` table. The daily `reconcile_alpaca_fills.py` script will automatically resync the local database to an empty book on the next run.

---

## Scaling Roadmap

### Stage 1: Paper Trading (Current)
- **Infrastructure:** Serverless Cloud Run + Cloud SQL f1-micro.
- **Risk:** Gated by 90-day `gate_progress.py` metrics (Sharpe > 1.0, MaxDD < 15%).
- **Cost:** ~$11/month.

### Stage 2: Real Money Proof of Concept ($1,000 - $2,000)
- **Infrastructure:** No changes. Re-point Secret Manager keys to Alpaca Live credentials.
- **Risk:** Halve the position sizing (`atr_risk_pct = 0.005`). Enable real-time SMS alerts (PagerDuty) for the stop-loss trigger.
- **Cost:** ~$11/month + Potential SIP data feed costs.

### Stage 3: Growth ($10,000+)
- **Infrastructure:** Disable the Cloud SQL Start/Stop automation. Leave the database running 24/7 to allow intraday querying from Looker Studio and external dashboards without cold starts.
- **Risk:** Implement a secondary execution broker (e.g., Interactive Brokers) as a failover if Alpaca goes down.
- **Cost:** ~$20/month.

### Stage 4: Institutional ($100,000+)
- **Infrastructure:** Upgrade Cloud SQL to `db-custom-1-3840` (1 vCPU, 3.75GB RAM) with High Availability (Regional cross-zone replication). Migrate from serverless Cloud Run Jobs to an always-on Kubernetes (GKE) cluster or persistent Compute Engine instances running the original Rust gRPC engine for latency advantages.
- **Risk:** Full redundancy. Multiple data feeds (Polygon + Alpaca + IBKR) cross-referenced to prevent bad ticks from triggering false stop-losses.
- **Cost:** ~$150 - $300/month.
