# META Position Risk Analysis
**Position:** 8 shares @ $603.94 (Entry: 2026-05-06)
**Current Status:** -$37 (-0.76%)

## 1. Stop Loss Thresholds
*   **Hard Stop Loss:** -5% (Standard for `big_tech` sector in `alpaca_direct.py`).
    *   Trigger Price: **$573.74**
    *   Action: Automatic market SELL via `check_and_trigger_stops()`.
*   **Hard Stop Warn:** -3%.
    *   Warn Price: **$585.82**
    *   Action: Log WARN and firing of Telegram alert.
*   **ATR-Based Stop:** 1x ATR below entry (set at time of signal).
    *   Estimated ATR at entry: ~$12.00.
    *   Strategy Stop: **~$591.94**.

## 2. Strategy SELL Triggers
The `MomentumStrategy` will fire a SELL signal if any of the following occur:
*   **RSI Overbought:** RSI(7) > 70. Current RSI is consolidating near 50. A price surge to ~$630+ would likely trigger this.
*   **Bearish MA Crossover:** Fast MA (5) crosses below Slow MA (15).
    *   *Note:* This SELL may be suppressed by the `trend_ride_exit_gate` if the wider trend (MA20 > MA50) remains bullish.
*   **Regime Shift:** While BEAR regime blocks BUYs, it does not force SELLs. However, a BEAR regime is often accompanied by the price dropping below the MA crossover point.

## 3. Risk Assessment: Should we be worried?
**No.** A loss of -0.76% is well within the normal daily "noise" for a high-beta tech name.
*   The ATR-based sizing (2.0x) ensures that this position only risks ~1% of total portfolio equity if the stop is hit.
*   The current consolidation is healthy after the recent BULL regime breakout.
*   Support is strong at the $580-$590 level (Slow MA cluster).

## 4. CPI May 14 Impact
*   **Blackout:** The `EconomicCalendar` will trigger a blackout on **May 13 and May 14**.
*   **BUY behavior:** All new BUY signals for META (if we were to add more) or other tickers will be blocked.
*   **SELL behavior:** SELL signals are **NOT** blocked. If CPI causes a sharp drop that triggers the stop loss or a bearish cross, the system will exit the position to preserve capital.
*   **Holding:** If the price remains stable, the position will be held through the event. We are not "gambling" on the print; we are letting the existing momentum play out.

## 5. Risk/Reward Assessment
*   **Reward:** Target is RSI 70 exit, likely near $635-$645 (+5% to +7%).
*   **Risk:** Hard stop at $573.74 (-5%).
*   **Ratio:** ~1:1. This is a standard "Trend Ride" entry. The conviction remains high as long as the SPY remains in BULL regime.
