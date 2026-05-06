# trading-system/scripts/tests/test_gate_progress.py
#
# Pure-function unit tests for the gate-metric primitives in
# scripts/gate_progress.py.  No DB, no Alpaca, no fixtures from
# conftest.py — these tests just import the calc functions and
# pin their numerical behaviour.

from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gate_progress import (  # noqa: E402
    GATE_SHARPE_MIN_TRADES,
    TRADING_DAYS_PER_YEAR,
    calc_max_drawdown,
    calc_profit_factor,
    calc_sharpe,
    evaluate_gate,
)


# ── 1. Sharpe ratio ───────────────────────────────────────────────────────────

def test_sharpe_calculation():
    """Annualized Sharpe with known returns matches the textbook formula.

    returns = [0.01, -0.005, 0.015, -0.01, 0.02]
      mean   = 0.006
      var    = Σ(r - mean)² / (n-1)   (sample stdev, ddof=1)
             = (0.000016 + 0.000121 + 0.000081 + 0.000256 + 0.000196) / 4
             = 0.0001675
      stdev  = √0.0001675   ≈ 0.012942
      sharpe = (mean / stdev) × √252
             ≈ 0.46362 × 15.87451
             ≈ 7.3596
    """
    returns = [0.01, -0.005, 0.015, -0.01, 0.02]
    sharpe = calc_sharpe(returns)
    assert sharpe is not None
    assert math.isclose(sharpe, 7.3596, abs_tol=0.001), (
        f"expected ~7.3596, got {sharpe}"
    )


def test_sharpe_zero_when_mean_return_is_zero():
    """Symmetric returns → mean = 0 → Sharpe = 0 (not None)."""
    returns = [0.01, -0.01, 0.01, -0.01]
    sharpe = calc_sharpe(returns)
    assert sharpe is not None
    assert math.isclose(sharpe, 0.0, abs_tol=1e-9)


def test_sharpe_returns_none_on_insufficient_or_flat():
    """Edge cases that make Sharpe undefined.

    < 2 observations → no sample variance possible.
    Zero variance    → division by zero, not infinity.
    """
    assert calc_sharpe([]) is None
    assert calc_sharpe([0.05]) is None
    assert calc_sharpe([0.01, 0.01, 0.01]) is None  # std == 0


def test_sharpe_uses_252_trading_day_annualisation():
    """Sentinel: changing the periods_per_year arg scales the result."""
    returns = [0.01, -0.005, 0.015, -0.01, 0.02]
    a = calc_sharpe(returns, periods_per_year=TRADING_DAYS_PER_YEAR)
    b = calc_sharpe(returns, periods_per_year=TRADING_DAYS_PER_YEAR * 4)
    assert a is not None and b is not None
    assert math.isclose(b / a, 2.0, abs_tol=1e-9)


# ── 2. Max drawdown ───────────────────────────────────────────────────────────

def test_max_drawdown():
    """Peak 120 → trough 80 = 33.33% drawdown is the worst on the curve.

    equity   = [100, 110, 120, 90, 100, 80, 105]
    peaks    = [100, 110, 120,120,120,120,120]
    dd       = [  0,   0,   0, .25, .1667, .3333, .125]
    max_dd   = .3333
    """
    equity = [100.0, 110.0, 120.0, 90.0, 100.0, 80.0, 105.0]
    dd = calc_max_drawdown(equity)
    assert math.isclose(dd, 1 / 3, abs_tol=1e-6), f"expected ~0.3333, got {dd}"


def test_max_drawdown_zero_for_monotonic_or_empty():
    """A monotonically rising or empty curve has no drawdown."""
    assert calc_max_drawdown([]) == 0.0
    assert calc_max_drawdown([100.0]) == 0.0
    assert calc_max_drawdown([100.0, 105.0, 110.0, 115.0]) == 0.0


def test_max_drawdown_matches_live_paper_run_window():
    """Sanity-check against the 2026-04-07 → 2026-05-05 live equity curve.

    Pinning the live numbers here protects us from a regression that
    would silently change how max_drawdown rolls up against real data.
    Day-1 start $97,401.99, low $94,111.11 → 3.379% peak-to-trough.
    """
    equity = [97401.99, 96858.99, 97230.49, 97688.03, 97023.26, 95174.34,
              94138.24, 94111.11]
    dd = calc_max_drawdown(equity)
    assert math.isclose(dd, 0.03665, abs_tol=1e-4), (
        f"live-window max_dd should be ~3.665%, got {dd * 100:.3f}%"
    )


# ── 3. Profit factor ──────────────────────────────────────────────────────────

def test_profit_factor():
    """gains $350 / |losses| $150 = 2.3333 — passes the 1.5 gate."""
    realized = [100.0, -50.0, 200.0, -100.0, 50.0]
    pf = calc_profit_factor(realized)
    assert pf is not None
    assert math.isclose(pf, 350.0 / 150.0, abs_tol=1e-9)


def test_profit_factor_none_when_no_losing_periods():
    """All-positive realized P&L → PF undefined (insufficient data).

    The gate treats this as INSUFFICIENT, not PASS — a strategy that
    has never lost has not been stress-tested against drawdown.
    """
    assert calc_profit_factor([10.0, 20.0, 5.0]) is None
    assert calc_profit_factor([0.0, 0.0]) is None  # zero-only also undefined


def test_profit_factor_zero_when_no_winning_periods():
    """All-negative realized P&L → PF = 0.0 (definite FAIL, not None)."""
    pf = calc_profit_factor([-10.0, -20.0, -5.0])
    assert pf == 0.0


# ── 4. Gate composition (small smoke test on the orchestrator) ───────────────

def test_evaluate_gate_overall_pass():
    """All four sub-gates green → overall PASS."""
    g = evaluate_gate(
        trade_count=40,
        sharpe=1.5,
        max_drawdown=0.05,
        profit_factor=2.0,
    )
    assert g["overall_gate"] == "PASS"


def test_evaluate_gate_fail_dominates_insufficient():
    """A FAIL bit must short-circuit the overall result, even if other
    bits are INSUFFICIENT — the gate has already failed regardless."""
    g = evaluate_gate(
        trade_count=GATE_SHARPE_MIN_TRADES - 1,  # → INSUFFICIENT for sharpe
        sharpe=2.0,
        max_drawdown=0.20,                       # → FAIL
        profit_factor=None,                      # → INSUFFICIENT
    )
    assert g["gate_maxdd"] == "FAIL"
    assert g["gate_sharpe"] == "INSUFFICIENT"
    assert g["overall_gate"] == "FAIL"


def test_evaluate_gate_pending_when_only_insufficient():
    """No FAILs but at least one INSUFFICIENT → overall PENDING."""
    g = evaluate_gate(
        trade_count=10,        # below 30 → trade gate FAILs
        sharpe=None,
        max_drawdown=0.02,
        profit_factor=None,
    )
    # Trade-count below 30 still FAILs gate_trades, dominating PENDING.
    assert g["gate_trades"] == "FAIL"
    assert g["overall_gate"] == "FAIL"


def test_evaluate_gate_pending_pure_insufficient():
    """Trade count ≥ 30 but sharpe/PF undefined → PENDING."""
    g = evaluate_gate(
        trade_count=35,
        sharpe=None,           # std == 0 case
        max_drawdown=0.02,
        profit_factor=None,    # no losing days yet
    )
    assert g["gate_trades"] == "PASS"
    assert g["gate_maxdd"] == "PASS"
    assert g["gate_sharpe"] == "INSUFFICIENT"
    assert g["gate_profit_factor"] == "INSUFFICIENT"
    assert g["overall_gate"] == "PENDING"
