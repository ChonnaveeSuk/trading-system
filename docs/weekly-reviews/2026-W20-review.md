# Weekly Performance Review: 2026-W20
**Period:** May 4 – May 11, 2026

## Executive Summary
Week 20 marked a significant milestone: the first profitable live exit of Phase 5. The system successfully managed the transition from FOMC volatility into a BULL regime, capturing a ~$420 gain on NVDA. Total equity stands at $96,564 as we approach the mid-May macro hurdles.

## Trades Executed This Week
| Date | Symbol | Side | Price | Qty | Notional | Result |
|------|--------|------|-------|-----|----------|--------|
| 2026-05-06 | NVDA | BUY | $200.67 | 24 | $4,816 | Held |
| 2026-05-06 | META | BUY | $603.94 | 8 | $4,831 | Open (-$37) |
| 2026-05-11 | NVDA | SELL | ~$219.00 | 23 | $5,037 | **+$420 profit** |

*Note: 1 share of NVDA remains open due to a rounding discrepancy in the exit logic.*

## P&L Breakdown
*   **Realized P&L:** +$421.59 (NVDA)
*   **Unrealized P&L:** -$37.12 (META)
*   **Cumulative Phase 5 P&L:** -$3,435 (still recovering from Apr 29 PM liquidation)
*   **Current Equity:** $96,564

## Gate Progress Update (Day 13/90)
*   **Sharpe Ratio:** -3.65 (INSUFFICIENT) - Dragged down by the Day 1 liquidation. Needs consistent wins to normalize.
*   **Max Drawdown:** 0.03% (since reset) ✅
*   **Trades:** 8/30 (Tracking toward target)

## Market Context
*   **Regime:** BULL (SPY > MA200)
*   **SPY Performance:** +8.3% over the trailing 30 days.
*   **Volatility:** VIXY at 27.9 (CALM state).
*   **Macro:** FOMC (May 6) passed without systemic shock. CPI is the next major hurdle (May 14).

## Assessment
### What Worked
*   **RSI Filter Conviction:** The NVDA entry was boosted by the RSI filter (score 0.82), allowing for a high-conviction position in a leading growth name.
*   **Regime Permissiveness:** Staying in BULL regime allowed the system to ignore minor pullbacks and stay long.
*   **Sector Gate:** No more than 3 tech names were entered, keeping the book balanced.

### What Didn't
*   **Quantity Rounding:** The exit logic for NVDA rounded down to 23 shares while the entry was 24. This leaves "dust" in the portfolio which requires manual reconciliation or a logic fix to "SELL ALL" on exit signals.

## Outlook: Week 21
The primary focus is the **CPI release on May 14**. The strategy will enter a **blackout period** starting May 13.
*   **META:** Holding through the consolidation. Support is firm at $580.
*   **Blackout:** No new BUY orders will be submitted on May 13-14.
*   **Target:** Looking for a second entry in Semiconductors (AMD/AVGO) if the BULL regime holds post-CPI.
