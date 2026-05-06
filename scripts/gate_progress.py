#!/usr/bin/env python3
# trading-system/scripts/gate_progress.py
#
# Computes the 90-day paper-trading gate metrics from Cloud SQL and
# (optionally) appends a row to the gate_progress audit table.
#
# Single source of truth for the Day-90 decision (Sharpe / MaxDD /
# profit factor / trade count).  Designed to be safe to run repeatedly
# and from cron — every invocation inserts a new gate_progress row
# keyed by computed_at, so the table doubles as a time-series of how
# the gate evolved over the run.
#
# Usage:
#   python3 scripts/gate_progress.py             # compute, print, write
#   python3 scripts/gate_progress.py --no-write  # compute + print only
#
# Pure calculation functions (calc_sharpe, calc_max_drawdown,
# calc_profit_factor, evaluate_gate) take plain numerics and have no
# DB / argparse coupling — they are exercised directly by
# scripts/tests/test_gate_progress.py.

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional, Sequence

import psycopg2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("gate_progress")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _db import database_url as _database_url  # noqa: E402

# ── Constants ────────────────────────────────────────────────────────────────

PAPER_RUN_START = date(2026, 4, 7)
PAPER_RUN_DAYS = 90

# Gate thresholds (advisor recommendation 2026-05-06: profit factor
# replaces win-rate, since trend-following systems naturally have
# 40–50% win rates with positive expectancy via fat winners).
GATE_SHARPE_MIN = 1.0
GATE_MAXDD_MAX = 0.15            # 15% as a fraction
GATE_PROFIT_FACTOR_MIN = 1.5
GATE_TRADES_MIN = 30
GATE_SHARPE_MIN_TRADES = 20      # below this → INSUFFICIENT (Sharpe noise)

TRADING_DAYS_PER_YEAR = 252

# Synthetic test fills to exclude from the gate trade count.
# 2026-04-30 cluster = test_alpaca_connection.py round-trips, not
# strategy signals.  Hardcoded per advisor note 2026-05-06.
_SYNTHETIC_TEST_DATE = date(2026, 4, 30)
_SYNTHETIC_TEST_SYMBOLS = ("EUR-USD", "BTC-USD", "AAPL")


# ── Pure calculation primitives (DB-free; covered by unit tests) ─────────────

def calc_sharpe(
    daily_returns: Sequence[float],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> Optional[float]:
    """Annualized Sharpe ratio from a sequence of daily returns.

    Uses sample standard deviation (ddof=1) per industry convention.
    Returns None when there are fewer than two observations or when
    std == 0 (no variance → Sharpe is undefined, not infinite).
    """
    n = len(daily_returns)
    if n < 2:
        return None
    mean = sum(daily_returns) / n
    var = sum((r - mean) ** 2 for r in daily_returns) / (n - 1)
    std = math.sqrt(var)
    if std == 0:
        return None
    return (mean / std) * math.sqrt(periods_per_year)


def calc_max_drawdown(equity_curve: Sequence[float]) -> float:
    """Maximum peak-to-trough drawdown as a positive fraction.

    Empty / single-point curves return 0.  A monotonically rising
    curve also returns 0.  Result is in [0, 1] — multiply by 100
    for percentage display.
    """
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def calc_profit_factor(
    realized_per_period: Sequence[float],
) -> Optional[float]:
    """Sum of positive periods divided by absolute sum of negative periods.

    Returns None when there are no negative periods (denominator
    undefined → caller treats as INSUFFICIENT until a loss appears).
    Returns 0.0 when there are losses but no wins.
    """
    gains = sum(r for r in realized_per_period if r > 0)
    losses = sum(r for r in realized_per_period if r < 0)
    if losses == 0:
        return None
    return float(gains) / abs(float(losses))


@dataclass
class GateMetrics:
    """All computed metrics for a single gate_progress evaluation."""
    trade_count: int
    win_count: int
    loss_count: int
    sharpe_ratio: Optional[float]
    max_drawdown: float
    profit_factor: Optional[float]
    days_elapsed: int
    days_remaining: int
    gate_sharpe: str
    gate_maxdd: str
    gate_profit_factor: str
    gate_trades: str
    overall_gate: str


def evaluate_gate(
    trade_count: int,
    sharpe: Optional[float],
    max_drawdown: float,
    profit_factor: Optional[float],
) -> dict[str, str]:
    """Evaluate each gate criterion → PASS / FAIL / INSUFFICIENT.

    Combines them into an `overall_gate` of PASS / FAIL / PENDING:
      - any FAIL  → FAIL
      - else any INSUFFICIENT → PENDING
      - else                  → PASS
    """
    if trade_count < GATE_SHARPE_MIN_TRADES or sharpe is None:
        gate_sharpe = "INSUFFICIENT"
    else:
        gate_sharpe = "PASS" if sharpe > GATE_SHARPE_MIN else "FAIL"

    gate_maxdd = "PASS" if max_drawdown < GATE_MAXDD_MAX else "FAIL"

    if profit_factor is None:
        gate_profit_factor = "INSUFFICIENT"
    else:
        gate_profit_factor = (
            "PASS" if profit_factor > GATE_PROFIT_FACTOR_MIN else "FAIL"
        )

    gate_trades = "PASS" if trade_count >= GATE_TRADES_MIN else "FAIL"

    statuses = (gate_sharpe, gate_maxdd, gate_profit_factor, gate_trades)
    if any(s == "FAIL" for s in statuses):
        overall = "FAIL"
    elif any(s == "INSUFFICIENT" for s in statuses):
        overall = "PENDING"
    else:
        overall = "PASS"

    return {
        "gate_sharpe": gate_sharpe,
        "gate_maxdd": gate_maxdd,
        "gate_profit_factor": gate_profit_factor,
        "gate_trades": gate_trades,
        "overall_gate": overall,
    }


# ── DB I/O ───────────────────────────────────────────────────────────────────

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS gate_progress (
    computed_at        TIMESTAMPTZ PRIMARY KEY DEFAULT now(),
    trade_count        INT          NOT NULL,
    sharpe_ratio       NUMERIC(10,4),
    max_drawdown       NUMERIC(10,4) NOT NULL,
    profit_factor      NUMERIC(10,4),
    days_remaining     INT          NOT NULL,
    gate_sharpe        VARCHAR(20)  NOT NULL,
    gate_maxdd         VARCHAR(20)  NOT NULL,
    gate_profit_factor VARCHAR(20)  NOT NULL,
    gate_trades        VARCHAR(20)  NOT NULL,
    overall_gate       VARCHAR(20)  NOT NULL
)
"""

# Synthetic-fill exclusion clause — appended to any orders/fills query.
# Inlined as static SQL because the values are hardcoded constants
# (no untrusted input → no parameterization risk).
_SYNTHETIC_FILTER_SQL = (
    "NOT (symbol IN ('EUR-USD', 'BTC-USD', 'AAPL') "
    "AND created_at::date = DATE '2026-04-30')"
)


def _fetch_trade_count(cur) -> int:
    """FILLED orders since paper-run start, excluding synthetic test fills."""
    cur.execute(
        f"""
        SELECT COUNT(*)
        FROM orders
        WHERE status = 'FILLED'
          AND created_at >= %s
          AND {_SYNTHETIC_FILTER_SQL}
        """,
        (PAPER_RUN_START,),
    )
    return int(cur.fetchone()[0])


def _fetch_daily_pnl(cur) -> list[tuple[date, Decimal, Decimal]]:
    """All daily_pnl rows since paper-run start, ending_value not null."""
    cur.execute(
        """
        SELECT trading_date, ending_value, realized_pnl
        FROM daily_pnl
        WHERE trading_date >= %s
          AND ending_value IS NOT NULL
          AND ending_value > 0
        ORDER BY trading_date
        """,
        (PAPER_RUN_START,),
    )
    return [
        (r[0], Decimal(str(r[1])), Decimal(str(r[2])))
        for r in cur.fetchall()
    ]


def compute_metrics(conn) -> GateMetrics:
    """Pull from Cloud SQL and assemble a GateMetrics record."""
    with conn.cursor() as cur:
        trade_count = _fetch_trade_count(cur)
        rows = _fetch_daily_pnl(cur)

    equity = [float(r[1]) for r in rows]
    realized = [float(r[2]) for r in rows]

    # daily_return[i] = (equity[i] - equity[i-1]) / equity[i-1]
    daily_returns = [
        (equity[i] - equity[i - 1]) / equity[i - 1]
        for i in range(1, len(equity))
        if equity[i - 1] > 0
    ]

    sharpe = calc_sharpe(daily_returns)
    max_dd = calc_max_drawdown(equity)
    profit_factor = calc_profit_factor(realized)

    # Win/loss approximation: count days with realized_pnl > 0 vs < 0.
    # Coarse — a single day can contain multiple trades — but the only
    # signal available without per-trade P&L attribution.
    win_count = sum(1 for r in realized if r > 0)
    loss_count = sum(1 for r in realized if r < 0)

    today = date.today()
    days_elapsed = (today - PAPER_RUN_START).days
    days_remaining = max(0, PAPER_RUN_DAYS - days_elapsed)

    gates = evaluate_gate(trade_count, sharpe, max_dd, profit_factor)

    return GateMetrics(
        trade_count=trade_count,
        win_count=win_count,
        loss_count=loss_count,
        sharpe_ratio=sharpe,
        max_drawdown=max_dd,
        profit_factor=profit_factor,
        days_elapsed=days_elapsed,
        days_remaining=days_remaining,
        **gates,
    )


def write_metrics(conn, m: GateMetrics) -> None:
    """Append one row to gate_progress (creates the table on first run)."""
    with conn:
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE_SQL)
            cur.execute(
                """
                INSERT INTO gate_progress (
                    trade_count, sharpe_ratio, max_drawdown, profit_factor,
                    days_remaining,
                    gate_sharpe, gate_maxdd, gate_profit_factor,
                    gate_trades, overall_gate
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    m.trade_count,
                    None if m.sharpe_ratio is None else round(m.sharpe_ratio, 4),
                    round(m.max_drawdown, 4),
                    None if m.profit_factor is None else round(m.profit_factor, 4),
                    m.days_remaining,
                    m.gate_sharpe,
                    m.gate_maxdd,
                    m.gate_profit_factor,
                    m.gate_trades,
                    m.overall_gate,
                ),
            )


# ── Reporting ────────────────────────────────────────────────────────────────

def _fmt_optional(value: Optional[float], spec: str = ".4f") -> str:
    return "n/a" if value is None else format(value, spec)


def render_report(m: GateMetrics) -> str:
    """Human-readable gate report."""
    lines = [
        "═══════════════════════════════════════════════════════",
        " QuantAI 90-Day Paper Trading Gate Progress",
        "═══════════════════════════════════════════════════════",
        f" Run window:     {PAPER_RUN_START} → +{PAPER_RUN_DAYS} days",
        f" Day:            {m.days_elapsed} / {PAPER_RUN_DAYS}"
        f"   ({m.days_remaining} remaining)",
        "",
        " ── Metrics ─────────────────────────────────────────────",
        f"  Trade count       : {m.trade_count}"
        f"   (wins: {m.win_count} days, losses: {m.loss_count} days)",
        f"  Sharpe ratio      : {_fmt_optional(m.sharpe_ratio, '.4f')}"
        f"   (annualized, ddof=1)",
        f"  Max drawdown      : {m.max_drawdown * 100:.2f}%",
        f"  Profit factor     : {_fmt_optional(m.profit_factor, '.4f')}",
        "",
        " ── Gates ────────────────────────────────────────────────",
        f"  Sharpe        > {GATE_SHARPE_MIN:.1f}"
        f"           : {m.gate_sharpe}",
        f"  Max drawdown  < {GATE_MAXDD_MAX * 100:.0f}%"
        f"           : {m.gate_maxdd}",
        f"  Profit factor > {GATE_PROFIT_FACTOR_MIN:.1f}"
        f"           : {m.gate_profit_factor}",
        f"  Trade count   ≥ {GATE_TRADES_MIN}"
        f"            : {m.gate_trades}",
        "",
        f"  OVERALL GATE                : {m.overall_gate}",
        "═══════════════════════════════════════════════════════",
    ]
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute and (optionally) persist the 90-day gate metrics.",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Print the gate report but skip the gate_progress INSERT.",
    )
    args = parser.parse_args(argv)

    dsn = _database_url()
    conn = psycopg2.connect(dsn)
    try:
        m = compute_metrics(conn)
        print(render_report(m))
        if args.no_write:
            logger.info("--no-write set; skipping DB insert.")
        else:
            write_metrics(conn, m)
            logger.info("gate_progress row written (overall=%s).", m.overall_gate)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        logger.error("gate_progress failed: %s", e)
        sys.exit(1)
