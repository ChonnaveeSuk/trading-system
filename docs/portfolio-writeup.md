# QuantAI: Production-Grade Algorithmic Trading System

**Role:** Senior Quantitative Developer / AI Engineer
**Links:** [GitHub Repository](https://github.com/ChonnaveeSuk/trading-system) | *Live Cloud Run deployment is private.*

## Project Summary
QuantAI is a fully autonomous, production-grade algorithmic trading system that executes a multi-layered momentum strategy across US equities. Built from scratch using a hybrid Python and Rust architecture, the system operates completely serverless on Google Cloud Platform, achieving a sub-3-minute CI/CD deployment pipeline and maintaining a highly resilient $11/month infrastructure footprint. It strictly enforces a 90-day paper-trading validation gate before ever allocating real capital.

## Technical Highlights
- **Serverless GCP Architecture:** Utilizes Cloud Scheduler, Cloud Run Jobs, Cloud SQL (PostgreSQL), Secret Manager, and BigQuery to operate reliably for just ~$11/month.
- **Hybrid Core Engine:** Core order management system (OMS) and risk rules originally written in Rust (compiled via Cargo/Tonic gRPC) for memory safety and latency. Shifted to a Python-only serverless live path for zero-maintenance operation.
- **Automated CI/CD:** GitHub Actions workflow building and pushing Docker images to Artifact Registry, completing full test and deployment cycles in under 3 minutes.
- **Infrastructure as Code (IaC):** 92 distinct GCP resources provisioned and managed entirely via Terraform.
- **Observability & Alerting:** Comprehensive tracking via a 29-panel Grafana dashboard, coupled with real-time Telegram alerts for executed trades, hard-stop triggers, and market regime shifts.

## Engineering Challenges Solved

**1. The "Precious Metals" Concentration Crash**
*Problem:* In April 2026, 10/10 of the portfolio's positions became concentrated in precious metals right before a massive correlated crash, causing a 5.3% drawdown.
*Solution:* Developed a hard-coded sector exposure gate mapping symbols to sectors (`big_tech`, `growth`, etc.), actively blocking any new `BUY` that would push a sector beyond 3 positions or 30% of total equity.

**2. Asynchronous Calendar Blackouts**
*Problem:* High-impact macro events (FOMC, CPI) caused unpredictable gap-downs, invalidating the technical indicators.
*Solution:* Built a `EconomicCalendar` and `EarningsCalendar` class that dynamically flags D-0 and D-1 blackout dates, suppressing `BUY` orders entirely while still allowing `SELL` orders to exit positions before the volatility hit.

**3. Cloud SQL Cost Optimization**
*Problem:* Traditional always-on architectures would run $30+/month, wasteful for a script that executes once daily for 5 minutes.
*Solution:* Migrated compute to stateless Cloud Run Jobs triggered by Cloud Scheduler. Cloud SQL was downsized to a `db-f1-micro` instance, cutting infrastructure overhead by over 70%.

**4. The "Aborted Transaction" Trap**
*Problem:* A rejected API status caused a PostgreSQL `CheckViolation`, plunging the `psycopg2` connection into an "aborted transaction" state and cascading failures across the entire reconciliation loop.
*Solution:* Refactored the database context manager to employ explicit `conn.rollback()` rescue logic around safe updates, completely isolating bad payload transactions from halting the execution thread.

**5. VIX Proxy Scaling Drift**
*Problem:* Standard VIX filters assumed absolute bounds (e.g. 20/30), but the tradable VIXY ETF decayed over time, invalidating the thresholds.
*Solution:* Designed a dual-mode evaluation framework that allowed switching between absolute and relative scaling `(VIXY_MA - low_252d) / low_252d`, dynamically adjusting the strategy's definition of "PANIC" to the local volatility environment.

## Skills Demonstrated

- **Systems Architecture:** Designing a distributed, message-driven system utilizing Pub/Sub and BigQuery for audit logging.
- **Quantitative Research:** Formulating walk-forward backtesting harnesses to validate Sharpe, MaxDD, and Profit Factor.
- **DevOps & IaC:** Writing modular Terraform scripts to instantiate secure VPC boundaries and Workload Identity Federation.
- **Database Engineering:** Designing robust PostgreSQL schemas, handling daily P&L migrations, and utilizing Cloud SQL Auth Proxies.
- **Python / Rust:** Exhibiting deep knowledge of Python (NumPy/Pandas vectorization) and Rust (Concurrency, Traits, gRPC).

## Key Metrics
- **Test Coverage:** 260+ passing tests (Rust + Python).
- **Cost:** ~$11 / month total operating expense.
- **Reliability:** 8 unique layers of protective gates (Regime, VIX, Earnings, Calendar, Trend, Stop-Loss, Sector, Position).
- **Validation:** Automated 90-day gating mechanism requiring Sharpe > 1.0 and MaxDD < 15% for live promotion.

## What I Would Do Differently
1. **Adopt dbt/Polars Early:** Pandas became a memory bottleneck during deep hyperparameter sweeps. Polars would have vectorized the walk-forward testing 10x faster.
2. **Start with a Simple Universe:** Beginning with 30 disparate symbols masked correlation risks. I should have built the sector limits before defining the asset pool.
3. **Avoid the Rust/Python gRPC Bridge for EOD Strategies:** Rust was amazing for the HFT Order Management System design, but an End-of-Day (EOD) momentum strategy running on Cloud Run never required sub-millisecond execution. I would stick to pure Python earlier for serverless compatibility.
