# QuantAI: Career Development Plan

**Target Role:** AI Engineer / Senior Quantitative Developer
**Location:** Bangkok, Thailand / Global Remote

## Current Skills Inventory & Market Value

The development of the QuantAI Trading System has built a highly marketable, cross-functional skill set. Here is the estimated market value of these skills in the current (2026) landscape.

| Skill / Technology | Depth Demonstrated in QuantAI | Estimated Market Rate (Bangkok) | Estimated Market Rate (Global Remote) |
| :--- | :--- | :--- | :--- |
| **Python (Pandas, NumPy, Pytest)** | Vectorized walk-forward backtesting, API integrations, comprehensive unit testing. | 100k - 150k THB / month | $80k - $130k USD / year |
| **Rust (Cargo, Tonic, gRPC)** | High-performance risk engine and Order Management System (OMS). Memory-safe concurrency. | 120k - 180k THB / month | $100k - $160k USD / year |
| **GCP (Cloud Run, SQL, Terraform)** | Full serverless CI/CD, IaC, Secret Manager, Workload Identity Federation. | 130k - 160k THB / month | $110k - $150k USD / year |
| **PostgreSQL / BigQuery** | Schema design, daily P&L migrations, analytical queries, JSON payload streaming. | 100k - 140k THB / month | $90k - $130k USD / year |
| **Docker & Linux (Ubuntu/WSL)** | Containerization of runners, bash scripting, cron orchestration. | Baseline expectation | Baseline expectation |
| **Algorithmic Trading / Finance** | Sharpe/MaxDD math, order lifecycle, Alpaca REST API, technical indicators (RSI/MA/BB). | Highly specialized premium | Highly specialized premium |
| **AI / ML (Claude, LLMs, Agents)** | Prompt engineering, multi-agent architectures (Phase 6 roadmap). | 150k - 200k THB / month | $120k - $180k USD / year |

## Skill Gap Analysis (Target: AI Engineer)

To transition from a "Quantitative Developer" to a pure "AI Engineer", the following gaps must be closed:

1. **Missing Skills:**
   - Deep understanding of Vector Databases (e.g., `pgvector`) beyond theoretical design.
   - Fine-tuning foundational open-source LLMs (Llama 3, Mistral) using LoRA/QLoRA.
   - Production deployment of RAG (Retrieval-Augmented Generation) pipelines using LangChain or LlamaIndex.
2. **Certifications (Optional but helpful):**
   - Google Cloud Professional Machine Learning Engineer (Validates GCP ecosystem knowledge).
   - DeepLearning.AI Generative AI with Large Language Models (Coursera).
3. **Projects to Demonstrate Capability:**
   - **Execute Phase 6 of QuantAI:** Implement the `pgvector` regime retrieval and the Anthropic SDK morning-report narrative. This bridges the gap between traditional quant dev and modern AI engineering.

---

## 3 Career Path Options

### Path A: Stay at RMUTK + QuantAI Side Project
- **Description:** Maintain current academic/staff position while running QuantAI on the side. Wait for the 90-day gate to pass and slowly scale real capital.
- **Income Projection:** 
  - Month 12: Base Salary + small algorithmic profits (~$50/mo).
  - Month 24: Base Salary + medium algorithmic profits (~$500/mo).
  - Month 36: Base Salary + compounding returns (highly variable).
- **Risk:** Low (Stable primary income).
- **Ceiling:** Medium (Constrained by personal capital for the trading account).

### Path B: Freelance ERPNext + Claude Consulting
- **Description:** Leverage existing ERPNext knowledge and integrate Claude API/n8n automation for local Thai SMEs. 
- **Income Projection:** 
  - Variable: 50k - 300k THB / month depending on client pipeline.
- **Client Acquisition:** Cold outreach on LinkedIn to operations managers in manufacturing/logistics in Bangkok. Offer a "free AI audit".
- **Risk:** Medium (Variable income, pipeline management overhead).
- **Ceiling:** High (Can scale into an agency model).

### Path C: Remote AI Engineering Role
- **Description:** Package QuantAI as a flagship portfolio piece and apply to remote US/EU startups or high-paying SEA tech hubs (Agoda, Line, Grab).
- **Target Companies:** Remote-first fintechs, proprietary trading firms, or AI-first product startups.
- **Application Strategy:** Direct outreach to engineering managers via LinkedIn, bypassing standard portals. "I built a production trading system for $11/mo on GCP; here is how I can optimize your infrastructure."
- **Timeline:** 3-6 months to first offer.
- **Salary Range (Bangkok-based):** 150,000 - 250,000 THB / month.

---

## 90-Day Career Action Plan

This plan runs parallel to the 90-day QuantAI paper trading gate (May - July 2026).

### Weeks 1-2 (May)
- **QuantAI:** Monitor Phase 5. Ensure the new 16-symbol tech universe stabilizes the Sharpe ratio.
- **Career:** Update LinkedIn profile, upload `portfolio-writeup.md` as a featured article. Push the latest QuantAI code to a clean public GitHub repository (sanitized of any secrets).

### Weeks 3-4 (May)
- **QuantAI:** Begin drafting Phase 6 ML implementations locally (do not deploy to live yet). Set up a local `pgvector` test instance.
- **Career:** Draft and schedule the 10 LinkedIn posts. Begin connecting with 5 Engineering Managers or Lead Quants per day.

### Month 2 (June)
- **QuantAI:** If Sharpe > 1.0, begin preparing the real-money Alpaca live account API keys.
- **Career:** Apply to 10 highly targeted Remote AI Engineering / Senior Dev roles (Path C). Attend at least one local Bangkok tech/AI meetup to network for Path B.

### Month 3 (July)
- **QuantAI:** Day 90 evaluation. If the gate passes, deploy $1,000 real money. If it fails, freeze the strategy and write the post-mortem.
- **Career:** Interview looping. Shift focus to technical interview prep (LeetCode, System Design) and finalizing contract negotiations.

---

## LinkedIn Profile Optimization

### Headline
**Senior AI Engineer & Quantitative Developer | Python, Rust, GCP | Building autonomous, serverless trading systems | ERPNext & LLM Consultant**

### About Section
I am a software architect and quantitative developer obsessed with building resilient, highly automated systems. Most recently, I architected and deployed "QuantAI", a production-grade algorithmic trading system executing a momentum strategy across US equities. Built from scratch with a hybrid Python/Rust stack, the system operates completely serverless on Google Cloud Platform (Cloud Run, Cloud SQL, BigQuery) and is managed entirely via Terraform—all while maintaining an infrastructure footprint of just $11/month.

I specialize in crossing the boundaries between quantitative finance, DevOps, and Artificial Intelligence. Whether it's writing memory-safe Rust for latency-critical Order Management Systems, vectorizing Pandas DataFrames for walk-forward backtesting, or integrating Large Language Models (Claude) for automated financial reporting, I build systems that require zero human intervention.

Currently based in Bangkok and open to remote Senior Software Engineering or AI Engineering roles where I can leverage my expertise in GCP, Python, and scalable architecture to solve complex, data-heavy problems.

**Key Technologies:** Python, Rust, PostgreSQL, Google Cloud Platform (GCP), Terraform, Docker, GitHub Actions, LLM APIs (Anthropic/OpenAI).
