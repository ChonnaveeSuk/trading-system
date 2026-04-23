# trading-system/scripts/tests/test_update_daily_pnl_equity_delta.py
#
# Regression tests for the 2026-04-23 "BUY-notional-as-loss" bug.
#
# The old realized-P&L formula (cash-flow CASE in update_daily_pnl.py:75-85)
# treated every BUY fill as a realized loss, producing
#   total_pnl = -(sum of BUY notional + commission)
# for open positions, which cascaded into a false 50.55% drawdown and a
# tripped gate.  The fix: use Alpaca's account equity delta
#   today_pnl = equity - last_equity
# which matches the Alpaca UI exactly and is always correct even when
# positions are still open.
#
# All tests offline — HTTP and DB mocked.

from __future__ import annotations

import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import update_daily_pnl as upd  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_account_response(equity: str, last_equity: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock(return_value=None)
    resp.json = MagicMock(return_value={
        "equity": equity,
        "last_equity": last_equity,
        "status": "ACTIVE",
    })
    return resp


# ── 1. Happy path: fetch_today_pnl_from_alpaca returns delta ──────────────────

def test_alpaca_pnl_returns_equity_delta():
    """equity=$99,457, last_equity=$100,000 → today_pnl = -$543."""
    session = MagicMock()
    session.get.return_value = _mock_account_response("99457.01", "100000.00")

    today_pnl, equity = upd._fetch_today_pnl_from_alpaca(
        session, "https://paper-api.alpaca.markets/v2",
    )

    assert today_pnl == Decimal("-542.99"), (
        f"expected -542.99, got {today_pnl}"
    )
    assert equity == Decimal("99457.01")


def test_alpaca_pnl_positive_day():
    """equity > last_equity → positive today_pnl."""
    session = MagicMock()
    session.get.return_value = _mock_account_response("101250.50", "100000.00")

    today_pnl, equity = upd._fetch_today_pnl_from_alpaca(
        session, "https://paper-api.alpaca.markets/v2",
    )

    assert today_pnl == Decimal("1250.50")
    assert equity == Decimal("101250.50")


# ── 2. Regression: BUY-only day does NOT produce a -notional loss ─────────────

def test_buy_only_day_does_not_trigger_false_loss():
    """The 2026-04-23 scenario: 10 BUYs, 0 SELLs, equity slightly down.

    Old code: total_pnl = -$49,234 (cash-flow CASE).
    New code: today_pnl = equity - last_equity = ~-$543 (real Alpaca delta).
    """
    session = MagicMock()
    # 10 opens × ~$5k each moved us from $97,402 → $96,859 on a small down day
    session.get.return_value = _mock_account_response("96858.99", "97401.99")

    today_pnl, equity = upd._fetch_today_pnl_from_alpaca(
        session, "https://paper-api.alpaca.markets/v2",
    )

    assert today_pnl == Decimal("-543.00")
    # Critical: today_pnl is NOT anywhere near -$49k (the old bug)
    assert today_pnl > Decimal("-5000"), (
        f"fix failed — today_pnl={today_pnl} smells like the old BUY-notional bug"
    )


# ── 3. Alpaca API failure is non-fatal (returns None, None) ───────────────────

def test_alpaca_pnl_nonfatal_on_api_failure(caplog):
    """Network error during equity fetch → (None, None) + warning logged.

    Caller should treat None as 'skip this update' rather than overwrite
    daily_pnl with corrupt zeros."""
    import logging
    import requests

    session = MagicMock()
    session.get.side_effect = requests.ConnectionError("alpaca down")

    with caplog.at_level(logging.WARNING):
        today_pnl, equity = upd._fetch_today_pnl_from_alpaca(
            session, "https://paper-api.alpaca.markets/v2",
        )

    assert today_pnl is None
    assert equity is None
    assert any("alpaca" in r.message.lower() or "equity" in r.message.lower()
               for r in caplog.records), (
        f"expected warning about alpaca/equity failure, got: "
        f"{[r.message for r in caplog.records]}"
    )


# ── 4. Missing last_equity field → None (avoid corrupt math) ──────────────────

def test_alpaca_pnl_handles_missing_last_equity():
    """Defensive: if Alpaca response lacks last_equity, return None rather
    than calculating a bogus delta against 0."""
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock(return_value=None)
    resp.json = MagicMock(return_value={"equity": "99000.00"})  # no last_equity
    session.get.return_value = resp

    today_pnl, equity = upd._fetch_today_pnl_from_alpaca(
        session, "https://paper-api.alpaca.markets/v2",
    )

    assert today_pnl is None
    assert equity is None
