# QuantAI Strategy: Momentum v1

## Strategy Overview
- **Name:** Momentum Strategy v1 (`MomentumStrategy`)
- **Type:** Long-only, daily momentum with mean-reversion pullbacks.
- **Universe:** 15 highly liquid tech-focused symbols (+1 VIX proxy).
- **Timeframe:** Daily (end-of-day signals generated at 22:00 UTC).

The strategy capitalizes on prolonged macro uptrends in specific high-beta tech equities and ETFs, while employing heavy defensive filtering to preserve capital during bear markets and flash crashes.

---

## Signal Generation

### 1. Dual MA Crossover (fast=5, slow=15)
The core trend-following engine.
- **BUY:** When the 5-day MA crosses above the 15-day MA. Requires a minimum spread of 5bps (noise filter) and positive price momentum (current close > close 5 days ago) to avoid buying "dead-cat bounces".
- **SELL:** When the 5-day MA crosses below the 15-day MA.

### 2. RSI Filter (period=7)
A mean-reversion layer designed to buy temporary dips and sell extreme tops.
- **Standalone BUY:** If RSI drops below 30 (oversold), a BUY signal is fired regardless of the MA crossover state, provided the long-term trend filter (MA200) is positive.
- **Standalone SELL:** If RSI exceeds 70 (overbought), a SELL signal is fired instantly to take profits early.
- **Score Multiplier:** If an MA cross fires while RSI is oversold, the signal's conviction score is boosted 1.5x. If RSI is overbought, the BUY score is slashed 0.3x.

### 3. Volume Confirmation
Validates MA crossovers.
- A crossover is only respected if the day's volume is $\geq$ the 10-day average volume (`vol_period=10`). Sparse-volume instruments (like FX) auto-bypass this rule but have a 4x wider noise filter applied.

### 4. Trend Ride Signals
Designed to catch a ride on an already established uptrend.
- Fires a BUY if the fast MA has been above the slow MA for $\geq 10$ consecutive days AND the RSI experiences a mild pullback ($30 < RSI < 45$).
- **Exit Gate:** Suppresses a fast/slow bearish cross SELL if the wider MA20 > MA50 trend is still strongly intact, preventing premature exits on standard market retracements.

### 5. Bollinger Band Signals
Mean-reversion entry logic.
- **BUY:** If price touches the lower 2$\sigma$ band while the 5/15 MA is still in an uptrend, it signals an extreme dip and fires a BUY. BB SELLs are disabled to avoid cutting trend profits short.

---

## 8 Protection Layers

| Layer | Function | Triggers When | Prevents | Code Location |
| :--- | :--- | :--- | :--- | :--- |
| **1. Market Regime** | Blocks all BUYs | SPY price falls 2% below its 200-day MA (`BEAR`). | Buying into prolonged macro downtrends. | `momentum.py: update_regime()` |
| **2. VIX Filter** | Blocks BUYs / Halves Qty | VIXY MA20 exceeds 60 (PANIC) or 45 (CAUTION). | Entering during extreme volatility / market panic. | `momentum.py: update_vix()` |
| **3. Econ Calendar** | Blocks BUYs | Today or tomorrow is FOMC, NFP, CPI, or GDP day. | Whipsaws from high-impact macro news. | `economic_calendar.py` |
| **4. Earnings Block** | Blocks BUYs | Specific symbol reports earnings today/tomorrow. | 10% gap-downs from bad fundamental reports. | `economic_calendar.py` |
| **5. Long-term Trend** | Blocks BUYs | Asset price is below its own long-term MA. | Catching falling knives in single-name crashes. | `momentum.py: generate_signal()`|
| **6. Hard Stop-Loss** | Sells instantly | Position hits -5% unrealized loss (or -7% for growth). | Bleeding beyond expected drawdown thresholds. | `alpaca_direct.py: check_and_trigger_stops()` |
| **7. Sector Limit** | Blocks BUYs | Sector hits 3 positions or 30% notional exposure. | Correlated multi-asset crashes (e.g. 2026-04-28). | `alpaca_direct.py: submit_signal()` |
| **8. Position Limit** | Blocks BUYs | Portfolio has 10 total open positions. | Over-leveraging the account equity. | `alpaca_direct.py: submit_signal()` |

---

## Position Sizing

- **ATR-Based Sizing:** Allocates capital based on the asset's recent volatility. `qty = (portfolio * 1% risk) / ATR(14)`. This means we buy fewer shares of highly volatile stocks and more shares of stable stocks.
- **Maximum Size:** Capped at an absolute maximum of 5% of total portfolio equity per position.
- **Hard Stop Limits:** Default stop loss is 5% unrealized P&L. For high-beta growth names (TSLA, AMD, AVGO), it is widened to 7% to account for daily noise.

---

## Backtest Results

A rigorous Walk-Forward methodology (252 days In-Sample, 63 days Out-of-Sample) was used to validate the model over 663 trading days (2024-06-07 to 2026-04-28).

- **Sharpe Ratio:** 3.50 (Note: This is extremely high and statistically suspicious. It is likely inflated because it is calculated from the *equity curve* trajectory of an all-tech universe during the massive 2024-2025 AI bull run. The 90-day paper gate acts as the ultimate truth teller).
- **Max Drawdown:** 4.31%
- **Average Daily P&L:** ~749 THB/day
- **Total Trades:** 248

---

## Known Weaknesses

1. **Long-Only Bias:** The strategy has no mechanism to short the market. In a severe multi-year bear market, it will simply sit in cash for months.
2. **Bull Market Over-fitting:** The backtest results are heavily skewed by the performance of mega-cap tech stocks (NVDA, MSFT) during the AI boom.
3. **Low Trade Frequency:** With only 1-2 trades a week across the 15-symbol universe, it takes a long time to achieve statistical significance in live trading.
4. **Parameter Sensitivity:** The 5/15 MA cross and RSI(7) are highly tuned to the 2024-2026 regime. If market frequency shifts, the strategy may suffer whipsaws.

---

## Future Improvements

1. **Cross-Sectional Momentum Ranking:** Instead of evaluating symbols in a vacuum, rank all 15 symbols by 30-day momentum and only buy the top 3.
2. **Inverse Volatility Weighting:** Size the entire portfolio inversely to the VIX, rather than just using static CAUTION/PANIC thresholds.
3. **ML Regime Detection:** Use K-Nearest Neighbors (kNN) over historical feature vectors (pgvector) to classify the exact market regime type instead of a simple MA200 rule.
4. **Multi-Timeframe Signals:** Incorporate weekly and hourly data to confirm the daily trend before entry.
