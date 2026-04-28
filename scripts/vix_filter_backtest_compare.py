#!/usr/bin/env python3
"""Compare VIX filter ON vs OFF on the production symbol set.

Runs single-pass BacktestEngine on each tradable symbol with vix_filter=False
then again with vix_filter=True, aggregating Sharpe/MaxDD/trade-count delta.
Also reports the precious-metals subset specifically (April 2026 was the
incident the filter is meant to mitigate).

Usage:
  python3 scripts/vix_filter_backtest_compare.py [--symbols SYM1 SYM2 …]
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategy"))

from src.data.fetcher import PostgresOhlcvFetcher
from src.signals.momentum import MomentumStrategy, MomentumConfig
from src.backtester.engine import BacktestEngine
from src.backtester import BacktestConfig

PRECIOUS_METALS = {
    "GLD", "IAU", "SLV", "GDX", "GDXJ", "RING", "PAAS", "SILJ", "WPM",
    "HL", "CDE", "NEM", "AEM", "AGI", "GOLD", "KGC",
}

DEFAULT_SYMBOLS = [
    "BTC-USD",
    "GLD", "IAU", "SLV",
    "GDX", "GDXJ", "RING", "PAAS", "SILJ", "WPM", "HL", "CDE",
    "NEM", "AEM", "AGI", "GOLD", "KGC",
    "URA", "URNM", "DBC", "SCCO", "MP",
    "SPY", "QQQ", "IWM", "XLK", "AAPL", "TLT", "EEM",
]


def _run_one(engine, fetcher, symbol, vix_filter: bool, regime_df, vix_df):
    df = fetcher.fetch(symbol, days=700)
    if df.empty:
        return None
    cfg = MomentumConfig(
        fast_period=5, slow_period=15, vol_period=10, bb_period=0,
        vix_filter=vix_filter,
    )
    strat = MomentumStrategy(cfg)
    return engine.run(
        symbol, df, strat, regime_df=regime_df,
        vix_df=vix_df if vix_filter else None,
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    args = p.parse_args()

    engine = BacktestEngine(BacktestConfig(commission_per_share=0.005, slippage_bps=0.5))

    with PostgresOhlcvFetcher() as fetcher:
        spy_df = fetcher.fetch("SPY", days=700)
        vix_df = fetcher.fetch("VIXY", days=700)

        if vix_df.empty:
            print("FATAL: VIXY data missing — run scripts/seed_alpaca.py --symbols VIXY first")
            sys.exit(1)

        # Show current VIX state for context
        vstate = MomentumStrategy(MomentumConfig()).update_vix(vix_df)
        print(f"\nCurrent VIX state (VIXY MA20): {vstate}")
        print(f"Latest VIXY: {vix_df.index[-1].date()}  close=${float(vix_df['close'].iloc[-1]):.2f}\n")

        rows = []
        per_symbol_blocked = {}

        for symbol in args.symbols:
            res_off = _run_one(engine, fetcher, symbol, vix_filter=False,
                               regime_df=spy_df, vix_df=vix_df)
            res_on  = _run_one(engine, fetcher, symbol, vix_filter=True,
                               regime_df=spy_df, vix_df=vix_df)
            if res_off is None or res_on is None:
                continue

            blocked = max(0, res_off.num_trades - res_on.num_trades)
            per_symbol_blocked[symbol] = blocked
            rows.append({
                "symbol": symbol,
                "is_pm": symbol in PRECIOUS_METALS,
                "off": (res_off.sharpe_ratio, res_off.max_drawdown, res_off.num_trades, res_off.total_return),
                "on":  (res_on.sharpe_ratio,  res_on.max_drawdown,  res_on.num_trades,  res_on.total_return),
                "blocked": blocked,
            })

        # ── Per-symbol table ─────────────────────────────────────────────────
        print(f"{'Symbol':<10} {'Sharpe(off→on)':>20} {'MaxDD(off→on)':>20} {'Trades(off→on)':>20} {'Blocked':>8}")
        print("─" * 85)
        for r in rows:
            so, do, no_, _ = r["off"]
            sn, dn, nn, _ = r["on"]
            tag = " *PM" if r["is_pm"] else ""
            print(f"{r['symbol']:<10}{tag:<3} "
                  f"{so:>8.2f} → {sn:>7.2f}    "
                  f"{do*100:>7.2f}% → {dn*100:>6.2f}%    "
                  f"{no_:>7d} → {nn:>7d}    "
                  f"{r['blocked']:>5d}")

        # ── Aggregate metrics ─────────────────────────────────────────────────
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
        print("AGGREGATE — VIX filter OFF vs ON")
        print("═" * 85)
        print(f"  Mean Sharpe (trading windows):  {_mean(sharpes_off):>6.2f}  →  {_mean(sharpes_on):>6.2f}")
        print(f"  Max MaxDD across symbols:       {max(dd_off, default=0)*100:>6.2f}% →  {max(dd_on, default=0)*100:>6.2f}%")
        print(f"  Mean MaxDD across symbols:      {_mean(dd_off)*100:>6.2f}% →  {_mean(dd_on)*100:>6.2f}%")
        print(f"  Total trades:                   {trades_off:>6d}  →  {trades_on:>6d}  "
              f"(blocked: {trades_off - trades_on})")
        print(f"  Sum total_return:               {ret_off*100:>6.2f}% →  {ret_on*100:>6.2f}%")

        # ── Precious-metals subset (April 2026 incident focus) ───────────────
        pm_rows = [r for r in rows if r["is_pm"]]
        if pm_rows:
            pm_blocked = sum(r["blocked"] for r in pm_rows)
            pm_sharpe_off = _mean([r["off"][0] for r in pm_rows if r["off"][2] > 0])
            pm_sharpe_on  = _mean([r["on"][0]  for r in pm_rows if r["on"][2]  > 0])
            pm_dd_off = _mean([r["off"][1] for r in pm_rows]) * 100
            pm_dd_on  = _mean([r["on"][1]  for r in pm_rows]) * 100
            print("\nPRECIOUS-METALS subset (Apr 2026 incident-relevant):")
            print(f"  Symbols:           {len(pm_rows)}")
            print(f"  Trades blocked:    {pm_blocked}")
            print(f"  Mean Sharpe:       {pm_sharpe_off:>5.2f}  →  {pm_sharpe_on:>5.2f}")
            print(f"  Mean MaxDD:        {pm_dd_off:>5.2f}% →  {pm_dd_on:>5.2f}%")

        # ── Did filter prevent April precious-metals entries? ────────────────
        # Check whether any PM trade in the test window happened on a PANIC bar.
        print("\nApril-2026 specific check — PM BUYs on PANIC bars vs CALM bars:")
        from src.signals.momentum import MomentumStrategy as _MS, MomentumConfig as _MC
        cfg_off = _MC(fast_period=5, slow_period=15, vol_period=10, bb_period=0,
                      regime_filter=True, vix_filter=False)
        from datetime import date as _date
        april = _date(2026, 4, 1)
        pm_panic_buys = pm_calm_buys = pm_caution_buys = 0
        for sym in PRECIOUS_METALS:
            df = fetcher.fetch(sym, days=700)
            if df.empty:
                continue
            sigs = _MS(cfg_off).generate_signals_series(sym, df, regime_df=spy_df, vix_df=vix_df)
            april_buys = sigs[(sigs.index.date >= april) & (sigs["direction"] == "BUY")]
            for _, row in april_buys.iterrows():
                state = row.get("vix_state", "")
                if state == "PANIC": pm_panic_buys += 1
                elif state == "CAUTION": pm_caution_buys += 1
                elif state == "CALM": pm_calm_buys += 1
        print(f"  Apr-2026 PM BUYs on PANIC bars:   {pm_panic_buys}  (would be blocked by filter)")
        print(f"  Apr-2026 PM BUYs on CAUTION bars: {pm_caution_buys}  (would be size-halved)")
        print(f"  Apr-2026 PM BUYs on CALM bars:    {pm_calm_buys}    (unchanged)")


if __name__ == "__main__":
    main()
