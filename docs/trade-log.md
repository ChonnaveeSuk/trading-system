# Trade Log

A running log of all real trades with analysis.

| Date | Symbol | Side | Entry | Exit | P&L | Reason | Lesson |
|------|--------|------|-------|------|-----|--------|--------|
| 2026-04-22 | 10x PM | BUY | - | - | - | Signal cluster in precious metals sector. | Sector concentration risk was not yet hard-bounded in code. |
| 2026-04-29 | 10x PM | SELL | - | - | -$4,825 | Emergency liquidation of concentrated precious metals positions. | Unmanaged sector exposure can wipe out months of alpha in days. |
| 2026-05-06 | NVDA | BUY | $200.67 | - | - | Bullish MA crossover confirmed by price momentum and RSI filter. | High-conviction entry in a core growth name. |
| 2026-05-06 | META | BUY | $603.94 | - | - | Trend continuation (trend_ride) signal on RSI pullback. | Using established uptrends to enter late-cycle strength. |
| 2026-05-11 | NVDA | SELL | $200.67 | ~$219 | ~+$420 | RSI overbought (take profit) signal fired. | First clean exit of a profitable trade in Phase 5. |

## Analysis of Strategic Intent

### 2026-04-22: The Precious Metals Cluster
**Strategy Thinking:** Momentum signals fired across the entire gold/silver miner universe. At the time, the strategy only capped total positions (10), but had no sector-specific gates. It "saw" alpha in every miner and filled the book.
**The Lesson:** Correlation is not diversification. The strategy now has a hard sector cap: max 3 positions per sector, max 30% notional exposure.

### 2026-05-06: NVDA & META Entries
**Strategy Thinking:** After the PM cleanup, the universe was rebalanced toward Big Tech. On May 6, NVDA fired a clean MA crossover signal. META didn't cross, but it was in a strong existing uptrend and dipped into the "pullback zone" (RSI 45), triggering the `trend_ride` logic.
**The Result:** NVDA hit the RSI overbought target (70) on May 11. META is currently consolidating (-0.76%).

### 2026-05-11: NVDA Exit
**Strategy Thinking:** "Profit taking is never a mistake." The RSI overbought filter is designed to exit momentum trades before the inevitable mean-reversion. NVDA surged to ~$219, pushing RSI past 70.
**Observation:** 24 shares were bought, but only 23 were sold due to a rounding mismatch in the sizing logic for high-price stocks. This has been noted for Task 15 refinement.
