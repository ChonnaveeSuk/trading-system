# trading-system/scripts/tests/test_reconcile_resilience.py
#
# Regression tests for the 2026-04-25 pghfd incident.
#
# Bug chain that was fixed:
#   1. Alpaca returned status="canceled" for a duplicate KGC order
#   2. reconcile_alpaca_fills.py uppercased it to "CANCELED"
#   3. orders.status CHECK constraint rejected "CANCELED" (only allowed
#      British "CANCELLED") → psycopg2.errors.CheckViolation
#   4. Exception escaped the for loop → _sync_positions_from_alpaca
#      never ran → positions table emptied → morning report degraded
#
# These tests lock in the resilience layer of the fix:
#   1. A constraint violation on _update_order_status logs a warning and
#      reconcile() continues to the next order
#   2. _sync_positions_from_alpaca always runs after the orders loop,
#      regardless of how many UPDATEs failed
#   3. The migration_006 vocabulary covers Alpaca's full lifecycle
#
# Tests 1 and 2 run offline (mocks). Test 3 is a schema-level check that
# only runs if a live psycopg2 connection is available — otherwise skipped.

from __future__ import annotations

import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2  # noqa: E402

import reconcile_alpaca_fills as rec  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_conn() -> MagicMock:
    """psycopg2-like connection mock that supports both context-manager and
    plain dict-cursor calls."""
    conn = MagicMock()
    cur = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=cur)
    ctx.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = ctx
    conn._cur = cur
    return conn


def _http_ok(payload) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock(return_value=None)
    resp.json = MagicMock(return_value=payload)
    return resp


def _submitted_order_row(broker_id: str = "alp-1234abcd", symbol: str = "KGC") -> dict:
    return {
        "client_order_id": f"quantai-tr-{broker_id}",
        "broker_order_id": broker_id,
        "symbol": symbol,
        "side": "BUY",
        "quantity": 153,
        "signal_score": 0.72,
        "strategy_id": "trend_ride",
    }


# ── 1. Constraint violation does not crash the loop ───────────────────────────


def test_update_order_status_constraint_violation_logged(caplog):
    """A CheckViolation in _update_order_status must be caught, logged,
    and the reconcile loop must continue."""
    conn = _make_conn()
    # First fetchall() returns 1 SUBMITTED order; subsequent cursor calls
    # are the per-order UPDATE (which will raise) and the position-sync
    # statements (which must still run).
    conn._cur.fetchall.return_value = [_submitted_order_row()]

    # Make the UPDATE raise CheckViolation, but allow other executes through.
    def _execute_side_effect(sql, *args, **kwargs):
        if sql.lstrip().upper().startswith("UPDATE ORDERS SET STATUS"):
            raise psycopg2.errors.CheckViolation(
                'new row for relation "orders" violates check constraint '
                '"orders_status_check"'
            )
        return None

    conn._cur.execute.side_effect = _execute_side_effect

    with patch.object(rec.psycopg2, "connect", return_value=conn):
        session = MagicMock()
        # Order lookup returns canceled → triggers the failing UPDATE path
        session.get.return_value = _http_ok({"status": "canceled"})

        with caplog.at_level(logging.WARNING):
            result = rec.reconcile(
                "postgres://fake", "https://paper-api.alpaca.markets/v2", session,
            )

    # Did not crash — got a result dict back
    assert isinstance(result, dict)
    assert result["update_failures"] == 1, (
        f"expected 1 update_failure, got {result}"
    )
    # Warning includes the order id so an operator can find it
    assert any(
        "fail" in r.message.lower() and ("alp-1234" in r.message or "status" in r.message.lower())
        for r in caplog.records
    ), f"expected a warning naming the failed update, got: {[r.message for r in caplog.records]}"
    # Rollback was issued so the connection is reusable
    conn.rollback.assert_called()


# ── 2. Position sync runs even when an order UPDATE fails ─────────────────────


def test_sync_positions_runs_even_when_orders_loop_fails():
    """If _update_order_status raises mid-loop, _sync_positions_from_alpaca
    must still be invoked. This is the behaviour whose absence caused the
    2026-04-25 morning report to ship with empty positions."""
    conn = _make_conn()
    # One SUBMITTED row to process — the UPDATE on it will fail.
    conn._cur.fetchall.return_value = [_submitted_order_row()]

    # Every UPDATE statement raises — proves sync still runs even in the
    # worst case where every order fails to update.
    def _execute_side_effect(sql, *args, **kwargs):
        if sql.lstrip().upper().startswith("UPDATE ORDERS SET STATUS"):
            raise psycopg2.errors.CheckViolation("constraint violation")
        return None

    conn._cur.execute.side_effect = _execute_side_effect

    with patch.object(rec.psycopg2, "connect", return_value=conn):
        with patch.object(rec, "_sync_positions_from_alpaca") as mock_sync:
            session = MagicMock()
            session.get.return_value = _http_ok({"status": "canceled"})

            result = rec.reconcile(
                "postgres://fake", "https://paper-api.alpaca.markets/v2", session,
            )

            mock_sync.assert_called_once(), (
                "_sync_positions_from_alpaca must run even when orders-loop "
                "UPDATEs fail — this is the core fix for pghfd"
            )

    assert result["positions_synced"] is True
    assert result["update_failures"] == 1


def test_sync_positions_runs_when_sync_itself_raises():
    """If _sync_positions_from_alpaca raises (e.g. Alpaca outage), reconcile
    must still complete and return a structured result, with positions_synced=False."""
    conn = _make_conn()
    conn._cur.fetchall.return_value = []  # no SUBMITTED orders

    with patch.object(rec.psycopg2, "connect", return_value=conn):
        with patch.object(
            rec, "_sync_positions_from_alpaca",
            side_effect=RuntimeError("alpaca outage"),
        ):
            result = rec.reconcile(
                "postgres://fake", "https://paper-api.alpaca.markets/v2", MagicMock(),
            )

    assert result["positions_synced"] is False
    assert result["fills_written"] == 0


# ── 3. Schema-level: full status vocabulary is accepted by the constraint ─────


def _live_db_url() -> str | None:
    """Return DATABASE_URL only if it points at a reachable Postgres."""
    url = os.environ.get(
        "DATABASE_URL",
        "postgres://quantai:quantai_dev_2026@localhost:5432/quantai",
    )
    try:
        c = psycopg2.connect(url, connect_timeout=2)
        c.close()
        return url
    except Exception:
        return None


_LIVE_URL = _live_db_url()
_MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "infra", "postgres", "migration_006_orders_status_lifecycle.sql",
)


@pytest.mark.skipif(_LIVE_URL is None, reason="No live Postgres available")
def test_full_status_vocabulary_accepted():
    """All Alpaca lifecycle statuses must be insertable after migration_006.

    Applies the migration in a SAVEPOINT, attempts an INSERT for each
    status, then rolls back so the schema is unchanged after the test.
    """
    statuses = [
        "PENDING", "SUBMITTED", "ACCEPTED", "NEW",
        "PARTIAL_FILL", "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED", "CANCELLED",
        "EXPIRED", "REJECTED", "REPLACED",
        "PENDING_CANCEL", "PENDING_REPLACE",
    ]

    with open(_MIGRATION_PATH) as f:
        migration_sql = f.read()

    # Apply the migration in autocommit so its embedded BEGIN/COMMIT cycle
    # closes cleanly. Then run the per-status checks inside a fresh
    # transaction that we'll roll back at the end.
    conn = psycopg2.connect(_LIVE_URL)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(migration_sql)

        conn.autocommit = False
        with conn.cursor() as cur:
            for status in statuses:
                cur.execute("SAVEPOINT s_status_test")
                try:
                    cur.execute(
                        """
                        INSERT INTO orders (
                            client_order_id, symbol, side, order_type,
                            quantity, stop_loss, status
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            f"test-status-{status}-vocab", "TEST", "BUY",
                            "MARKET", 1, 0.01, status,
                        ),
                    )
                except psycopg2.errors.CheckViolation as e:
                    pytest.fail(
                        f"status={status!r} rejected by constraint after "
                        f"migration_006: {e}"
                    )
                finally:
                    cur.execute("ROLLBACK TO SAVEPOINT s_status_test")
    finally:
        conn.rollback()  # leave schema untouched
        conn.close()
