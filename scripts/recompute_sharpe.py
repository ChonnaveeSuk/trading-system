#!/usr/bin/env python3
# trading-system/scripts/recompute_sharpe.py
#
# Recompute annualized Sharpe ratio from daily_pnl and patch the most
# recent gate_progress row.  Migration 008 reseeded gate_progress with
# sharpe_ratio=NULL because Sharpe can't be expressed cleanly in SQL;
# this script fills it in.
#
# Equity curve is reconstructed from realized_pnl since the daily_pnl
# rows written by migration 008 have ending_value=NULL (no equity
# snapshot was available at backfill time).  Returns are computed as
# realized_pnl[i] / equity[i-1] against a starting equity of 100000.
#
# Usage:
#   python3 scripts/recompute_sharpe.py             # compute + write
#   python3 scripts/recompute_sharpe.py --dry-run   # compute + print only

from __future__ import annotations

import argparse
import logging
import os
import sys
from decimal import Decimal
from typing import Optional

import psycopg2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("recompute_sharpe")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _db import database_url as _database_url  # noqa: E402
from gate_progress import calc_sharpe, PAPER_RUN_START  # noqa: E402

STARTING_EQUITY = 100_000.0

# Local gate thresholds — per spec for this script (NOT the gate_progress
# module defaults, which use 1.0 / 30 trades).
SHARPE_PASS = 1.5
TRADES_INSUFFICIENT = 30


def build_equity_curve(realized_per_day: list[float]) -> list[float]:
    """Rolling equity starting at STARTING_EQUITY, compounded by realized."""
    eq = STARTING_EQUITY
    out = [eq]
    for r in realized_per_day:
        eq += r
        out.append(eq)
    return out


def daily_returns_from_equity(equity: list[float]) -> list[float]:
    """(equity[i] - equity[i-1]) / equity[i-1] for i >= 1; skip non-positive denominators."""
    return [
        (equity[i] - equity[i - 1]) / equity[i - 1]
        for i in range(1, len(equity))
        if equity[i - 1] > 0
    ]


def classify_gate(sharpe: Optional[float], trade_count: int) -> str:
    if trade_count < TRADES_INSUFFICIENT:
        return "INSUFFICIENT"
    if sharpe is None:
        return "INSUFFICIENT"
    return "PASS" if sharpe >= SHARPE_PASS else "FAIL"


def fetch_realized(conn) -> list[float]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT realized_pnl
            FROM daily_pnl
            WHERE trading_date >= %s
            ORDER BY trading_date
            """,
            (PAPER_RUN_START,),
        )
        return [float(r[0]) for r in cur.fetchall()]


def fetch_latest_gate(conn) -> Optional[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT computed_at, trade_count, sharpe_ratio, gate_sharpe
            FROM gate_progress
            ORDER BY computed_at DESC
            LIMIT 1
            """
        )
        return cur.fetchone()


def update_latest_gate(
    conn, computed_at, sharpe: Optional[float], gate_sharpe: str
) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE gate_progress
                SET sharpe_ratio = %s, gate_sharpe = %s
                WHERE computed_at = %s
                """,
                (
                    None if sharpe is None else round(sharpe, 4),
                    gate_sharpe,
                    computed_at,
                ),
            )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recompute Sharpe and patch the latest gate_progress row."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print but do not write.",
    )
    args = parser.parse_args(argv)

    dsn = _database_url()
    conn = psycopg2.connect(dsn)
    try:
        realized = fetch_realized(conn)
        latest = fetch_latest_gate(conn)
        if latest is None:
            logger.error("gate_progress is empty — nothing to update.")
            return 1

        computed_at, trade_count, old_sharpe, old_gate_sharpe = latest

        equity = build_equity_curve(realized)
        returns = daily_returns_from_equity(equity)
        sharpe = calc_sharpe(returns)
        gate_sharpe = classify_gate(sharpe, trade_count)

        print("── Recompute Sharpe ─────────────────────────────────")
        print(f"  daily_pnl rows           : {len(realized)}")
        print(f"  daily returns (n)        : {len(returns)}")
        print(f"  trade_count (gate row)   : {trade_count}")
        print(f"  Sharpe (before)          : {old_sharpe}")
        print(f"  gate_sharpe (before)     : {old_gate_sharpe}")
        print(f"  Sharpe (after)           : {sharpe}")
        print(f"  gate_sharpe (after)      : {gate_sharpe}")
        print(f"  target gate_progress row : {computed_at}")

        if args.dry_run:
            logger.info("--dry-run set; skipping UPDATE.")
            return 0

        update_latest_gate(conn, computed_at, sharpe, gate_sharpe)
        logger.info(
            "gate_progress updated: sharpe=%s gate_sharpe=%s",
            "NULL" if sharpe is None else round(sharpe, 4),
            gate_sharpe,
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        logger.error("recompute_sharpe failed: %s", e)
        sys.exit(1)
