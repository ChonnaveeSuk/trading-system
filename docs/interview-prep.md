# QuantAI: Technical Interview Prep Guide

## 1. The 30-Second Elevator Pitch
"I architected and built QuantAI, a production-grade algorithmic trading system that executes a multi-layered momentum strategy across US equities. It operates completely serverless on Google Cloud Platform, utilizing Cloud Run and Cloud SQL, and costs just $11 a month to run. I wrote the execution pipeline in Python, managed the infrastructure with Terraform, and instituted a rigid 90-day paper-trading validation gate that demands strict Sharpe and Drawdown thresholds before it ever touches real capital."

## 2. The 2-Minute Technical Overview
"QuantAI is a daily-frequency quantitative system designed for maximum resilience and minimal cloud overhead. The core strategy evaluates 16 tech-focused assets using a Dual Moving Average crossover logic, enhanced by RSI mean-reversion filters and ATR-based adaptive position sizing. 

To ensure survival, the algorithm passes signals through 8 distinct protection layers, including a macro market regime filter, a VIX proxy volatility block, calendar blackout windows for FOMC events, and a hard-coded sector concentration gate.

The infrastructure is entirely serverless. A GCP Cloud Scheduler triggers a Dockerized Python Cloud Run Job once a day at market close. It boots up a PostgreSQL Cloud SQL instance via the gcloud CLI, runs the Pandas-vectorized math, submits orders directly to the Alpaca REST API, streams the audit logs to BigQuery via Pub/Sub, and instantly spins the database back down to scale costs to near-zero. The entire pipeline is CI/CD automated via GitHub Actions and governed by 92 Terraform resources."

---

## 3. Deep Dive Answers

### Python Questions

**"Explain how your vectorized backtest works"**
*Model Answer:* "Instead of iterating row-by-row over a Pandas DataFrame—which is highly inefficient due to Python's GIL and object overhead—I utilize native Pandas rolling windows. For instance, to calculate the 15-day moving average, I apply `close.rolling(15).mean()`, which drops down to highly optimized C code under the hood. To detect crossovers, I use vectorized boolean logic: `bullish_cross = (prev_fast <= prev_slow) & (curr_fast > curr_slow)`. This approach allows the system to backtest years of OHLCV data across multiple symbols in milliseconds."
*What NOT to say:* Do not say "I used a for loop over the DataFrame."

**"How do you handle NaN in pandas?"**
*Model Answer:* "NaN values naturally occur during the 'warm-up' period of indicators. For example, a 15-day MA will yield NaNs for the first 14 days. I handle this explicitly. Instead of blindly calling `.fillna(0)`, which skews mathematical averages, I verify data sufficiency at the start of the function (`len(df) < required_bars`). If a NaN propagates into a signal score, the function detects it via `np.isnan()` and gracefully returns a `HOLD` signal with a score of 0.0, failing safe."

### System Design Questions

**"Design a trading system for 1000 symbols"**
*Model Answer:* "The current synchronous loop fetching 90 days of data per symbol will hit an N+1 bottleneck at 1000 symbols. I would restructure the database layer to perform a single batch fetch for all active symbols using the `IN` clause. Then, I would utilize Python's `asyncio` with `aiohttp` to parallelize the external Alpaca API calls for position lookups and order submissions. On the compute side, the Pandas math is fast enough to handle 1000 symbols, but if memory becomes a constraint, I would distribute the evaluation across multiple parallel Cloud Run workers using Pub/Sub fan-out architecture."

**"How would you add real-time signals?"**
*Model Answer:* "Currently, the system is an End-of-Day batch job. To transition to real-time, I would shift from REST polling to a WebSocket-driven architecture. I would spin up a persistent Kubernetes pod or Compute Engine instance. The engine would subscribe to Alpaca's SIP websocket feed, pushing tick data onto a Redis stream. The evaluation logic would shift from heavy Pandas rolling windows to incremental, event-driven math, updating the moving averages purely based on the delta of the newest incoming tick."

### GCP/Infrastructure Questions

**"Why Cloud Run Jobs over Kubernetes (GKE)?"**
*Model Answer:* "Cost and operational overhead. GKE requires a persistent control plane and node pools, meaning a baseline cost of $70+ per month even if nothing is executing. Because my algorithm only evaluates data once a day at 22:00 UTC, paying for 23 hours and 55 minutes of idle compute is architectural waste. Cloud Run Jobs allow me to package the exact same Docker container, trigger it on a cron schedule, pay by the millisecond of execution, and scale to exactly zero. It costs me literally $0.00 in compute."

**"How do you manage secrets?"**
*Model Answer:* "I never hardcode API keys or database passwords in source code or `.env` files committed to Git. I provision Google Secret Manager resources via Terraform. During CI/CD, GitHub Actions authenticates to GCP using Workload Identity Federation—meaning no static JSON keys are used. At runtime, the Cloud Run Job retrieves the secrets securely into environment variables native to the container, isolating them completely from the filesystem."

### Behavioral Questions

**"Tell me about a production incident"**
*Model Answer:* "During the Phase 5 paper trading run, my portfolio lost almost 5% in a single day because the strategy went 100% long on precious metal miners just as the sector crashed. The lagging moving averages didn't exit fast enough. I immediately halted trading and performed a 5-Whys root cause analysis. I realized walk-forward backtesting completely ignores cross-sectional correlation. To fix it, I architected a hard-coded Sector Concentration Gate directly in the execution client that physically rejects orders pushing any sector beyond 30% exposure. It was a painful paper loss, but it forced an institutional-grade risk upgrade."

### Finance/Quant Questions

**"Why is your backtest Sharpe suspicious?"**
*Model Answer:* "My backtest yielded a 3.50 Sharpe ratio, which is statistically improbable for a basic momentum strategy. A real quant knows this is an artifact. First, the strategy is long-only and was backtested across the massive 2024-2025 AI tech bull run. It wasn't extracting pure Alpha; it was heavily capturing structural Beta. Second, the Sharpe denominator was artificially shrunk by ignoring cash-drag on days the regime filter blocked trading. The true out-of-sample expected Sharpe is likely around 1.0, which is exactly why the 90-day live paper gate exists—to prove the math in real-time without the benefit of hindsight."

**"How do you prevent overfitting?"**
*Model Answer:* "I strictly utilize Walk-Forward optimization rather than in-sample curve fitting. The algorithm optimizes its parameters over a 252-day window, but its performance is strictly recorded over the subsequent 63-day unseen out-of-sample window. However, this doesn't protect against regime-overfitting if the entire dataset is a single bull market. To truly prevent overfitting, the system demands out-of-sample live paper validation, and parameter tuning is strictly locked out once the gate begins."
