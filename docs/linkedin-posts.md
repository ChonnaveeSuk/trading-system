# QuantAI: LinkedIn Post Series

Here are 10 optimized LinkedIn posts designed to build professional authority, engage the quantitative and software engineering communities, and attract recruiter attention.

---

### Post 1: "I built a trading system that runs for $11/month on GCP"
Running a production-grade algorithmic trading system usually means burning hundreds of dollars a month on idle VMs. I refused to do that.

For my latest project, QuantAI, I migrated the entire compute layer to serverless Google Cloud Run Jobs triggered by Cloud Scheduler. The heavy lifting is done in 5 minutes at 22:00 UTC, and then the compute scales to exactly zero. 

To handle database persistence, I used a `db-f1-micro` Cloud SQL instance and wrote an automation script to toggle its activation policy right before the job runs. 

The result? A fully autonomous trading system—complete with CI/CD via GitHub Actions, IaC via Terraform, and audit logging to BigQuery—for roughly the cost of a Netflix subscription ($11/month).

If you are running once-a-day batch jobs on always-on architecture, you are overpaying. 

What is your favorite serverless hack for side projects?

#Serverless #GCP #CloudComputing #Python #AlgoTrading

---

### Post 2: "The $4,825 mistake that taught me about sector concentration"
Walk-forward backtesting is a liar if you don't account for cross-sectional correlation. 

My momentum strategy hit a spectacular 3.50 Sharpe ratio in backtesting. But in live paper trading, it blindly filled its maximum 10 position slots with precious metal mining stocks right as the sector began to aggressively roll over. 

Because the lagging moving average indicators hadn't crossed yet, the portfolio bled -$4,825 in unrealized losses in a single day. 

The fix wasn't tweaking the MA lengths. The fix was architectural: I hardcoded a sector exposure gate in the Order Management System. Now, the system physically rejects any `BUY` order that pushes a single sector beyond 30% of total portfolio equity. 

Backtests measure the asset in a vacuum. Live markets measure how assets crash together. 

How do you handle correlation risk in your trading or ML models?

#QuantitativeFinance #RiskManagement #Python #TradingSystems #LessonsLearned

---

### Post 3: "Why I use a 90-day gate before putting real money in my algo"
The fastest way to lose money in algorithmic trading is deploying a strategy because the backtest looked pretty. 

For my QuantAI system, I engineered a strict 90-day "Paper Trading Gate". Before a single dollar of real capital is allocated, the system must trade live, out-of-sample, for three months and hit three hard metrics:
1. Annualized Sharpe Ratio > 1.0
2. Maximum Drawdown < 15%
3. Profit Factor > 1.5

A dedicated python script (`gate_progress.py`) audits the database daily and updates a Grafana dashboard. If the strategy fails, it gets frozen. No in-place tweaking. It is treated as a falsified hypothesis.

If you don't have objective, falsifiable exit criteria before you go live, you aren't trading—you're gambling. 

What gates do you put in place before pushing critical code to production?

#AlgoTrading #SoftwareEngineering #DataScience #DevOps #RiskManagement

---

### Post 4: "Claude Code vs Gemini CLI: an honest comparison after 30 days"
I spent the last 30 days building a complex Rust and Python trading architecture using CLI-based AI agents. 

Here is what I learned: You have to know when to use the scalpel and when to use the sledgehammer. 

Agentic tools like Gemini CLI are brilliant orchestrators. I can point them at a messy directory of 50+ files and say, "Audit this codebase and write a markdown report on technical debt," and it will parallelize file reads and return a pristine document. 

But when it comes to hyper-specific logic—like debugging a Postgres `CheckViolation` that causes an aborted transaction trap in my Python loop—I still need to manually guide the logic. The AI is a senior peer programmer, not a replacement for domain expertise.

The real unlock isn't having the AI write the code; it is having the AI document, refactor, and write the unit tests for the code *you* designed. 

Are you using CLI agents in your daily workflow yet?

#ArtificialIntelligence #SoftwareDevelopment #Coding #Productivity #LLMs

---

### Post 5: "What FOMC blackouts taught me about production date handling bugs"
Timezones and dates will break your code. Guaranteed.

My momentum strategy uses an `EconomicCalendar` class to block `BUY` orders on the day of—and the day before—major macro events like FOMC meetings to avoid volatility whipsaws. 

But during a live run, it bought a stock the day before the Fed spoke. Why? Because the backtest loop evaluated dates using the *bar's* timestamp, while the live script evaluated using *today's* UTC datetime. By the time the script ran at 22:00 UTC, the market date and the server date had drifted out of alignment. 

The fix? Explicitly injecting an `as_of_date` parameter throughout the entire signal generation chain so the live system perfectly mimics the historical evaluation frame. 

Implicit state is the enemy of deterministic systems. 

What is the worst timezone bug you have ever shipped to production?

#Python #SoftwareEngineering #Debugging #TechNotes #Programming

---

### Post 6: "How I use Terraform to manage 92 cloud resources for a side project"
"Terraform is overkill for a personal project." I hear this all the time, and I completely disagree. 

My trading system relies on 92 distinct Google Cloud resources: Secret Manager payloads, Pub/Sub topics, Cloud Run Jobs, BigQuery datasets, and strict IAM bindings. 

If I configured all of this by clicking around the GCP Console, it would be an unmaintainable, undocumented mess. By writing it all in Terraform (`main.tf`, `cloud_sql.tf`, `cloud_run_jobs.tf`), I gained three things:
1. Complete disaster recovery (I can rebuild the entire cloud infra from scratch in 4 minutes).
2. Workload Identity Federation (GitHub Actions can deploy without storing long-lived JSON keys).
3. Total visibility into my architecture.

Infrastructure as Code isn't just for enterprise teams; it is the ultimate documentation tool for solo developers. 

Do you use IaC for your personal projects, or stick to the UI?

#Terraform #DevOps #GCP #InfrastructureAsCode #CloudEngineering

---

### Post 7: "Why I retired Rust and went Python-only (and why that's OK)"
I love Rust. I originally wrote my trading system's Order Management System and Risk Engine entirely in Rust for memory safety and sub-millisecond latency. 

But as the architecture evolved, I realized something important: My strategy trades once a day at 22:00 UTC. Sub-millisecond latency offers literally zero advantage for End-of-Day momentum trading. 

When I migrated the compute layer to serverless Cloud Run Jobs to save money, maintaining a persistent gRPC bridge between a Python strategy layer and a Rust execution engine became an architectural nightmare. 

So, I retired the Rust live path. I rewrote the risk execution logic into a pure Python REST client (`alpaca_direct.py`). It is slower, yes, but it is infinitely easier to deploy, debug, and monitor in a serverless environment.

Choose the right tool for the job, not just the language you want to write in.

Have you ever had to rewrite a beloved piece of code for pragmatism?

#RustLang #Python #SoftwareArchitecture #Engineering #TechChoices

---

### Post 8: "Gate metrics: Sharpe, MaxDD, Profit Factor — which one matters most?"
When evaluating a trading algorithm, everyone stares at the win rate. Stop doing that. 

For my QuantAI project, I completely stripped Win Rate out of the evaluation gate. Momentum systems inherently have low win rates (often ~40%); they survive by cutting losers fast and riding outliers. 

Instead, my 90-day production gate requires:
1. Max Drawdown < 15% (Capital Preservation)
2. Profit Factor > 1.5 (Gross Profits / Gross Losses)
3. Annualized Sharpe > 1.0 (Risk-adjusted return)

A 90% win rate strategy can blow up your account in a single trade. Profit Factor and Max Drawdown tell you if the system can actually survive a structural regime shift.

What metrics do you prioritize when evaluating statistical or ML models?

#DataScience #Quant #Finance #Statistics #AlgorithmicTrading

---

### Post 9: "Building AI agents for finance: what Anthropic's new tools mean for indie devs"
We are moving past LLMs as "chatbots" and entering the era of LLMs as "executors."

In Phase 6 of my QuantAI system, I am integrating the Anthropic SDK to build a "Morning Report Narrative Agent." Instead of just getting a Telegram alert with a raw P&L integer, the system queries the PostgreSQL database, retrieves the `pgvector` kNN similarity of the current market regime, and uses Claude to write a human-readable, context-aware analysis of why the portfolio shifted. 

The barrier to entry for building intelligent, multi-agent systems has never been lower. If you can write a clean API schema and a solid system prompt, you can automate senior-level analytical workflows.

How are you integrating LLM APIs into your traditional software stacks?

#ArtificialIntelligence #LLM #Anthropic #MachineLearning #SoftwareEngineering

---

### Post 10: "Day 9 of 90: honest update on my algorithmic trading experiment"
We are on Day 9 of the 90-day live paper trading gate for QuantAI, and the system is currently holding 24 shares of NVDA and 8 shares of META. 

The biggest lesson so far? Automation forces discipline. 

When the market got choppy last week, my human instinct was to manually intervene and flatten the book. But the code had already processed the moving averages, evaluated the RSI, checked the VIX proxy, and sized the positions according to the ATR risk limit. It held steady. 

Building an algorithmic system isn't just about math; it is about outsourcing your emotional control to a server in Singapore. 

I'll be posting regular updates as we push toward Day 90. If the gate passes, real money goes in. 

What is the hardest part about trusting your own code in production?

#BuildInPublic #AlgoTrading #Python #SoftwareDevelopment #QuantAI
