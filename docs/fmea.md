# QuantAI: Failure Mode Analysis (FMEA)

## 1. Strategy Layer (`momentum.py`)

| Failure Mode | Probability | Impact | Current Mitigation | Recommended Mitigation |
| :--- | :--- | :--- | :--- | :--- |
| `yfinance` returns empty or truncated DataFrame. | HIGH | MEDIUM | Logs warning, returns `HOLD` signal. Fails safe. | Implement fallback to secondary data provider (e.g., Alpaca SIP). |
| `RSI` results in `NaN` due to insufficient warm-up bars. | MEDIUM | LOW | Defaults to a neutral 50.0 value and logs a debug statement. | Dynamically adjust lookback window to fetch exactly $N+2\times \text{RSI}_{period}$ bars. |
| `fast_ma` equals `slow_ma` exactly. | LOW | LOW | Evaluated as false for crossovers. Handled by `noise_filter_bps`. | None required. Noise filter acts as robust buffer. |
| `volume` array contains all zeros (e.g., FX pair). | HIGH | LOW | `sparse_volume` boolean flag auto-detects and bypasses volume checks. | None required. Existing sparse logic works perfectly. |
| Signal fires intraday during active market hours. | LOW | MEDIUM | Assumes closing price but executes at intraday market price, causing slippage. | Script execution bound to Cloud Scheduler at 22:00 UTC. Add check inside `run_live()` to abort if market is open. |
| SPY proxy data is $>30$ days stale. | LOW | CRITICAL | Defaults to `BULL` regime, exposing portfolio to massive crash risks. | Fail closed. If data is stale, force `BEAR` regime and halt trading. |
| VIXY undergoes reverse split, corrupting absolute pricing. | MEDIUM | CRITICAL | VIX filter absolute mode locks in perpetual `CALM` or `PANIC`. | Switch to `relative` mode (percentage over 252-day low) to neutralize structural price shifts. |
| Unmapped symbol passes through sector concentration. | MEDIUM | HIGH | Symbol defaults to "other" sector, potentially concentrating risk. | Raise exception or block signal if symbol is not found in `SYMBOL_TO_SECTOR`. |
| Stock undergoes a 10-for-1 stock split. | HIGH | HIGH | Indicator calculation (MA/ATR) sees a massive 90% gap-down, triggering false SELL. | Use split-adjusted OHLCV endpoints exclusively. |
| `portfolio_value` passed as 0 or negative. | LOW | LOW | Handled by division limits and max cap limits in ATR math. | Add explicit `assert portfolio_value > 0` before sizing logic. |

## 2. Execution Layer (`alpaca_direct.py`)

| Failure Mode | Probability | Impact | Current Mitigation | Recommended Mitigation |
| :--- | :--- | :--- | :--- | :--- |
| Alpaca returns `429 Too Many Requests`. | MEDIUM | HIGH | Logs HTTP Error and continues to next symbol. Signal is dropped for the day. | Implement Exponential Backoff with Jitter using the `tenacity` library. |
| Order partially fills. | LOW | LOW | Reconcile script maps filled quantity to DB. Remaining is cancelled at EOD. | Ensure DB constraints allow float matching for partial fills without throwing errors. |
| Position exists in Alpaca but missing from Postgres. | LOW | HIGH | Script sees Alpaca position, blocks double-buy. Syncs correctly next day. | None required. The Alpaca read is the source of truth. |
| Stop loss fires on the same day as a new `BUY`. | LOW | LOW | Stop-loss runs *before* signal loop. Clears equity for new buy. | Verify pattern Day Trade limits on Alpaca if frequency increases. |
| Alpaca API keys revoked or rotated incorrectly. | LOW | CRITICAL | `health_check()` fails, script aborts cleanly. | Setup automated Secret Manager rotation linked directly to Alpaca OAuth. |
| `unrealized_plpc` missing from position payload. | LOW | LOW | Handled gracefully. Logs warning and skips stop-loss evaluation. | None required. |
| `POST /orders` times out but executes on Alpaca's end. | LOW | HIGH | Request throws exception, script thinks it failed, but order is live. | Use `client_order_id` strictly. Alpaca idempotency will prevent double-execution if retried. |
| Sector Concentration math overflows on massive equity. | LOW | LOW | Python `Decimal` handles arbitrary precision natively. | None required. |
| PostgreSQL `INSERT` throws `CheckViolation` on bad payload. | MEDIUM | CRITICAL | `conn.rollback()` isolates the failure. Fails to JSONL fallback log. | Build an automated ingestion cron to sweep the JSONL log back into Postgres once fixed. |
| Alpaca paper environment undergoes maintenance reset. | LOW | HIGH | Positions wiped. Script assumes cash book and starts buying anew. | Add sanity check: if portfolio equity drops by 50% overnight, halt and alert. |

## 3. Infrastructure Layer (GCP)

| Failure Mode | Probability | Impact | Current Mitigation | Recommended Mitigation |
| :--- | :--- | :--- | :--- | :--- |
| Cloud SQL instance in `STOPPED` state at execution time. | HIGH | CRITICAL | Fails `psycopg2` connect, fallback to JSONL, no historical data to trade. | `run_daily.sh` executes `gcloud sql instances patch` to force start before Python script. |
| Cloud Run Job runs Out of Memory (OOM). | LOW | HIGH | Container crashes. Logs state `Exit Code 137`. | Monitor memory usage in Cloud metrics. Bump from 512MB to 1GB if limits approached. |
| GCP Secret Manager is unavailable. | LOW | CRITICAL | Job crashes instantly as env vars fail to resolve. | Cache non-critical keys locally, but accept crash for critical credentials (fails safe). |
| Cloud Scheduler misfires or misses schedule. | LOW | HIGH | No trades evaluated that day. | Create a Cloud Monitoring alert for "Job Execution Count == 0" between 22:00 and 23:00 UTC. |
| BigQuery streaming quota exceeded. | LOW | LOW | Pub/Sub messages route to Dead Letter Queue (DLQ). | Configure alerts on DLQ depth to manually replay messages. |
| GitHub Actions pushes broken Docker image `latest`. | MEDIUM | CRITICAL | Cloud Run executes broken code, potentially corrupting DB or firing bad trades. | Tag images by SHA. Update Terraform to use explicit digests instead of `latest`. |
| Region `asia-southeast1` suffers total outage. | LOW | HIGH | Trading halts. | Terraform variables configured to quickly spin up replica in `asia-northeast1` (Tokyo). |
| Dockerfile pulls malicious updated dependency from PyPI. | LOW | CRITICAL | Container executes malware with full Service Account permissions. | Pin dependencies using `pip-compile --generate-hashes`. |
| Terraform local state file corrupted or deleted. | MEDIUM | HIGH | Infrastructure loses tracking, cannot apply updates easily. | Migrate state to GCS bucket immediately. |
| Cloud Logging quota exceeded. | LOW | LOW | Logs dropped. | Configure log sinks to exclude noisy `DEBUG` channels from ingestion billing. |
