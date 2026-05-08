<div align="center">

# 📈 QuantAI Trading System

**A production-grade, serverless algorithmic trading architecture running on GCP for $11/month.**

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg?style=flat&logo=python&logoColor=white)](https://www.python.org)
[![GCP](https://img.shields.io/badge/Google_Cloud-Serverless-4285F4.svg?style=flat&logo=google-cloud&logoColor=white)](https://cloud.google.com/)
[![Terraform](https://img.shields.io/badge/Terraform-IaC-623CE4.svg?style=flat&logo=terraform&logoColor=white)](https://www.terraform.io/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Cloud_SQL-336791.svg?style=flat&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

*Stop burning hundreds of dollars on idle VMs. Trade the markets with institutional-grade risk management and zero server maintenance.*

</div>

---

## ⚡ Overview
QuantAI is a fully autonomous algorithmic trading pipeline. It executes a multi-layered momentum strategy across US equities, heavily protected by strict volatility filters and hard-coded sector limits. Instead of relying on expensive always-on compute, the entire execution layer is Dockerized and triggered via Google Cloud Scheduler, processing the daily math and scaling to exactly zero. 

It is fast, mathematically rigorous, and ruthlessly cheap.

## 🏗️ Architecture

```text
  GitHub CI/CD ──────> Artifact Registry
       │                        │
       ▼                        v
 Cloud Scheduler ───>  Cloud Run Job (Python) ───> Alpaca Trading API (REST)
                            │   ▲                              │
            (Secret Manager)│   │                              │ (Fills)
                            ▼   │                              v
                       Cloud SQL (PostgreSQL)           GCP Pub/Sub ──> BigQuery
```

## 📊 Key Metrics

| Metric | Value |
| :--- | :--- |
| **Cloud Cost** | ~$11 / month |
| **Strategy** | Dual MA Crossover + RSI Mean-Reversion |
| **Test Coverage** | 260+ passing tests (Pytest) |
| **Infrastructure** | 92 Terraform resources |
| **Protections** | 8 hard-coded safety gates |

## 🚀 Quick Start (Local Backtesting)

You can run the walk-forward backtest locally without deploying to GCP.

```bash
# 1. Clone the repository
git clone https://github.com/QuantAI/trading-system.git
cd trading-system

# 2. Spin up local Postgres
docker compose up -d postgres

# 3. Install dependencies
pip install -r strategy/requirements.txt

# 4. Seed historical data (Requires yfinance)
python scripts/seed_yfinance.py --days 600

# 5. Run the walk-forward backtest
python strategy/run_strategy.py --mode backtest
```

## 🔥 Features

- 🧠 **Walk-Forward Validation:** No curve fitting. Evaluates models strictly out-of-sample.
- 🛡️ **8-Layer Risk Engine:** Macro regime blocks, VIX proxies, calendar blackout windows (FOMC/CPI), and hard stop-losses.
- 🧱 **Sector Concentration Limits:** Physically rejects orders that push a single sector beyond 30% of the portfolio (Learning from our $4k paper loss).
- ☁️ **Scale-to-Zero GCP:** Uses Cloud Run Jobs and Cloud SQL toggle automation to keep active compute costs under $3/month.
- 🔐 **Keyless Deployment:** Workload Identity Federation allows GitHub Actions to deploy safely without static JSON keys.
- 📉 **BigQuery Auditing:** Natively streams all trade execution data to BigQuery via Pub/Sub for analytical permanence.

## 🚧 The 90-Day Paper Gate Framework

We do not trust backtests. Before a single real dollar touches the market, the algorithm must trade live out-of-sample data on the Alpaca Paper API for 90 consecutive days. 

To pass, it must hit:
1. **Annualized Sharpe Ratio > 1.0**
2. **Maximum Drawdown < 15%**
3. **Profit Factor > 1.5**

If it fails, the code is frozen and the hypothesis is falsified. No in-place tweaking.

## 🛠️ Tech Stack
- **Compute:** Python 3.11, Pandas, NumPy, Google Cloud Run Jobs
- **Infrastructure:** Terraform, Cloud Scheduler, Secret Manager
- **Database & Auditing:** Cloud SQL (PostgreSQL), GCP Pub/Sub, BigQuery
- **Brokerage:** Alpaca Markets REST API
- **Observability:** Grafana, Telegram API

## 💡 Lessons Learned
1. **Backtests lie about correlation:** A Sharpe ratio of 3.50 is usually just 15 tech stocks surfing the same Beta wave. If you don't limit sector concentration, your portfolio is a ticking time bomb.
2. **Sub-millisecond latency is overrated:** Writing the OMS in Rust was fun, but useless for a daily End-of-Day strategy. Python in a serverless container is 10x easier to maintain.
3. **Timezones will break you:** Evaluate signals using the *timestamp of the market data bar*, not the `datetime.today()` of your server clock. 

## 🤝 Contributing
Pull requests are welcome. Please ensure you install the pre-commit hooks to prevent secret leakage:
```bash
pip install pre-commit
pre-commit install
```

## 📜 License
Distributed under the MIT License. See `LICENSE` for more information.

---
*If you found this architecture useful, please consider giving the repo a ⭐!*
