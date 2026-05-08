# QuantAI: Competitive Analysis

## 1. TradingAgents (GitHub: 29.9k stars)
**Overview:** TradingAgents is a wildly popular open-source repository that relies on a multi-agent Large Language Model (LLM) approach. It utilizes specialized agents (e.g., Bull Researcher, Bear Researcher, Sentiment Analyst) that debate market conditions and formulate trading decisions based on parsed news and social media sentiment.

**How it differs from QuantAI:**
- **Paradigm:** TradingAgents is a qualitative, NLP-driven system relying on the emergent reasoning of LLMs. QuantAI is a strict quantitative, deterministic system relying on pure price action, moving averages, and hardcoded mathematical risk gates.
- **Reproducibility:** LLM outputs are inherently non-deterministic. A backtest of TradingAgents today might yield a different result than tomorrow. QuantAI's walk-forward backtests are 100% mathematically deterministic.
- **Latency/Cost:** Running 5 LLM agents for daily decisions incurs significant API costs (OpenAI/Anthropic). QuantAI's mathematical evaluation costs literally $0.00 in compute.

**Integration Potential:**
QuantAI could integrate the TradingAgent paradigm purely as a **Risk Overlay** (Phase 6). Instead of making BUY/SELL decisions, a "Macro Sentiment Agent" could act as the 9th Protection Layer. If the agent detects overwhelming bearish sentiment in fundamental news, it triggers a `CAUTION` state, halving position sizes similar to the VIX filter.

---

## 2. Momentum-Investing Repo (tanish35)
**Overview:** A highly regarded implementation of traditional momentum strategies using cross-sectional ranking. It ranks a wide universe of stocks, applies FIP (Frog-in-the-Pan) scoring to penalize choppy momentum, and utilizes skewness filters to avoid assets prone to sudden crashes.

**What QuantAI is missing:**
- **Cross-Sectional Ranking:** QuantAI evaluates each of its 16 symbols in a vacuum (Absolute Momentum). It will buy all 16 if they all cross their MAs. Tanish35's repo ranks them (Relative Momentum) and only buys the top N, ensuring capital is always deployed to the strongest assets.
- **Quality of Momentum:** QuantAI treats a volatile 10% gain the same as a smooth 10% gain. Tanish35 uses FIP scoring to favor slow, continuous information diffusion (smooth uptrends) over gap-driven, news-based spikes.

**Recommendation:**
QuantAI's Phase 6 roadmap must implement cross-sectional ranking. This solves the capital allocation problem when >10 BUY signals fire simultaneously, moving the system from "first-come, first-served" to "best-in-class".

---

## 3. Commercial Platforms (QuantConnect, Alpaca, Interactive Brokers)
**Overview:** Platforms that provide end-to-end backtesting, data feeds, and live execution engines hosted on their own servers.

**What they offer that QuantAI doesn't:**
- **Tick-Level/Options Data:** QuantConnect has petabytes of clean, survivorship-bias-free data spanning decades, including options and futures. QuantAI relies on free yfinance/Alpaca daily OHLCV.
- **Managed Execution:** Hosted platforms handle order routing, slippage modeling, and margin requirements natively.
- **Speed:** Co-located servers near exchanges offer microsecond latency.

**What QuantAI does better:**
- **Absolute Control:** QuantAI isn't locked into proprietary C# or Python libraries (`QCAlgorithm`). The entire pipeline (Postgres $\rightarrow$ Cloud Run $\rightarrow$ Alpaca REST) is owned, debuggable, and extensible.
- **Cost:** Commercial platforms charge hefty monthly subscription fees for live nodes and data. QuantAI operates for ~$11/month on GCP.
- **Portability:** The strategy logic can be disconnected from Alpaca and wired into Interactive Brokers simply by writing a new REST bridge, without rewriting the core momentum math.

**Migration Path:**
If QuantAI scales beyond $100k, managing local PostgreSQL instances and API rate limits becomes a liability. The mathematical logic of `momentum.py` should be ported into the QuantConnect LEAN engine for institutional-grade execution, while retaining the GCP infrastructure for analytical reporting and BigQuery archiving.

---

## 4. Hermes Agent + Pi Agent (LLM Workflows)
**Overview:** Advanced autonomous coding and operational agents capable of executing complex workflows across terminal environments.

**Could they replace the Claude Code / Gemini CLI workflow?**
- Currently, QuantAI utilizes CLI-based agents as orchestrators and peer programmers. Deeply autonomous agents like Hermes could, theoretically, be given SSH access to a GCP instance to actively monitor the logs, rewrite broken Python functions on the fly, and push hotfixes during a live market session.

**Trade-offs:**
- **Risk of Catastrophe:** Allowing an autonomous agent write-access to live trading logic is a catastrophic risk. A hallucination could modify `_MAX_POSITION_PCT` from 0.05 to 0.50, destroying the portfolio.
- **Context Limitations:** While they are excellent at isolated tasks, deep codebase audits (like tracing a Rust gRPC failure to a PostgreSQL `CheckViolation`) still heavily benefit from human architectural intuition.

**Hybrid Workflow Recommendation:**
Keep the agents strictly in the `development` and `research` environments. Use them to write test coverage, format documentation, and draft boilerplate API integrations. Human authorization must strictly govern all PR merges and Terraform applies to production.
