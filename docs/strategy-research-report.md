# QuantAI: Comprehensive Strategy Research Report

## Current Strategy Analysis

The QuantAI Momentum Strategy v1 (`MomentumStrategy`) is a daily, long-only algorithmic strategy that combines dual moving average crossovers with strict mean-reversion and volatility filters.

### Exact Signal Generation Logic & Math Formulas

The primary signal engine evaluates four distinct criteria, executing in a strict priority hierarchy (RSI > Trend Ride > BB > MA Crossover).

1. **Dual MA Crossover (Trend Following):**
   - $\text{Fast MA}_t = \frac{1}{5} \sum_{i=0}^{4} P_{t-i}$
   - $\text{Slow MA}_t = \frac{1}{15} \sum_{i=0}^{14} P_{t-i}$
   - **BUY Trigger:** $(\text{Fast MA}_{t-1} \le \text{Slow MA}_{t-1}) \land (\text{Fast MA}_t > \text{Slow MA}_t)$
   - **Filters:** 
     - Noise: $\frac{|\text{Fast MA}_t - \text{Slow MA}_t|}{\text{Slow MA}_t} \ge 0.0005$ (5 bps spread)
     - Price Momentum: $P_t > P_{t-5}$
     - Volume: $V_t \ge \frac{1}{10} \sum_{i=0}^{9} V_{t-i}$

2. **RSI Mean-Reversion:**
   - Evaluates a 7-period Wilder Smoothed RSI.
   - **BUY Trigger:** $\text{RSI}_t < 30.0$ (oversold, standalone signal bypassing MA requirement).
   - **SELL Trigger:** $\text{RSI}_t > 70.0$ (overbought, preempts trend exits).

3. **Trend Ride Signals:**
   - Detects pullbacks within deeply established uptrends.
   - **Trigger:** Fast MA > Slow MA for $\ge 10$ consecutive days $\land$ ($30.0 < \text{RSI}_t < 45.0$).

4. **Bollinger Band Signals:**
   - **BUY Trigger:** $P_t < \text{BB}_{lower} = \text{SMA}_{20} - 2\sigma$ (while still in a macro MA uptrend).

### Parameter Sensitivity Analysis

The strategy's reliance on `fast_period=5` and `slow_period=15` makes it highly sensitive to parameter selection:
- **`fast_period=3`:** Makes the system hypersensitive. Crossovers occur rapidly on minor 1-2 day bounces, generating excessive false positives (whipsaws) and significantly higher transaction costs. The noise filter (5 bps) struggles to suppress the false signals.
- **`fast_period=7`:** Introduces lagging entries. By the time a 7-day MA crosses a 15-day MA, the initial explosive move of the momentum burst is already over. The risk/reward ratio worsens as the entry price is closer to the eventual exhaustion point of the short-term swing.
- **Why 5/15?** A 5-day MA perfectly encapsulates a single trading week, filtering out daily noise while reacting fast enough to catch Friday/Monday continuations. The 15-day (3-week) slow MA provides enough baseline stability to define a structural micro-trend.

### Why the Walk-Forward Sharpe of 3.50 is Suspicious

A Sharpe ratio of 3.50 over a 663-day period (mid-2024 to early 2026) is astronomically high for a traditional momentum equity strategy, bordering on statistically impossible without severe upward bias or structural data snooping. In typical quantitative finance contexts, a live daily Sharpe ratio above 1.5 is considered exceptional, and anything consistently above 2.0 suggests market-making operations, statistical arbitrage with extreme leverage, or an inherent flaw in the backtesting methodology. In the case of the QuantAI momentum strategy, this 3.50 Sharpe ratio is almost certainly an artifact of structural beta exposure and macro correlation rather than pure mathematical alpha.

During the in-sample and out-of-sample periods tested, the US equity market—specifically the technology sector—experienced an unprecedented, monolithic bull run driven by the rapid commercialization of generative AI. The revised 16-symbol universe is heavily concentrated in this exact sector (`big_tech`, `tech_etf`, `growth`), encompassing massive winners like NVIDIA, Meta, and Broadcom. Because the strategy is strictly long-only and applies a dual-MA crossover entry, it essentially acts as a leveraged beta proxy. When the entire universe moves upwards in a highly correlated manner, a walk-forward backtest will generate continuous winning trades with minimal drawdowns. The backtest does not measure the strategy's ability to extract alpha from idiosyncratic price movements; it merely measures its ability to stay long during one of the steepest tech rallies in financial history.

Furthermore, the Sharpe calculation methodology itself introduces an upward bias. The backtest computes the Sharpe ratio from the aggregate equity curve of trading days. By omitting periods where the portfolio was entirely in cash (e.g., during the brief corrections where the market regime filter suppressed all `BUY` signals), the denominator—the standard deviation of returns—is artificially suppressed. Cash has zero volatility. Excluding cash days concentrates the return stream into only the periods of active capital deployment, which happened to coincide precisely with the steepest upward slopes of the macro trend. When volatility is artificially excluded from the denominator, the Sharpe ratio explodes upwards.

Finally, walk-forward optimization, while designed to prevent overfitting, cannot protect against regime over-fitting when the entire 663-day dataset represents a single, unbroken macro regime. The model parameters (fast=5, slow=15, RSI=30/70) were selected because they perfectly capitalized on the specific frequency of the 2024-2025 AI boom's pullbacks. The strategy essentially learned to "buy the dip" in an environment where every single dip was immediately followed by a new all-time high. The model has never been truly stress-tested in a prolonged stagflationary or secular bear market environment. 

This is exactly why the 90-day paper trading gate is the most critical component of the QuantAI architecture. Live, out-of-sample paper trading forces the strategy to navigate the market without the benefit of hindsight. A Sharpe of 3.50 is an illusion of the backtest; the true expected Sharpe in a normalized or sideways market regime is likely closer to a sustainable 0.8 to 1.2.

### Academic Context: Momentum Literature

- **Jegadeesh & Titman (1993):** Found that buying past winners (top decile) and selling past losers (bottom decile) over 3-12 month horizons generated significant positive returns. QuantAI adapts this by using absolute momentum (MA crossovers) rather than cross-sectional relative ranking, operating on a compressed timeframe (1-3 weeks).
- **Why it works (Behavioral):** Momentum exploits investor underreaction to new information (slow anchoring) followed by overreaction (herding/FOMO).
- **Why it fails:** Crowding effects cause sharp momentum reversals (crashes). When the regime changes suddenly (e.g., 2026-04-28 precious metals crash), trend-following MAs lag, trapping the system in losing positions.

---

## Alternative Strategies to Consider After Day 90

If the Momentum v1 strategy fails the paper gate or reaches capacity, the following alternatives should be evaluated for Phase 6.

### 1. Cross-Sectional Momentum
- **Concept:** Rank all symbols weekly by 30-day return. Buy top 3, Short bottom 3.
- **Academic Basis:** Asness, Moskowitz, and Pedersen (2013) "Value and Momentum Everywhere".
- **Pros/Cons:** Pro: Market neutral, immune to regime crashes. Con: Requires margin/shorting, high turnover.
- **Implementation Complexity:** 6/10
- **Expected Sharpe:** 1.0 - 1.5
- **Capital:** $10,000+ (Margin required for shorting)
- **Role:** Replace current strategy.

### 2. Pure Mean Reversion (Statistical)
- **Concept:** Trade extreme deviations (RSI < 20, BB < 3σ) with very tight intraday take-profits.
- **Academic Basis:** Lo & MacKinlay (1990) short-term contrarian profits.
- **Pros/Cons:** Pro: High win rate. Con: Left-tail risk (catching falling knives).
- **Implementation Complexity:** 4/10
- **Expected Sharpe:** 0.8 - 1.2
- **Capital:** $2,000+
- **Role:** Complement (trades when momentum is flat).

### 3. Long-Term Trend Following (CTA Model)
- **Concept:** Breakout of 50-day highs, exit on 20-day lows.
- **Academic Basis:** Hurst, Ooi, Pedersen (2017) A Century of Evidence on Trend-Following.
- **Pros/Cons:** Pro: Captures massive outliers. Con: Win rate often < 40%, severe psychological drawdowns.
- **Implementation Complexity:** 2/10
- **Expected Sharpe:** 0.7 - 0.9
- **Capital:** $5,000+
- **Role:** Replace current strategy.

### 4. Statistical Arbitrage (Pairs Trading)
- **Concept:** Identify cointegrated tech pairs (e.g., AMD vs NVDA). Short the overperformer, long the underperformer when spread > 2σ.
- **Academic Basis:** Gatev et al. (2006) Pairs Trading.
- **Pros/Cons:** Pro: Pure alpha, market neutral. Con: Spread computation requires tick-level/minute data, execution slippage kills edge.
- **Implementation Complexity:** 9/10
- **Expected Sharpe:** 1.5 - 2.5
- **Capital:** $25,000+ (High execution costs)
- **Role:** Replace current strategy.

### 5. Volatility Targeting
- **Concept:** Size positions so the portfolio's daily standard deviation is constant (e.g., 10% annualized).
- **Academic Basis:** Harvey et al. (2018) The Impact of Volatility Targeting.
- **Pros/Cons:** Pro: Smooths the equity curve, cuts tails. Con: Limits upside during explosive low-vol bull runs.
- **Implementation Complexity:** 5/10
- **Expected Sharpe:** Boosts base strategy Sharpe by ~0.2
- **Capital:** $5,000+
- **Role:** Complement (layer on top of current strategy).

---

## Risk Framework Analysis (The 8 Protection Layers)

| Layer | Math / Logic | Edge Cases | Failure Mode | Test Coverage |
| :--- | :--- | :--- | :--- | :--- |
| **1. Market Regime** | $\text{SPY}_t < \text{MA}_{200} \times 0.98 \rightarrow$ Block BUY | SPY data goes stale > 7 days. | Fails open (defaults to BULL) if data missing, risking buying into bear markets. | `test_regime_filter.py` |
| **2. VIX Filter** | $\text{VIXY MA}_{20} > 60 \rightarrow$ PANIC (Block BUY) | VIXY undergoes a reverse split. | Fails open (CALM) if data missing. Absolute bounds degrade if VIXY structural prices shift. | `test_vix_filter.py` |
| **3. Econ Calendar** | $\Delta(\text{Event Date}, \text{Today}) \le 1 \rightarrow$ Block BUY | Events spanning weekends. | A new Fed emergency meeting isn't hardcoded in the static calendar. | `test_economic_calendar.py` |
| **4. Earnings Block** | $\Delta(\text{Earnings Date}, \text{Today}) \le 1 \rightarrow$ Block BUY | Earnings moved unannounced. | API rate limits on the earnings fetcher bypass the block. | `test_earnings_calendar.py` |
| **5. Long-term Trend** | $P_t < \text{SMA}_{trend\_period} \rightarrow$ Block BUY | IPOs or stocks with $<200$ bars. | Defaults to True if insufficient data, exposing system to falling knives. | `test_phase2.py` |
| **6. Hard Stop-Loss** | $P_{current} < \text{AvgCost} \times (1 - 0.05) \rightarrow$ MARKET SELL | API fails during partial liquidation. | System remains exposed. A fallback JSON log exists but doesn't exit the trade. | `test_alpaca_direct.py` |
| **7. Sector Limit** | $\sum(\text{Pos}_{sector}) \ge 3 \lor \sum(\text{Notional}_{sector}) \ge 0.3E \rightarrow$ Block BUY | Unmapped symbol defaults to "other". | "Other" sector becomes a dumping ground, recreating concentration risk. | `test_alpaca_direct.py` |
| **8. Position Limit** | $\text{Open Positions} \ge 10 \rightarrow$ Block BUY | Alpaca sync drift (ghost positions). | System thinks it's full and refuses valid trades. | `test_alpaca_direct.py` |
