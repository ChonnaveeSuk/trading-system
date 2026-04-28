#!/usr/bin/env python3
"""Compare economic-calendar filter ON vs OFF on the production universe.

For each symbol runs single-pass BacktestEngine twice — once with
`calendar_filter=True`, once `False` — keeping every other gate (regime, VIX)
identical to current production defaults.  Reports aggregate Sharpe / MaxDD /
trades blocked, plus an April-2026 specific check (the FOMC Jan-29 + CPI Apr-14
window covers the precious-metals incident period).

Usage:
  python3 scripts/calendar_filter_backtest_compare.py
"""

from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategy"))

from src.data.fetcher import PostgresOhlcvFetcher
from src.signals.momentum import MomentumStrategy, MomentumConfig
from src.backtester.engine import BacktestEngine
from src.backtester import BacktestConfig
from src.filters.economic_calendar import EconomicCalendar

PRECIOUS_METALS = {
    "GLD", "IAU", "SLV", "GDX", "GDXJ", "RING", "PAAS", "SILJ", "WPM",
    "HL", "CDE", "NEM", "AEM", "AGI", "GOLD", "KGC",
}

SYMBOLS = [
    "BTC-USD",
    "GLD", "IAU", "SLV",
    "GDX", "GDXJ", "RING", "PAAS", "SILJ", "WPM", "HL", "CDE",
    "NEM", "AEM", "AGI", "GOLD", "KGC",
    "URA", "URNM", "DBC", "SCCO", "MP",
    "SPY", "QQQ", "IWM", "XLK", "AAPL", "TLT", "EEM",
]


def _run(engine, fetcher, symbol, calendar_filter, regime_df, vix_df):
    df = fetcher.fetch(symbol, days=700)
    if df.empty:
        return None
    cfg = MomentumConfig(
        fast_period=5, slow_period=15, vol_period=10, bb_period=0,
        calendar_filter=calendar_filter,
    )
    return engine.run(symbol, df, MomentumStrategy(cfg),
                      regime_df=regime_df, vix_df=vix_df)


def main() -> None:
    engine = BacktestEngine(BacktestConfig(commission_per_share=0.005, slippage_bps=0.5))
    cal = EconomicCalendar(blackout_days_before=1)

    with PostgresOhlcvFetcher() as fetcher:
        spy_df = fetcher.fetch("SPY", days=700)
        vix_df = fetcher.fetch("VIXY", days=700)
        if vix_df.empty or spy_df.empty:
            print("FATAL: SPY/VIXY missing — run scripts/seed_alpaca.py first")
            sys.exit(1)

        # Cap regime+VIX warm-up at the same dates so both runs see the same input
        rows = []
        for sym in SYMBOLS:
            res_off = _run(engine, fetcher, sym, calendar_filter=False,
                           regime_df=spy_df, vix_df=vix_df)
            res_on  = _run(engine, fetcher, sym, calendar_filter=True,
                           regime_df=spy_df, vix_df=vix_df)
            if res_off is None or res_on is None:
                continue
            rows.append({
                "symbol": sym,
                "is_pm": sym in PRECIOUS_METALS,
                "off": (res_off.sharpe_ratio, res_off.max_drawdown,
                        res_off.num_trades, res_off.total_return),
                "on":  (res_on.sharpe_ratio,  res_on.max_drawdown,
                        res_on.num_trades,  res_on.total_return),
            })

        # ── Per-symbol table ─────────────────────────────────────────────────
        print(f"\n{'Symbol':<10} {'Sharpe(off→on)':>20} "
              f"{'MaxDD(off→on)':>20} {'Trades(off→on)':>20} {'Blocked':>8}")
        print("─" * 85)
        for r in rows:
            so, do, no_, _ = r["off"]
            sn, dn, nn, _ = r["on"]
            tag = " *PM" if r["is_pm"] else ""
            blocked = max(0, no_ - nn)
            print(f"{r['symbol']:<10}{tag:<3} "
                  f"{so:>8.2f} → {sn:>7.2f}    "
                  f"{do*100:>7.2f}% → {dn*100:>6.2f}%    "
                  f"{no_:>7d} → {nn:>7d}    "
                  f"{blocked:>5d}")

        # ── Aggregate ────────────────────────────────────────────────────────
        def _mean(xs): return (sum(xs) / len(xs)) if xs else 0.0
        sharpes_off = [r["off"][0] for r in rows if r["off"][2] > 0]
        sharpes_on  = [r["on"][0]  for r in rows if r["on"][2]  > 0]
        dd_off = [r["off"][1] for r in rows]
        dd_on  = [r["on"][1]  for r in rows]
        trades_off = sum(r["off"][2] for r in rows)
        trades_on  = sum(r["on"][2]  for r in rows)
        ret_off = sum(r["off"][3] for r in rows)
        ret_on  = sum(r["on"][3]  for r in rows)

        print("\n" + "═" * 85)
        print("AGGREGATE — Calendar filter OFF vs ON")
        print("═" * 85)
        print(f"  Mean Sharpe (trading windows):  {_mean(sharpes_off):>6.2f}  →  {_mean(sharpes_on):>6.2f}")
        print(f"  Max MaxDD across symbols:       {max(dd_off, default=0)*100:>6.2f}% →  {max(dd_on, default=0)*100:>6.2f}%")
        print(f"  Mean MaxDD across symbols:      {_mean(dd_off)*100:>6.2f}% →  {_mean(dd_on)*100:>6.2f}%")
        print(f"  Total trades:                   {trades_off:>6d}  →  {trades_on:>6d}  "
              f"(blocked: {trades_off - trades_on})")
        print(f"  Sum total_return:               {ret_off*100:>6.2f}% →  {ret_on*100:>6.2f}%")

        # ── April-2026 PM-specific check ─────────────────────────────────────
        # Did any precious-metals BUYs in April 2026 land on calendar-blackout
        # bars?  If so, the filter would have prevented those entries.
        from src.signals.momentum import MomentumStrategy as _MS, MomentumConfig as _MC
        cfg_off = _MC(fast_period=5, slow_period=15, vol_period=10, bb_period=0,
                      calendar_filter=False, regime_filter=True, vix_filter=True)
        april = date(2026, 4, 1); may = date(2026, 5, 1)
        pm_blackout = pm_clear = 0
        for sym in PRECIOUS_METALS:
            df = fetcher.fetch(sym, days=700)
            if df.empty:
                continue
            sigs = _MS(cfg_off).generate_signals_series(sym, df, regime_df=spy_df, vix_df=vix_df)
            april_buys = sigs[(sigs.index.date >= april) & (sigs.index.date < may)
                              & (sigs["direction"] == "BUY")]
            for ts in april_buys.index:
                if cal.is_blackout_day(ts.date()):
                    pm_blackout += 1
                else:
                    pm_clear += 1
        print("\nApril-2026 PM-incident check — did calendar block PM BUYs?")
        print(f"  PM BUYs on blackout bars (would be blocked): {pm_blackout}")
        print(f"  PM BUYs on clear bars (calendar would not help): {pm_clear}")


if __name__ == "__main__":
    main()
