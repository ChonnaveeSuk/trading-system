#!/usr/bin/env python3
# strategy/simulations/param_sweep.py
#
# Algorithm sensitivity sweep — Task 3 of Optimization Audit.
#
# Tests:
#   MA:  fast/slow/vol = 5/15/10 (baseline) vs 5/20/10
#   RSI: oversold/overbought = 30/70 (baseline) vs 25/75 vs 35/65
#   ATR: trailing_stop_atr_mult = 1.5 (skip, covered in trailing stop session)
#
# Decision rule: keep change if avg_daily_thb AND sharpe improve vs baseline.
# Each config runs on all 31 symbols, 700-day fetch (same as production sim).
#
# Usage:
#   cd /home/chonsuk/trading-system/strategy
#   python3 simulations/param_sweep.py

from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import date
from typing import NamedTuple

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backtester.engine import BacktestEngine
from src.data.fetcher import PostgresOhlcvFetcher
from src.signals.momentum import MomentumConfig, MomentumStrategy

# ── Constants (match run_1m_thb.py) ───────────────────────────────────────────
STARTING_CAPITAL_USD = 28_000.0
THB_PER_USD = 35.7
POSITION_PCT = 0.05

SYMBOLS = [
    "BTC-USD", "BNB-USD",
    "GLD", "IAU", "SLV",
    "GDX", "GDXJ", "RING", "PAAS", "SILJ", "WPM", "HL", "CDE",
    "NEM", "AEM", "AGI", "GOLD", "KGC",
    "URA", "URNM", "DBC", "SCCO", "MP",
    "SPY", "QQQ", "IWM", "XLK", "AAPL", "TLT", "EEM", "GBP-USD",
]


class SweepResult(NamedTuple):
    label: str
    avg_daily_thb: float
    total_return_pct: float
    max_dd_pct: float
    sharpe: float
    n_trades: int


def simulate_portfolio(
    symbol_dfs: dict,
    strategy: MomentumStrategy,
    label: str,
) -> SweepResult:
    """Run simulation on all 31 symbols and compute aggregate metrics."""
    all_daily_usd: dict[date, float] = defaultdict(float)
    total_trades = 0

    for symbol, df in symbol_dfs.items():
        if df is None or len(df) < strategy.config.slow_period + 5:
            continue
        try:
            signals = strategy.generate_signals_series(symbol, df)
        except ValueError:
            continue

        price_slice = df.loc[df.index.isin(signals.index)].copy()
        result = BacktestEngine._simulate_on_slice(
            price_slice, signals, STARTING_CAPITAL_USD, POSITION_PCT
        )
        dates = [ts.date() for ts in price_slice.index]
        equity = result["equity_curve"]
        for i, d in enumerate(dates):
            all_daily_usd[d] += equity[i + 1] - equity[i]
        total_trades += len([t for t in result["trades"] if "pnl" in t])

    sorted_dates = sorted(all_daily_usd)
    combined_daily = [all_daily_usd[d] for d in sorted_dates]

    if not combined_daily:
        return SweepResult(label, 0.0, 0.0, 0.0, 0.0, 0)

    eq = np.array([STARTING_CAPITAL_USD] + [
        STARTING_CAPITAL_USD + sum(combined_daily[:i+1])
        for i in range(len(combined_daily))
    ])
    run_max = np.maximum.accumulate(eq)
    max_dd = float(abs(((eq - run_max) / run_max).min()))

    returns = np.array(combined_daily) / STARTING_CAPITAL_USD
    sharpe = (
        float(returns.mean() / returns.std() * np.sqrt(252))
        if returns.std() > 0 else 0.0
    )
    total_return = (eq[-1] - eq[0]) / eq[0]
    avg_daily_thb = float(np.mean(combined_daily)) * THB_PER_USD

    return SweepResult(
        label=label,
        avg_daily_thb=round(avg_daily_thb, 2),
        total_return_pct=round(total_return * 100, 2),
        max_dd_pct=round(max_dd * 100, 2),
        sharpe=round(sharpe, 3),
        n_trades=total_trades,
    )


def main() -> None:
    print("Loading OHLCV data for 31 symbols (700 days)...")
    symbol_dfs: dict = {}
    with PostgresOhlcvFetcher() as fetcher:
        for symbol in SYMBOLS:
            df = fetcher.fetch(symbol, days=700)
            symbol_dfs[symbol] = df if not df.empty else None
            print(f"  {symbol}: {len(df)} bars")

    print("\nRunning parameter sweep...\n")

    configs = [
        # Baseline
        ("5/15/10  RSI30/70 [BASELINE]",
         MomentumConfig(fast_period=5, slow_period=15, vol_period=10, bb_period=0,
                        rsi_oversold=30.0, rsi_overbought=70.0)),
        # MA variation
        ("5/20/10  RSI30/70",
         MomentumConfig(fast_period=5, slow_period=20, vol_period=10, bb_period=0,
                        rsi_oversold=30.0, rsi_overbought=70.0)),
        # RSI tighter (stricter oversold gate)
        ("5/15/10  RSI25/75",
         MomentumConfig(fast_period=5, slow_period=15, vol_period=10, bb_period=0,
                        rsi_oversold=25.0, rsi_overbought=75.0)),
        # RSI wider (more frequent signals)
        ("5/15/10  RSI35/65",
         MomentumConfig(fast_period=5, slow_period=15, vol_period=10, bb_period=0,
                        rsi_oversold=35.0, rsi_overbought=65.0)),
        # Combined: wider RSI + slower MA
        ("5/20/10  RSI35/65",
         MomentumConfig(fast_period=5, slow_period=20, vol_period=10, bb_period=0,
                        rsi_oversold=35.0, rsi_overbought=65.0)),
    ]

    results: list[SweepResult] = []
    for label, cfg in configs:
        strategy = MomentumStrategy(cfg)
        r = simulate_portfolio(symbol_dfs, strategy, label)
        results.append(r)
        print(f"  {label:<30} | {r.avg_daily_thb:>8.1f} THB/day | "
              f"ret {r.total_return_pct:>6.2f}% | MaxDD {r.max_dd_pct:>5.2f}% | "
              f"Sharpe {r.sharpe:>5.3f} | trades {r.n_trades}")

    # Summary table
    baseline = results[0]
    print("\n" + "=" * 90)
    print(f"{'Config':<30} | {'Avg/day THB':>11} | {'Return':>8} | {'MaxDD':>7} | {'Sharpe':>7} | {'Trades':>7} | {'vs baseline'}")
    print("-" * 90)
    for r in results:
        delta = f"+{r.avg_daily_thb - baseline.avg_daily_thb:+.1f} THB/day" if r != baseline else "—"
        marker = " ✅" if r != baseline and r.avg_daily_thb > baseline.avg_daily_thb and r.sharpe > baseline.sharpe else (
            " ❌" if r != baseline else "")
        print(f"{r.label:<30} | {r.avg_daily_thb:>10.1f} | {r.total_return_pct:>7.2f}% | "
              f"{r.max_dd_pct:>6.2f}% | {r.sharpe:>6.3f} | {r.n_trades:>6} | {delta}{marker}")

    # Decision
    print("\nDecision rules: keep if avg_daily_thb > baseline AND sharpe > baseline")
    best = max(results[1:], key=lambda r: r.avg_daily_thb + r.sharpe * 50)
    if best.avg_daily_thb > baseline.avg_daily_thb and best.sharpe > baseline.sharpe:
        print(f"  → CANDIDATE: {best.label} (+{best.avg_daily_thb - baseline.avg_daily_thb:.1f} THB/day, "
              f"Sharpe {best.sharpe:.3f} vs {baseline.sharpe:.3f})")
        print("    Re-run walk-forward gate before committing.")
    else:
        print(f"  → KEEP BASELINE (5/15/10, RSI 30/70): no variant beats it on both metrics")


if __name__ == "__main__":
    main()
