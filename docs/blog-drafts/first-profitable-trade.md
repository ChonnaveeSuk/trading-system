# First Profitable Trade: NVDA and the 8 Layers of Protection

On Day 12 of our Phase 5 paper trading run, the QuantAI system closed its first profitable position. While a single winning trade doesn't prove an "edge," the *way* it was handled proves the system's architectural integrity. 

Here is the anatomy of the NVDA trade: from signal to fill, and the 8 layers of protection that kept it safe.

## The Signal: Conviction in Growth
On May 6, 2026, the `MomentumStrategy` fired a high-conviction BUY signal for **NVDA**. 

The setup was a textbook MA crossover: the 5-day Fast MA crossed above the 15-day Slow MA. But the strategy didn't just look at the cross. It applied a series of "conviction filters":
1.  **Price Momentum:** The close was higher than it was 5 days ago, confirming the uptick wasn't just a dead-cat bounce.
2.  **RSI Filter:** RSI(7) was in a neutral-to-bullish zone. Because the trend was clean, the RSI filter applied a 1.5x multiplier to the signal score, pushing it to **0.82**.

The system bought 24 shares at **$200.67**.

## The 8 Layers of Protection
Winning trades are easy to talk about, but they only happen if the system survives the losing ones. This trade was governed by 8 distinct protection layers:

1.  **Market Regime Filter:** The system only bought because SPY was above its 200-day MA (BULL regime).
2.  **VIX Filter:** VIXY was at 27.9 (CALM state), meaning volatility wasn't high enough to warrant defensive position sizing.
3.  **Sector Gate:** The system verified that we didn't already have more than 3 positions in "Big Tech."
4.  **Hard Stop Loss:** A -5% stop was immediately placed in the database ($190.64), ensuring a single bad print couldn't cause a systemic drawdown.
5.  **ATR Sizing:** The position size (24 shares) was determined by 14-day volatility, not just a flat dollar amount.
6.  **Economic Blackout:** The trade was entered *after* FOMC passed, ensuring no immediate macro "coin-flips."
7.  **Risk Engine:** The Rust-based OMS verified the signal score (>0.55) and portfolio limits before hitting the Alpaca API.
8.  **Dedup Guard:** A 3-layer check ensured that if the Cloud Run Job retried, we wouldn't double-buy the position.

## The Exit: Knowing When to Walk Away
The hardest part of momentum trading isn't getting in; it's getting out before the trend reverses. 

On May 11, NVDA surged toward **$219**. This push drove the RSI(7) past **70**—the "overbought" threshold. The strategy's mean-reversion layer fired a SELL signal. 

"Profit taking is never a mistake," the system's logic dictates. It submitted a market SELL for the position. We realized a profit of **~$420** (roughly +9% in 5 days).

## Honest Reflection: Luck or Edge?
Is a 9% win in 5 days a sign of a superior quant model? **Probably not.** 

NVDA is a high-beta growth leader. In a BULL regime, it tends to outperform. We were "lucky" that the macro environment remained stable during our holding period. 

However, the **Edge** lies in the **Repeatability**.
- We didn't over-leverage because of the ATR sizing.
- We didn't panic-sell because the stop loss was automated.
- We didn't miss the exit because the RSI filter is emotionless.

This trade wasn't a "home run"—it was a "disciplined single." 

## The Road to Day 90
We are currently Day 13 of 90. Our Sharpe ratio is still recovering from an early sector concentration incident in April, but the NVDA trade proves the "Tech-Focus Rebalance" is working. 

As we approach CPI on May 14, the system will enter a blackout. We won't be chasing the next rally. We will be waiting for the next high-conviction signal that passes all 8 layers of protection.

Onward to the Gate.
