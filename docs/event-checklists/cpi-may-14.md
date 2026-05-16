# Event Checklist: CPI May 14, 2026

## Overview
Consumer Price Index (CPI) data release. High-impact macro event. The system is designed to minimize exposure to fundamental "coin-flip" volatility.

## Pre-Event Check (Morning of May 14)
- [ ] **Morning Report:** Verify "BLACKOUT: CPI" is visible in the signal log features.
- [ ] **Regime:** Confirm SPY remains in BULL. A shift to NEUTRAL/BEAR before CPI would be a significant warning sign.
- [ ] **VIX State:** Check if VIXY is spiking. If VIX state = CAUTION/PANIC, the system is already defensive.
- [ ] **Positions:** Review META status. Ensure stop loss is active in the database/Alpaca.

## Strategy Behavior During Blackout (May 13–14)
*   **New BUYs:** 100% blocked by `EconomicCalendar`.
*   **Existing Positions:** Held. The strategy does **not** close positions just because of a blackout; it only stops *new* risk.
*   **SELL Signals:** Fully active. If the market reacts negatively to the print, the system will exit META via MA cross or Hard Stop.

## Scenario Analysis
### 1. CPI "Hotter" than Expected (Bullish for USD, Bearish for Tech)
*   **Market Reaction:** SPY/QQQ likely gap down.
*   **Strategy Action:** META may hit the -5% Hard Stop ($573.74). The system will liquidate immediately.
*   **Trader Action:** Do NOT cancel the stop loss. Let the system take the loss and move to cash.

### 2. CPI "Cooler" than Expected (Bearish for USD, Bullish for Tech)
*   **Market Reaction:** SPY/QQQ rally.
*   **Strategy Action:** META remains long. No new BUYs will fire until May 15 (Post-blackout).
*   **Trader Action:** Monitor RSI. If META surges, the RSI 70 exit may trigger on May 15.

## Action Items for Trader (Human)
1.  **Monitor the Cloud Run Logs:** Ensure the `quantai-daily-runner` executes successfully at 22:00 UTC.
2.  **Verify Alpaca Connection:** Run `python3 scripts/test_alpaca_connection.py --skip-order`.
3.  **Check Telegram:** Stay alert for 🛑 Stop Loss Triggered notifications.

## What NOT to do
*   **DO NOT** manually BUY more shares during the blackout.
*   **DO NOT** remove the stop loss to "give the trade more room" through the volatility.
*   **DO NOT** disable the `calendar_filter` in `MomentumConfig`.
