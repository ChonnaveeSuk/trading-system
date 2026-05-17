# Postmortem: 2026-04-28 Precious Metals Concentration Incident

## Executive Summary

On April 28, 2026, the QuantAI paper trading system experienced its first major failure since the Phase 5 gate began, realizing a cumulative -$3,733 drawdown. The system's entire portfolio capacity—10 out of 10 maximum open positions—became saturated exclusively with precious metal (PM) mining stocks. When the precious metals sector suffered an unexpected, correlated, intraday slide, the momentum strategy's slow-moving average (MA) exit signals failed to trigger fast enough, leading to severe and unchecked unrealized losses.

The core business impact was a sharp 5.3% drawdown in paper equity, dragging the 90-day gate's Sharpe ratio from a healthy +1.39 to a failing -1.35 in a single trading session. To fix this, an emergency manual liquidation of the PM positions was performed. The universe was completely rebuilt, swapping the 30-symbol PM-heavy list for a 16-symbol tech-focused universe. Crucially, a hard sector concentration gate (maximum 3 positions or 30% notional exposure per sector) and an unrealized stop-loss monitor (-5%) were implemented directly into the live trading path to prevent recurrence.

## Timeline

*All times are in UTC unless otherwise specified.*

- **2026-04-07 14:00:** The system enters Phase 5, starting its 90-day paper trading gate. The trading universe contains 30 symbols, 16 of which (53%) are highly correlated precious metal assets (e.g., gold/silver miners).
- **2026-04-18 → 2026-04-22:** Precious metals enter a strong macro uptrend. The momentum strategy correctly identifies this, and BUY signals begin firing across PM symbols.
- **2026-04-22 22:00:** During the daily cron job, the remaining portfolio capacity is consumed by PM stocks. The system hits the `_MAX_OPEN_POSITIONS = 10` limit. All 10 positions are now precious metals.
- **2026-04-23 → 2026-04-27:** The precious metals sector peaks and begins to roll over. The `slow_ma` in the strategy (set to 15 days) has not yet crossed over the `fast_ma` (5 days), so no SELL signals are generated.
- **2026-04-28 13:30 (Market Open):** The precious metals sector experiences a sudden, highly correlated crash.
- **2026-04-28 20:00:** The 10-position PM cluster bleeds past -$2,600 in unrealized losses. No automated stop-loss exists to override the MA crossover logic.
- **2026-04-29 02:00:** The daily cron runs. The MA crossover finally triggers SELL signals for the PM positions, realizing a catastrophic -$3,733 cumulative loss. The equity curve plummets, and the Sharpe ratio drops to -1.35.
- **2026-04-29 10:00:** Incident is officially declared. Trading is manually frozen.
- **2026-04-29 14:00:** The legacy 30-symbol universe is retired.
- **2026-04-30 22:00:** Patches for sector limits and hard stop-losses are deployed to `alpaca_direct.py` and `momentum.py`. The new 16-symbol tech-focused universe goes live.

## Root Cause Analysis

We use the 5 Whys methodology to drill down to the fundamental systemic failure:

1. **Why did the portfolio lose -$3,733 in a single day?**
   Because 10 out of 10 open positions suffered heavy losses simultaneously when the precious metals sector crashed.
2. **Why did the system have 10 positions in the same sector?**
   Because the trading universe was fundamentally skewed (16 out of 30 symbols were PMs), and the momentum strategy indiscriminately bought them as they all trended up together.
3. **Why didn't the system block the over-concentration?**
   Because there was no sector concentration limit in the order management system (`alpaca_direct.py`). The only limit was a global maximum of 10 open positions.
4. **Why weren't the bleeding positions cut earlier during the crash?**
   Because the strategy relied purely on the lagging Dual Moving Average (5/15) crossover to generate SELL signals. There was no hard, equity-based stop loss to preempt the lagging indicators.
5. **Why wasn't the lack of a sector gate or hard stop-loss caught during Phase 3 backtesting?**
   Because backtests measure the theoretical portfolio performance over long horizons, masking the acute, multi-day, cross-sectional concentration risks that arise when heavily correlated assets form a single cluster.

## Technical Deep Dive

Prior to the incident, `alpaca_direct.py` restricted total portfolio exposure via `_MAX_OPEN_POSITIONS` (10 positions) and `_MAX_POSITION_PCT` (5% notional). However, because 53% of the universe consisted of correlated assets, the moment a macro trend favored PMs, the 10 available slots were instantly filled by PM signals.

To fix this, we introduced the `sector_for()` mapping function inside `momentum.py` as a single source of truth for the system's sector definitions.

```python
# strategy/src/signals/momentum.py
SYMBOL_TO_SECTOR: dict[str, str] = {
    "AAPL": "big_tech", "MSFT": "big_tech", "NVDA": "big_tech",
    "GOOGL": "big_tech", "META": "big_tech",
    "QQQ": "tech_etf", "XLK": "tech_etf", "SMH": "tech_etf",
    # ...
}

def sector_for(symbol: str) -> str:
    return SYMBOL_TO_SECTOR.get(symbol, "other")
```

Inside `alpaca_direct.py`, we implemented **Guard 4: Sector Concentration**. This guard tallies the current sector exposure by inspecting live Alpaca positions. If the new `BUY` signal pushes the sector's position count beyond `_MAX_SECTOR_POSITIONS` (3) or its notional value beyond `_MAX_SECTOR_PCT` (30%), the order is rejected.

```python
# strategy/src/bridge/alpaca_direct.py
new_sector = sector_for(signal.symbol)
# ... loop through existing positions ...
if sector_count >= _MAX_SECTOR_POSITIONS:
    logger.warning("REJECTED: %s already at %d/%d sector limit", new_sector, sector_count, _MAX_SECTOR_POSITIONS)
    return BridgeResponse(accepted=False, status="REJECTED", ...)
```

Furthermore, to fix the lagging MA exits, we added a hard stop-loss trigger `check_and_trigger_stops()` that fires *before* the signal generation loop. If any position breaches `_DEFAULT_STOP_LOSS_PCT` (-5%), it is immediately liquidated via a market `DELETE` request to Alpaca.

## Impact Analysis

- **Financial Impact:** Realized paper loss of -$3,733. Portfolio equity dropped by ~5.3% from its peak.
- **Gate Metric Impact:** The Rolling 30-Day Sharpe ratio crashed below the 1.0 threshold (falling to -1.35). The Max Drawdown metric spiked above the 8% warning line but stayed under the fatal 15% line (peaking at ~8.86% under the old universe stats).
- **Timeline Impact:** The strategy operated in a degraded, risk-heavy state for 7 days.
- **Trust & Morale:** As the first major incident since the 90-day paper gate began, it highlighted a severe blind spot in how cross-sectional correlation affects backtest validity.
- **Positive Impact:** This was the most valuable incident in the project's history. It forced the implementation of institutional-grade risk controls (sector gates and hard equity stops) that will permanently protect the real-money deployment.

## Fix Applied

1. **Universe Rebalance:** The 30-symbol PM-heavy universe was retired. A new 16-symbol tech-focused universe was deployed, deliberately capping the largest sector (`big_tech`) at 5 symbols (31% of the universe).
2. **Sector Concentration Limit:** Added `_MAX_SECTOR_POSITIONS = 3` and `_MAX_SECTOR_PCT = 0.30` to `alpaca_direct.py`.
3. **Hard Stop-Loss Engine:** Added `check_and_trigger_stops()` to `AlpacaDirectClient`, enforcing an unrealized loss cut-off of -5% (and -7% for high-beta "growth" stocks).
4. **Monitoring Upgrades:** The `morning_report.py` script was updated to surface a new "Sector Exposure" block and a "Stop Loss Watch" block, alerting the operator when a sector approaches its 30% cap or a position nears its -5% stop.

## Prevention Measures

To ensure this class of failure never happens again, the following layers of defense are now active:
- **Live Order Gates:** Every `BUY` is evaluated against the `sector_for()` map. Over-concentration is mathematically impossible.
- **Daily Pre-Flight Liquidation:** The stop-loss evaluator runs daily, preempting any lagging indicator exits if a position bleeds past the critical threshold.
- **Observability:** Telegram alerts are now integrated into the stop-loss trigger. If a hard stop is hit, a `CRITICAL` alert is immediately pushed to the operator.
- **Test Coverage:** Integration tests must be written to verify that the `AlpacaDirectClient` correctly rejects signals that breach the sector concentration cap.

## Lessons Learned

1. **Backtests Ignore Correlation Risks:** Walk-forward backtesting measures the asset in a vacuum. A great Sharpe ratio on 16 correlated assets is an illusion; they will all fail on the same day.
2. **Lagging Indicators Are Not Risk Controls:** Moving averages dictate entry and exit regimes, but they cannot act as capital preservation mechanisms during sudden crashes. A hard equity-based stop is non-negotiable.
3. **Sector Caps Must Be Hardcoded:** Relying on universe diversification is insufficient. The execution engine itself must count the exposure and physically block the order.
4. **Visibility Precedes Control:** Without the morning report surfacing unrealized losses on an individual symbol basis, the -$2,600 bleed went unnoticed until it was realized.
5. **Failures in Paper are Features:** Losing $3,733 in paper money bought the system an architectural upgrade that will save real capital in Phase 6. The 90-day gate functioned exactly as intended.

## Action Items Table

| Priority | Owner | Action Item | Status | Due Date |
| :--- | :--- | :--- | :--- | :--- |
| **P0** | QuantAI Eng | Replace 30-symbol PM universe with 16-symbol Tech universe. | ✅ Done | 2026-04-29 |
| **P0** | QuantAI Eng | Implement sector limit logic in `alpaca_direct.py`. | ✅ Done | 2026-04-30 |
| **P0** | QuantAI Eng | Implement hard stop loss evaluator in `alpaca_direct.py`. | ✅ Done | 2026-04-30 |
| **P1** | QuantAI Eng | Add Sector and Stop-Loss blocks to `morning_report.py`. | ✅ Done | 2026-05-02 |
| **P1** | QuantAI Eng | Write automated integration tests for the sector concentration gate. | ⏳ Pending | 2026-05-10 |

## 2026-05-17 Reconciliation Note

A manual audit on 2026-05-17 revealed that the Postgres `orders` and `fills`
tables were missing **11 SELL records** that had executed successfully on
Alpaca — the 10 PM exit SELLs from 2026-04-29 and one GLD SELL from
2026-05-01. The morning-report pipeline, the `daily_pnl` aggregator, and
the `gate_progress` audit table had all been computing metrics against an
incomplete picture for ~18 days.

**Reconciled numbers (replaces all earlier estimates in this document):**

- Realized P&L of the PM cluster on 2026-04-29: **-$3,732.67**
  (per-trade, FIFO-matched from Alpaca fills — replaces the earlier
  estimate of -$4,825)
- Profit factor across the 12 closed round-trips since paper run start: **0.117**
- Total realized P&L for the paper run: **-$3,337.67** (matches the
  Alpaca cash math after removing a separate -$2,598 pre-paper testing
  artifact that had been double-counted in `daily_pnl`)

The reconciliation was applied via `migrations/008_reconcile_apr_pm_incident.sql`,
which backfills the missing orders + fills, flags 6 PAPER-prefixed test
records as `test_trade=true`, and recomputes `daily_pnl` and
`gate_progress` from clean data.

**Lesson — broker/DB reconciliation gap:** The existing
`scripts/reconcile_alpaca_fills.py` only watches the open window; it does
not detect or repair *historical* fill gaps. The PM SELLs from the
incident itself never made it into Postgres because the live engine was
in a degraded state during the cascade, and there was no after-the-fact
"sweep" job to catch the omission. The cumulative drift between the
DB-derived ledger and the Alpaca account-state ledger was not flagged
because no automated invariant compares the two totals daily.

**Follow-up task (open):** Add a daily reconcile-or-page job that
compares `SUM(fills.qty * fills.price * sign(side))` against the Alpaca
`/v2/account` equity delta — page on any drift > $50. This is the same
class of check called out by Backlog Task 17 (equity-divergence monitor)
and should ship together.
