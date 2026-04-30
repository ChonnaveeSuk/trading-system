#!/usr/bin/env python3
"""Compare earnings-blackout filter ON vs OFF on the production universe.

For each of the 9 single-stock names in the 16-symbol universe, runs the
backtester twice — once with `earnings_filter=True`, once `False` — keeping
every other gate (regime, VIX, macro calendar) at its production default.
Reports per-symbol Sharpe / MaxDD / trades blocked, and an aggregate "trades
saved vs trades lost" tally to quantify the filter's impact.

ETFs (SMH/QQQ/XLK/SPY/IWM/TLT/BND) and crypto (BTC-USD) have no earnings,
so they are excluded — the filter is a no-op for them.

Usage:
  python3 scripts/earnings_filter_backtest_compare.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategy"))

from src.data.fetcher import PostgresOhlcvFetcher
from src.signals.momentum import MomentumStrategy, MomentumConfig
from src.backtester.engine import BacktestEngine
from src.backtester import BacktestConfig

# 9 single-stock names tracked by EarningsCalendar
EARNINGS_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "META",
    "TSLA", "AMD", "AVGO",
]


def _run(engine, fetcher, symbol, earnings_filter, regime_df, vix_df):
    df = fetcher.fetch(symbol, days=700)
    if df.empty:
        return None
    cfg = MomentumConfig(
        fast_period=5, slow_period=15, vol_period=10, bb_period=0,
        earnings_filter=earnings_filter,
    )
    return engine.run(symbol, df, MomentumStrategy(cfg),
                      regime_df=regime_df, vix_df=vix_df)


def main() -> None:
    engine = BacktestEngine(BacktestConfig(commission_per_share=0.005, slippage_bps=0.5))

    with PostgresOhlcvFetcher() as fetcher:
        spy_df = fetcher.fetch("SPY", days=700)
        vix_df = fetcher.fetch("VIXY", days=700)
        if spy_df.empty or vix_df.empty:
            print("FATAL: SPY/VIXY missing — run scripts/seed_alpaca.py first")
            sys.exit(1)

        rows = []
        for sym in EARNINGS_SYMBOLS:
            res_off = _run(engine, fetcher, sym, earnings_filter=False,
                           regime_df=spy_df, vix_df=vix_df)
            res_on = _run(engine, fetcher, sym, earnings_filter=True,
                          regime_df=spy_df, vix_df=vix_df)
            if res_off is None or res_on is None:
                continue
            rows.append({
                "symbol": sym,
                "off": (res_off.sharpe_ratio, res_off.max_drawdown,
                        res_off.num_trades, res_off.total_return),
                "on":  (res_on.sharpe_ratio, res_on.max_drawdown,
                        res_on.num_trades, res_on.total_return),
            })

        # ── Per-symbol table ─────────────────────────────────────────────────
        print(f"\n{'Symbol':<8} {'Sharpe(off→on)':>20} "
              f"{'MaxDD(off→on)':>20} {'Trades(off→on)':>20} "
              f"{'Return(off→on)':>22} {'Blocked':>8}")
        print("─" * 105)

        # Track which symbols saw an improvement (saved trades) vs regression.
        saved = lost = 0
        for r in rows:
            so, do, no_, ro = r["off"]
            sn, dn, nn, rn = r["on"]
            blocked = max(0, no_ - nn)
            ret_delta = rn - ro
            if blocked > 0 and ret_delta > 0:
                saved += blocked
            elif blocked > 0 and ret_delta < 0:
                lost += blocked
            print(f"{r['symbol']:<8} "
                  f"{so:>8.2f} → {sn:>7.2f}     "
                  f"{do*100:>7.2f}% → {dn*100:>6.2f}%     "
                  f"{no_:>7d} → {nn:>7d}     "
                  f"{ro*100:>7.2f}% → {rn*100:>6.2f}%     "
                  f"{blocked:>5d}")

        # ── Aggregate ────────────────────────────────────────────────────────
        def _mean(xs): return (sum(xs) / len(xs)) if xs else 0.0
        sharpes_off = [r["off"][0] for r in rows if r["off"][2] > 0]
        sharpes_on = [r["on"][0]  for r in rows if r["on"][2] > 0]
        dd_off = [r["off"][1] for r in rows]
        dd_on = [r["on"][1] for r in rows]
        trades_off = sum(r["off"][2] for r in rows)
        trades_on = sum(r["on"][2] for r in rows)
        ret_off = sum(r["off"][3] for r in rows)
        ret_on = sum(r["on"][3] for r in rows)

        print("\n" + "═" * 105)
        print("AGGREGATE — Earnings filter OFF vs ON  (9 single-stock names)")
        print("═" * 105)
        print(f"  Mean Sharpe (trading windows):  {_mean(sharpes_off):>6.2f}  →  {_mean(sharpes_on):>6.2f}")
        print(f"  Max MaxDD across symbols:       {max(dd_off, default=0)*100:>6.2f}% →  {max(dd_on, default=0)*100:>6.2f}%")
        print(f"  Mean MaxDD across symbols:      {_mean(dd_off)*100:>6.2f}% →  {_mean(dd_on)*100:>6.2f}%")
        print(f"  Total trades:                   {trades_off:>6d}  →  {trades_on:>6d}  "
              f"(blocked: {trades_off - trades_on})")
        print(f"  Sum total_return:               {ret_off*100:>6.2f}% →  {ret_on*100:>6.2f}%")
        print(f"  Trades saved (block + better return):  {saved}")
        print(f"  Trades lost  (block + worse return):   {lost}")


if __name__ == "__main__":
    main()
