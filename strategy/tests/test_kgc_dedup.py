# trading-system/strategy/tests/test_kgc_dedup.py
#
# Regression tests for Task-15: KGC double-submission bug.
#
# Root cause: AlpacaDirectClient.submit_signal() checked _get_positions()
# (filled positions only) as its only dedup guard.  A pending order submitted
# in a previous Cloud Run invocation — not yet filled at market open — is
# invisible to GET /positions and passes the guard, producing a duplicate.
#
# On 2026-04-23 this caused KGC to receive two BUY orders (both "quantai-tr-*",
# 153 shares each) from what was effectively two Cloud Run invocations.  The
# duplicate was manually cancelled (Alpaca order 171538c0-...).
#
# Fix: two new guards in the BUY path of submit_signal():
#   Guard 2: session-level seen_symbols set  (within-invocation dedup)
#   Guard 3: GET /orders?status=open check   (cross-invocation dedup)
#
# Test naming convention:
#   test_*_blocks_*   → asserts the duplicate is REJECTED/SKIPPED
#   test_*_allows_*   → happy-path: legitimate trades still go through

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest
import requests

from src.signals import Direction, SignalResult
from src.bridge.alpaca_direct import AlpacaDirectClient
from src.bridge.client import BridgeResponse


# ── Helpers ───────────────────────────────────────────────────────────────────

def _kgc_buy_signal(qty: Decimal = Decimal("153")) -> SignalResult:
    return SignalResult(
        strategy_id="momentum_v1",
        symbol="KGC",
        direction=Direction.BUY,
        score=0.72,
        suggested_stop_loss=Decimal("6.50"),
        suggested_quantity=qty,
        features={"trend_ride": True, "rsi": 38.0},
    )


def _buy_signal(symbol: str = "GLD", qty: Decimal = Decimal("10")) -> SignalResult:
    return SignalResult(
        strategy_id="momentum_v1",
        symbol=symbol,
        direction=Direction.BUY,
        score=0.70,
        suggested_stop_loss=Decimal("185.00"),
        suggested_quantity=qty,
        features={"trend_ride": False, "rsi": 40.0},
    )


def _make_client() -> AlpacaDirectClient:
    """Connected AlpacaDirectClient with a mocked requests.Session."""
    client = AlpacaDirectClient()
    client._endpoint = "https://paper-api.alpaca.markets/v2"
    client._session = MagicMock(spec=requests.Session)
    return client


def _resp(body) -> MagicMock:
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = body
    return r


def _account_resp(equity: str = "100000") -> MagicMock:
    return _resp({"equity": equity, "status": "ACTIVE"})


def _positions_resp(positions: list[dict] | None = None) -> MagicMock:
    return _resp(positions or [])


def _open_orders_resp(orders: list[dict] | None = None) -> MagicMock:
    return _resp(orders or [])


def _order_submitted_resp(order_id: str = "alpaca-kgc-001") -> MagicMock:
    return _resp({"id": order_id, "status": "pending_new"})


def _url_routing_session(pending_orders: list[dict] | None = None) -> MagicMock:
    """Return a session mock that routes by URL path.

    Handles account, positions, and orders endpoints.
    pending_orders: list of order dicts returned by GET /orders.
    """
    session = MagicMock(spec=requests.Session)

    def get(url, **kwargs):
        if "/account" in url:
            return _account_resp()
        if "/positions" in url:
            return _positions_resp([])       # no filled positions
        if "/orders" in url:
            return _open_orders_resp(pending_orders or [])
        raise ValueError(f"unexpected GET URL: {url}")

    session.get.side_effect = get
    session.post.return_value = _order_submitted_resp()
    return session


# ── Guard 3: cross-invocation dedup via pending orders ────────────────────────

class TestPendingOrderBlocksNewBuy:
    """Task-15 primary regression: pending order from previous invocation."""

    def test_pending_buy_order_blocks_submission(self):
        """
        Reproduces the KGC bug:
          - No filled position for KGC  → old dedup (guard 1) PASSES
          - Pending BUY order exists    → new guard 3 must REJECT

        Pre-fix:  _get_open_orders() is never called; POST /orders fires.
                  result.accepted == True  →  test FAILS  (bug reproduced)
        Post-fix: guard 3 queries GET /orders, finds pending BUY, returns SKIPPED.
                  result.accepted == False →  test PASSES
        """
        client = _make_client()
        client._session.get.side_effect = [
            _account_resp(),       # GET /account
            _positions_resp([]),   # GET /positions — no filled positions
            _open_orders_resp([    # GET /orders?status=open — pending order exists
                {
                    "symbol": "KGC",
                    "side": "buy",
                    "status": "pending_new",
                    "id": "prev-run-kgc-order-171538c0",
                }
            ]),
        ]
        # post is configured so pre-fix (which calls it) doesn't raise
        client._session.post.return_value = _order_submitted_resp()

        with patch.object(client, "_record_order_pg"):
            result = client.submit_signal(_kgc_buy_signal(), current_price=7.0)

        assert result is not None
        assert result.accepted is False, (
            "Pending order should block second BUY submission (Task-15 fix)"
        )
        assert result.status == "SKIPPED"
        assert "pending" in result.message.lower(), (
            f"Expected 'pending' in message, got: {result.message!r}"
        )
        # No new order should have been submitted to Alpaca
        client._session.post.assert_not_called()

    def test_no_pending_order_allows_buy(self):
        """Happy path: no filled position, no pending order → BUY proceeds."""
        client = _make_client()
        client._session.get.side_effect = [
            _account_resp(),
            _positions_resp([]),   # no filled positions
            _open_orders_resp([]), # no pending orders
        ]
        client._session.post.return_value = _order_submitted_resp("kgc-new-order")

        with patch.object(client, "_record_order_pg"):
            result = client.submit_signal(_kgc_buy_signal(), current_price=7.0)

        assert result is not None
        assert result.accepted is True
        assert result.order_id == "kgc-new-order"
        client._session.post.assert_called_once()

    def test_pending_sell_order_does_not_block_buy(self):
        """A pending SELL order for the same symbol should not block a BUY."""
        client = _make_client()
        client._session.get.side_effect = [
            _account_resp(),
            _positions_resp([]),
            _open_orders_resp([
                {"symbol": "KGC", "side": "sell", "status": "pending_new", "id": "sell-order"}
            ]),
        ]
        client._session.post.return_value = _order_submitted_resp()

        with patch.object(client, "_record_order_pg"):
            result = client.submit_signal(_kgc_buy_signal(), current_price=7.0)

        assert result is not None
        assert result.accepted is True

    def test_open_orders_api_failure_is_nonfatal(self):
        """If GET /orders fails, log warning and proceed (do not block trading)."""
        client = _make_client()
        client._session.get.side_effect = [
            _account_resp(),
            _positions_resp([]),
            # Third GET call raises — simulates transient Alpaca API error
            requests.ConnectionError("timeout"),
        ]
        client._session.post.return_value = _order_submitted_resp()

        with patch.object(client, "_record_order_pg"):
            result = client.submit_signal(_kgc_buy_signal(), current_price=7.0)

        # Non-fatal: order proceeds despite the open-orders check failing
        assert result is not None
        assert result.accepted is True


# ── Guard 2: session-level dedup within same invocation ───────────────────────

class TestSessionDedupWithinInvocation:
    """Defense-in-depth: _submitted_symbols prevents double-submit same session."""

    def test_duplicate_buy_same_session_blocked(self):
        """
        Two submit_signal() calls for KGC in the same session.

        Pre-fix:  no _submitted_symbols tracking → second call accepted.
                  client._session.post.call_count == 2 → assert == 1 FAILS
        Post-fix: _submitted_symbols blocks second call → SKIPPED.
        """
        client = _make_client()
        client._session = _url_routing_session(pending_orders=[])
        client._session.post.return_value = _order_submitted_resp("kgc-order-1")

        signal = _kgc_buy_signal()

        with patch.object(client, "_record_order_pg"):
            result1 = client.submit_signal(signal, current_price=7.0)

        assert result1 is not None and result1.accepted is True, (
            "First BUY for KGC should be accepted"
        )

        # Second call — positions still empty (first order is pending, not filled)
        client._session.post.return_value = _order_submitted_resp("kgc-order-2")

        with patch.object(client, "_record_order_pg"):
            result2 = client.submit_signal(signal, current_price=7.0)

        assert result2 is not None
        assert result2.accepted is False, (
            "Second BUY for KGC within same session must be SKIPPED (Task-15 fix)"
        )
        assert result2.status == "SKIPPED"
        assert "session" in result2.message.lower() or "duplicate" in result2.message.lower(), (
            f"Expected session/duplicate in message, got: {result2.message!r}"
        )
        # Exactly one POST reached Alpaca
        assert client._session.post.call_count == 1, (
            f"Expected 1 order submitted, got {client._session.post.call_count}"
        )

    def test_session_dedup_does_not_block_different_symbol(self):
        """After KGC is submitted, GLD must still be submittable."""
        client = _make_client()
        client._session = _url_routing_session(pending_orders=[])

        with patch.object(client, "_record_order_pg"):
            result_kgc = client.submit_signal(_kgc_buy_signal(), current_price=7.0)
            result_gld = client.submit_signal(_buy_signal("GLD"), current_price=190.0)

        assert result_kgc is not None and result_kgc.accepted is True
        assert result_gld is not None and result_gld.accepted is True
        assert client._session.post.call_count == 2

    def test_submitted_symbols_initialised_on_new_client(self):
        """Fresh AlpacaDirectClient always has an empty _submitted_symbols set."""
        client = AlpacaDirectClient()
        assert hasattr(client, "_submitted_symbols")
        assert isinstance(client._submitted_symbols, set)
        assert len(client._submitted_symbols) == 0

    def test_submitted_symbols_reset_on_connect(self):
        """Calling connect() resets _submitted_symbols (safe for reuse)."""
        with patch("src.bridge.alpaca_direct._load_credentials",
                   return_value=("https://paper-api.alpaca.markets/v2", "K", "S")):
            client = AlpacaDirectClient()
            client._submitted_symbols.add("KGC")
            assert "KGC" in client._submitted_symbols

            client.connect()  # should reset the set
            assert len(client._submitted_symbols) == 0, (
                "connect() must reset _submitted_symbols"
            )
            client.disconnect()
