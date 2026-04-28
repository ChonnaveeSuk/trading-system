#!/usr/bin/env python3
"""Sweep VIX-filter threshold candidates and report Sharpe/MaxDD/blocked.

Scores each candidate by:
  - aggregate Sharpe (target: as close to OFF baseline 1.05 as possible)
  - total trades blocked across the universe
  - PANIC bars in the most-recent 60 trading days (the filter must still
    block something — a candidate with 0 PANIC bars ever is just OFF in
    disguise)

Usage:
  python3 scripts/vix_threshold_sweep.py
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategy"))

from src.data.fetcher import PostgresOhlcvFetcher
from src.signals.momentum import MomentumStrategy, MomentumConfig
from src.backtester.engine import BacktestEngine
from src.backtester import BacktestConfig

SYMBOLS = [
    "BTC-USD",
    "GLD", "IAU", "SLV",
    "GDX", "GDXJ", "RING", "PAAS", "SILJ", "WPM", "HL", "CDE",
    "NEM", "AEM", "AGI", "GOLD", "KGC",
    "URA", "URNM", "DBC", "SCCO", "MP",
    "SPY", "QQQ", "IWM", "XLK", "AAPL", "TLT", "EEM",
]


@dataclass
class Candidate:
    label: str
    cfg_kwargs: dict


def _aggregate(engine, fetcher, spy_df, vix_df, candidate: Candidate):
    sharpes = []
    total_trades = 0
    total_return = 0.0
    max_dd = 0.0
    for sym in SYMBOLS:
        df = fetcher.fetch(sym, days=700)
        if df.empty:
            continue
        cfg = MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10, bb_period=0,
            vix_filter=candidate.cfg_kwargs.get("vix_filter", True),
            **{k: v for k, v in candidate.cfg_kwargs.items() if k != "vix_filter"},
        )
        strat = MomentumStrategy(cfg)
        res = engine.run(
            sym, df, strat,
            regime_df=spy_df,
            vix_df=vix_df if cfg.vix_filter else None,
        )
        if res.num_trades > 0:
            sharpes.append(res.sharpe_ratio)
        total_trades += res.num_trades
        total_return += res.total_return
        max_dd = max(max_dd, res.max_drawdown)
    mean_sharpe = sum(sharpes) / len(sharpes) if sharpes else 0.0
    return {
        "mean_sharpe": mean_sharpe,
        "total_trades": total_trades,
        "total_return": total_return,
        "max_dd": max_dd,
    }


def _panic_bar_count(strat: MomentumStrategy, vix_df, last_n: int):
    """Generate signals on a synthetic series so we can read vix_state column.

    We just want to know how often the candidate's classifier fires PANIC
    over the trailing N bars of VIXY history.
    """
    sigs = strat.generate_signals_series(
        "VIXY", vix_df, regime_df=None, vix_df=vix_df,
    )
    if "vix_state" not in sigs.columns:
        return 0, 0
    tail = sigs.iloc[-last_n:] if len(sigs) > last_n else sigs
    panic = (tail["vix_state"] == "PANIC").sum()
    caution = (tail["vix_state"] == "CAUTION").sum()
    return int(panic), int(caution)


def main() -> None:
    candidates = [
        # Baseline: filter OFF (target Sharpe to match)
        Candidate("OFF (baseline)", {"vix_filter": False}),

        # ── Option A: absolute thresholds, raised ────────────────────────────
        Candidate("A1: abs 30/40", {"vix_mode": "absolute",
                                     "vix_caution_threshold": 30.0,
                                     "vix_panic_threshold": 40.0}),
        Candidate("A2: abs 35/50", {"vix_mode": "absolute",
                                     "vix_caution_threshold": 35.0,
                                     "vix_panic_threshold": 50.0}),
        Candidate("A3: abs 40/55", {"vix_mode": "absolute",
                                     "vix_caution_threshold": 40.0,
                                     "vix_panic_threshold": 55.0}),
        Candidate("A4: abs 45/60", {"vix_mode": "absolute",
                                     "vix_caution_threshold": 45.0,
                                     "vix_panic_threshold": 60.0}),

        # ── Option B: relative — % above 252d VIXY low ───────────────────────
        Candidate("B1: rel 0.20/0.50", {"vix_mode": "relative",
                                          "vix_caution_pct": 0.20,
                                          "vix_panic_pct": 0.50}),
        Candidate("B2: rel 0.30/0.60", {"vix_mode": "relative",
                                          "vix_caution_pct": 0.30,
                                          "vix_panic_pct": 0.60}),
        Candidate("B3: rel 0.40/0.80", {"vix_mode": "relative",
                                          "vix_caution_pct": 0.40,
                                          "vix_panic_pct": 0.80}),
        Candidate("B4: rel 0.50/1.00", {"vix_mode": "relative",
                                          "vix_caution_pct": 0.50,
                                          "vix_panic_pct": 1.00}),

        # Original spec defaults — for context
        Candidate("orig 20/30 (too tight)", {"vix_mode": "absolute",
                                              "vix_caution_threshold": 20.0,
                                              "vix_panic_threshold": 30.0}),
    ]

    engine = BacktestEngine(BacktestConfig(commission_per_share=0.005, slippage_bps=0.5))

    with PostgresOhlcvFetcher() as fetcher:
        spy_df = fetcher.fetch("SPY", days=700)
        vix_df = fetcher.fetch("VIXY", days=700)
        if vix_df.empty:
            print("FATAL: VIXY missing — run scripts/seed_alpaca.py --symbols VIXY")
            sys.exit(1)

        # Baseline OFF first to compute the target Sharpe
        baseline = None
        results = []
        for c in candidates:
            agg = _aggregate(engine, fetcher, spy_df, vix_df, c)
            if c.label.startswith("OFF"):
                baseline = agg
            # Count panic/caution bars in trailing 250 days (~1y of VIXY)
            if c.cfg_kwargs.get("vix_filter", True):
                cfg = MomentumConfig(
                    fast_period=5, slow_period=15, vol_period=10, bb_period=0,
                    **{k: v for k, v in c.cfg_kwargs.items() if k != "vix_filter"},
                )
                strat = MomentumStrategy(cfg)
                panic_n, caution_n = _panic_bar_count(strat, vix_df, 250)
            else:
                panic_n, caution_n = 0, 0
            results.append((c, agg, panic_n, caution_n))

        # ── Print table ──────────────────────────────────────────────────────
        print()
        print(f"{'Candidate':<28} {'Sharpe':>7} {'ΔSharpe':>8} "
              f"{'TotalRet':>10} {'Trades':>7} {'PanicBars':>10} {'CautionBars':>11}")
        print("─" * 95)
        target = baseline["mean_sharpe"] if baseline else 1.05
        for c, agg, panic_n, caution_n in results:
            d_sharpe = agg["mean_sharpe"] - target
            tag = "  ←OFF" if c.label.startswith("OFF") else ""
            print(f"{c.label:<28} {agg['mean_sharpe']:>7.3f} {d_sharpe:>+8.3f} "
                  f"{agg['total_return']*100:>9.2f}% {agg['total_trades']:>7d} "
                  f"{panic_n:>10d} {caution_n:>11d}{tag}")

        # ── Pick the winner ──────────────────────────────────────────────────
        # Best = candidate whose Sharpe is closest to baseline OFF AND has at
        # least 1 PANIC bar in trailing 250d (otherwise the filter is dead code).
        scored = []
        for c, agg, panic_n, caution_n in results:
            if c.label.startswith("OFF"):
                continue
            if not c.cfg_kwargs.get("vix_filter", True):
                continue
            d = abs(agg["mean_sharpe"] - target)
            # Penalize candidates with 0 PANIC bars (filter never fires)
            penalty = 0.50 if panic_n == 0 else 0.0
            scored.append((d + penalty, panic_n, c, agg))
        scored.sort(key=lambda t: (t[0], -t[1]))

        print("\nWINNER (closest Sharpe to OFF baseline + at least one PANIC bar in 250d):")
        if scored:
            score, panic_n, c, agg = scored[0]
            print(f"  → {c.label}")
            print(f"    cfg = {c.cfg_kwargs}")
            print(f"    Sharpe={agg['mean_sharpe']:.3f}  vs OFF baseline {target:.3f}  "
                  f"(Δ={agg['mean_sharpe']-target:+.3f})")
            print(f"    PANIC bars in trailing 250d: {panic_n}")
            print(f"    Total trades: {agg['total_trades']}  vs OFF "
                  f"{baseline['total_trades']}  (blocked: "
                  f"{baseline['total_trades']-agg['total_trades']})")


if __name__ == "__main__":
    main()
