# QuantAI: Phase 6 Technical Specification (ML/AI Enhancement Layer)

## Feature 1: Cross-Sectional Momentum Ranking

### Description
Currently, QuantAI utilizes *Absolute Momentum* (buying any stock that crosses its MA limit). This causes capital allocation bottlenecks when multiple stocks trigger simultaneously. Phase 6 will implement *Relative Momentum*, ranking the universe and allocating capital only to the top percentiles.

### Mathematical Formula
Calculate the 30-day rate of return normalized by 30-day volatility:
$\text{Momentum Score}_i = \frac{P_{t, i} - P_{t-30, i}}{P_{t-30, i}} \times \frac{1}{\sigma_{30, i}}$

### Implementation Plan
- **Code Structure:** Create `strategy/src/signals/ranking.py`. Instead of evaluating symbols iteratively in a vacuum, pass the entire cross-sectional `pandas.DataFrame` matrix (Prices x Symbols) to the function.
- **Risk Considerations:** If the entire market is crashing, the "top ranked" stock is simply the one losing the least. Absolute momentum filters (the 200-day MA trend) must still gate the final execution.
- **Tests Needed:** Mock a scenario where 10 symbols cross their MA, but only the 3 with the highest 30-day normalized return generate `BUY` signals.

---

## Feature 2: `pgvector` Regime Detection

### Description
Instead of relying on a binary `SPY < 200MA` check for market regimes, we will embed historical market conditions as vectors and use K-Nearest Neighbors (kNN) to find the most similar historical trading environments.

### Implementation Plan
- **Embedding Features:** For each trading day, construct a 5-dimensional vector: `[SPY 30d Return, VIXY Level, Fed Funds Rate, 10Y Yield, High-Yield Credit Spread]`.
- **Database Schema Changes:** Enable the `pgvector` extension in PostgreSQL. Add a `regime_vectors` table containing the `embedding vector(5)` and `forward_60d_return`.
- **Similarity Matching:** On the daily run, query the DB for the top 5 nearest neighbors using Cosine Similarity (`<=>`). If the average forward return of those 5 historical analogues is deeply negative, block `BUY` orders.
- **Code Structure:** Add `strategy/src/filters/vector_regime.py`.

---

## Feature 3: LLM Morning Report Narrative

### Description
Integrate the Anthropic SDK (Claude 3.5 Sonnet/Haiku) to generate human-readable narratives explaining the portfolio's daily P&L and risk shifts.

### Implementation Plan
- **Data Payload:** Dump the output of `daily_pnl`, the newly fired `signals`, and the `VIX/Regime` state into a JSON object.
- **Prompt Engineering:** *"You are a senior quantitative risk manager. Analyze this JSON payload of today's algorithmic trading results. In 3 bullet points, explain why the portfolio gained/lost value, highlighting specific sector concentration limits or stop-losses that triggered."*
- **Code Structure:** Update `scripts/morning_report.py`. Use the `anthropic` pip package.
- **Fallback:** Wrap the API call in a `try/except`. If Anthropic times out, gracefully fall back to the existing hardcoded integer Telegram message.

---

## Feature 4: Earnings Surprise Alpha Signal

### Description
Fundamental overlay. Post-earnings drift is a documented anomaly. A massive earnings beat often under-reacts on Day 1, providing a momentum tailwind for the next 2-3 weeks.

### Implementation Plan
- **Data Source:** Pull consensus estimates vs actual EPS from `yfinance` or a premium AlphaVantage endpoint.
- **Integration:** Modify `EarningsCalendar`. Instead of a total blackout, if `Actual EPS > Consensus EPS * 1.10` (10% beat), lift the blackout and apply a `+0.2` score multiplier to any MA crossover `BUY` signal.
- **Risk Considerations:** Earnings data via free APIs is notoriously dirty. A data-feed error could trigger an unearned multiplier. Must implement a sanity check clipping extreme outliers (e.g., ignoring beats > 500%).

---

## Feature 5: Multi-Timeframe Confirmation

### Description
Noise reduction. A daily moving average crossover is much more robust if the 1-hour chart is also trending upwards.

### Implementation Plan
- **Logic:** $\text{BUY} \iff (\text{Daily MA5} > \text{Daily MA15}) \land (\text{Hourly MA20} > \text{Hourly MA50})$.
- **Implementation with Alpaca:** Modify `PostgresOhlcvFetcher` to fetch `1Hour` aggregates for the final 5 trading days. 
- **Code Changes:** Update `momentum.py` to accept an optional `intraday_df` parameter.
- **Expected Improvement:** Expected to dramatically cut down whipsaw entries taken right before the close of a rapidly reversing market. Should increase overall Win Rate by ~5%, at the cost of a slightly delayed entry price.
