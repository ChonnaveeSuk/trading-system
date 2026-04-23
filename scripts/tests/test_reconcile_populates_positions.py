# trading-system/scripts/tests/test_reconcile_populates_positions.py
#
# Regression tests for the 2026-04-23 morning-report bug.
#
# Root cause: scripts/reconcile_alpaca_fills.py wrote fills + updated
# order status but never populated the `positions` table.  On Cloud Run
# (ALPACA_DIRECT=1) the Rust OMS — the only other writer — does not run,
# so `positions` stayed empty despite 10 open long positions on Alpaca.
#
# These tests lock in the fix:
#   1. After fills reconciliation, sync positions from GET /v2/positions
#   2. Stale DB rows (symbols not in Alpaca response) are zeroed out
#   3. Alpaca API failure during sync must not crash reconcile — fills
#      that were already inserted are preserved, sync is skipped with a
#      warning
#
# All tests run offline — HTTP and DB are mocked.

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import reconcile_alpaca_fills as rec  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_conn() -> MagicMock:
    """Build a psycopg2-like connection mock that records cursor.execute calls."""
    conn = MagicMock()
    cur = MagicMock()
    # Context-manager cursor — `with conn.cursor() as cur:`
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=cur)
    ctx.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = ctx
    conn._cur = cur  # expose for assertions
    return conn


def _alpaca_positions_payload() -> list[dict]:
    """Three open positions returned by Alpaca /v2/positions."""
    return [
        {
            "symbol": "GLD",
            "qty": "11",
            "avg_entry_price": "434.35",
            "unrealized_pl": "-5.50",
            "side": "long",
        },
        {
            "symbol": "KGC",
            "qty": "153",
            "avg_entry_price": "32.35",
            "unrealized_pl": "12.00",
            "side": "long",
        },
        {
            "symbol": "BTCUSD",            # Alpaca-style crypto
            "qty": "0.05",
            "avg_entry_price": "67000.00",
            "unrealized_pl": "0.00",
            "side": "long",
        },
    ]


def _mock_http_response(payload) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock(return_value=None)
    resp.json = MagicMock(return_value=payload)
    return resp


# ── 1. Happy path: sync populates positions ───────────────────────────────────

def test_sync_populates_three_positions():
    """After _sync_positions_from_alpaca, 3 rows are upserted with correct data."""
    conn = _make_conn()
    session = MagicMock()
    session.get.return_value = _mock_http_response(_alpaca_positions_payload())

    rec._sync_positions_from_alpaca(conn, session, "https://paper-api.alpaca.markets/v2")

    # Session called GET /positions
    session.get.assert_called_once()
    url = session.get.call_args[0][0]
    assert url.endswith("/positions"), f"expected /positions call, got {url}"

    # 3 INSERT ... ON CONFLICT upserts were issued (one per Alpaca position)
    cur = conn._cur
    upsert_calls = [c for c in cur.execute.call_args_list
                    if "INSERT INTO positions" in c.args[0]]
    assert len(upsert_calls) == 3, (
        f"expected 3 upserts, got {len(upsert_calls)}: "
        f"{[c.args[0][:40] for c in cur.execute.call_args_list]}"
    )

    # Extract (symbol, qty, avg_cost, unreal) tuples for the first 4 positional args
    got = sorted((c.args[1][0], c.args[1][1], c.args[1][2], c.args[1][3])
                 for c in upsert_calls)

    # Crypto symbol must be translated from Alpaca-style back to yfinance-style
    assert ("BTC-USD", "0.05", "67000.00", "0.00") in got, (
        f"BTC-USD not in upserts (bad crypto translation?): {got}"
    )
    assert any(sym == "GLD" for sym, *_ in got)
    assert any(sym == "KGC" for sym, *_ in got)

    # Commit happened
    conn.commit.assert_called()


# ── 2. Stale rows: symbol no longer in Alpaca → quantity = 0 ──────────────────

def test_sync_zeros_out_stale_positions():
    """A symbol present in our DB but no longer open on Alpaca is zeroed."""
    conn = _make_conn()
    session = MagicMock()
    session.get.return_value = _mock_http_response(_alpaca_positions_payload())

    rec._sync_positions_from_alpaca(conn, session, "https://paper-api.alpaca.markets/v2")

    cur = conn._cur
    zero_calls = [c for c in cur.execute.call_args_list
                  if "UPDATE positions" in c.args[0] and "quantity = 0" in c.args[0]]
    assert len(zero_calls) >= 1, (
        "expected at least one UPDATE positions … quantity = 0 to zero-out stale rows. "
        f"got: {[c.args[0][:60] for c in cur.execute.call_args_list]}"
    )


# ── 3. Empty Alpaca response must still work (no NOT IN () bug) ───────────────

def test_sync_handles_zero_open_positions():
    """When Alpaca returns [], the zero-out UPDATE must still work (no SQL error)."""
    conn = _make_conn()
    session = MagicMock()
    session.get.return_value = _mock_http_response([])

    # Must not raise (empty IN () is a classic Postgres pitfall)
    rec._sync_positions_from_alpaca(conn, session, "https://paper-api.alpaca.markets/v2")

    cur = conn._cur
    upsert_calls = [c for c in cur.execute.call_args_list
                    if "INSERT INTO positions" in c.args[0]]
    assert len(upsert_calls) == 0
    # Commit still runs
    conn.commit.assert_called()


# ── 4. Alpaca API failure: sync is non-fatal ──────────────────────────────────

def test_sync_nonfatal_on_api_failure(caplog):
    """If GET /positions raises, _sync_positions_from_alpaca logs a warning
    and returns without raising — so reconcile() can still commit its fills."""
    import logging
    conn = _make_conn()
    session = MagicMock()
    import requests
    session.get.side_effect = requests.ConnectionError("alpaca down")

    with caplog.at_level(logging.WARNING):
        # Must NOT raise
        rec._sync_positions_from_alpaca(conn, session, "https://paper-api.alpaca.markets/v2")

    assert any("sync" in r.message.lower() or "positions" in r.message.lower()
               for r in caplog.records), (
        f"expected a warning about sync failure, got: {[r.message for r in caplog.records]}"
    )


# ── 5. End-to-end: reconcile() calls sync after processing fills ──────────────

def test_reconcile_invokes_position_sync(monkeypatch):
    """reconcile() must call _sync_positions_from_alpaca once, after the
    SUBMITTED-orders loop.  Proves the wiring is correct."""
    # Make psycopg2.connect return our mock conn
    conn = _make_conn()
    monkeypatch.setattr(rec.psycopg2, "connect", MagicMock(return_value=conn))

    # No SUBMITTED orders to process — keep the happy-path minimal
    conn._cur.fetchall.return_value = []

    session = MagicMock()
    session.get.return_value = _mock_http_response([])

    with patch.object(rec, "_sync_positions_from_alpaca") as mock_sync:
        rec.reconcile("postgres://fake", "https://paper-api.alpaca.markets/v2", session)
        mock_sync.assert_called_once()
