# trading-system/strategy/tests/test_signal_type_attribution.py
#
# A/B attribution tests: signal_type persisted in orders table.
#
# All tests are pure unit tests (no live DB required) except
# test_migration_003_idempotent which connects to local Postgres and
# is skipped automatically when DATABASE_URL is unavailable.

from __future__ import annotations

import logging
import os
import sys
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.signals import Direction, SignalResult
from src.bridge.alpaca_direct import AlpacaDirectClient


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_signal(
    direction: Direction = Direction.BUY,
    trend_ride: bool = False,
    features: dict | None = None,
) -> SignalResult:
    feat = features if features is not None else {
        "trend_ride": trend_ride,
        "rsi": 35.0,
        "fast_ma": 100.0,
        "slow_ma": 98.0,
    }
    return SignalResult(
        strategy_id="momentum_v1",
        symbol="GLD",
        direction=direction,
        score=0.72,
        suggested_stop_loss=Decimal("185.00"),
        suggested_quantity=Decimal("10"),
        features=feat,
    )


def _make_client() -> AlpacaDirectClient:
    """Build an AlpacaDirectClient with credentials pre-loaded (no GCP calls)."""
    client = AlpacaDirectClient.__new__(AlpacaDirectClient)
    client._endpoint = "https://paper-api.alpaca.markets/v2"
    client._api_key = "FAKE_KEY"
    client._secret_key = "FAKE_SECRET"
    client._equity = Decimal("100000")
    client._session = None
    client._submitted_symbols = set()  # required by Task-15 session dedup guard
    return client


# ── client_order_id encoding ─────────────────────────────────────────────────

def test_client_order_id_encodes_momentum():
    """Momentum signal → client_order_id contains '-mom-'."""
    signal = _make_signal(direction=Direction.BUY, trend_ride=False)
    client = _make_client()

    account_resp = {"equity": "100000", "status": "ACTIVE"}
    positions_resp = {}
    order_resp = {"id": "broker-uuid-001", "status": "accepted"}

    with patch.object(client, "_get_account", return_value=account_resp), \
         patch.object(client, "_get_positions", return_value=positions_resp), \
         patch.object(client, "_submit_market_order", return_value=order_resp), \
         patch.object(client, "_record_order_pg") as mock_record, \
         patch("time.sleep"):
        resp = client.submit_signal(signal, current_price=190.0)

    assert resp is not None
    assert resp.accepted
    recorded_client_id = mock_record.call_args.kwargs["client_order_id"]
    assert "-mom-" in recorded_client_id, (
        f"Expected '-mom-' in client_order_id, got: {recorded_client_id}"
    )
    assert recorded_client_id.startswith("quantai-mom-")


def test_client_order_id_encodes_trend_ride():
    """Trend-ride signal → client_order_id contains '-tr-'."""
    signal = _make_signal(direction=Direction.BUY, trend_ride=True)
    client = _make_client()

    account_resp = {"equity": "100000", "status": "ACTIVE"}
    positions_resp = {}
    order_resp = {"id": "broker-uuid-002", "status": "accepted"}

    with patch.object(client, "_get_account", return_value=account_resp), \
         patch.object(client, "_get_positions", return_value=positions_resp), \
         patch.object(client, "_submit_market_order", return_value=order_resp), \
         patch.object(client, "_record_order_pg") as mock_record, \
         patch("time.sleep"):
        resp = client.submit_signal(signal, current_price=190.0)

    assert resp is not None
    assert resp.accepted
    recorded_client_id = mock_record.call_args.kwargs["client_order_id"]
    assert "-tr-" in recorded_client_id, (
        f"Expected '-tr-' in client_order_id, got: {recorded_client_id}"
    )
    assert recorded_client_id.startswith("quantai-tr-")


# ── DB insert persists signal_type ───────────────────────────────────────────

def test_order_insert_persists_signal_type():
    """_record_order_pg INSERT includes signal_type column."""
    client = _make_client()

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch("psycopg2.connect", return_value=mock_conn):
        client._record_order_pg(
            client_order_id="quantai-tr-" + str(uuid.uuid4()),
            broker_order_id="broker-id-123",
            symbol="GLD",
            side="BUY",
            qty=Decimal("10"),
            stop_loss=Decimal("185.00"),
            signal_score=0.72,
            strategy_id="momentum_v1",
            signal_type="trend_ride",
        )

    executed_sql = mock_cur.execute.call_args[0][0]
    params = mock_cur.execute.call_args[0][1]
    assert "signal_type" in executed_sql
    assert "trend_ride" in params


def test_order_insert_persists_momentum_signal_type():
    """_record_order_pg INSERT records 'momentum' for standard signals."""
    client = _make_client()

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch("psycopg2.connect", return_value=mock_conn):
        client._record_order_pg(
            client_order_id="quantai-mom-" + str(uuid.uuid4()),
            broker_order_id="broker-id-456",
            symbol="GLD",
            side="BUY",
            qty=Decimal("10"),
            stop_loss=Decimal("185.00"),
            signal_score=0.65,
            strategy_id="momentum_v1",
            signal_type="momentum",
        )

    params = mock_cur.execute.call_args[0][1]
    assert "momentum" in params


# ── Missing features → default to momentum ───────────────────────────────────

def test_missing_signal_type_defaults_to_momentum(caplog):
    """Signal with no features dict → signal_type defaults to 'momentum' + WARNING logged."""
    signal = SignalResult(
        strategy_id="momentum_v1",
        symbol="GLD",
        direction=Direction.BUY,
        score=0.65,
        suggested_stop_loss=Decimal("185.00"),
        suggested_quantity=Decimal("10"),
        features=None,  # missing features
    )
    client = _make_client()

    account_resp = {"equity": "100000", "status": "ACTIVE"}
    positions_resp = {}
    order_resp = {"id": "broker-uuid-003", "status": "accepted"}

    with patch.object(client, "_get_account", return_value=account_resp), \
         patch.object(client, "_get_positions", return_value=positions_resp), \
         patch.object(client, "_submit_market_order", return_value=order_resp), \
         patch.object(client, "_record_order_pg") as mock_record, \
         patch("time.sleep"), \
         caplog.at_level(logging.WARNING):
        resp = client.submit_signal(signal, current_price=190.0)

    assert resp is not None
    assert resp.accepted
    recorded_client_id = mock_record.call_args.kwargs["client_order_id"]
    assert "-mom-" in recorded_client_id
    # WARNING about missing features should be in logs
    assert any("no features" in r.message.lower() or "defaulting" in r.message.lower()
               for r in caplog.records), (
        f"Expected a warning about missing features. Got: {[r.message for r in caplog.records]}"
    )
    # signal_type kwarg must default to momentum
    assert mock_record.call_args.kwargs["signal_type"] == "momentum"


# ── Migration idempotency ─────────────────────────────────────────────────────

def _can_connect_local_db() -> bool:
    db_url = os.environ.get(
        "DATABASE_URL", "postgres://quantai:quantai_dev_2026@localhost:5432/quantai"
    )
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=3)
        conn.close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _can_connect_local_db(), reason="local Postgres not available")
def test_migration_003_idempotent():
    """Run migration_003 twice — no errors, columns exist with correct defaults."""
    import psycopg2

    db_url = os.environ.get(
        "DATABASE_URL", "postgres://quantai:quantai_dev_2026@localhost:5432/quantai"
    )
    migration_path = os.path.join(
        os.path.dirname(__file__),
        "../../infra/postgres/migration_003_signal_type.sql",
    )
    with open(migration_path) as f:
        migration_sql = f.read()

    conn = psycopg2.connect(db_url)
    try:
        # Run twice — must not raise
        with conn.cursor() as cur:
            cur.execute(migration_sql)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(migration_sql)
        conn.commit()

        # Verify columns exist on orders
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name, column_default, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'orders'
                  AND column_name IN ('signal_type', 'exit_reason', 'client_order_id')
                ORDER BY column_name
            """)
            rows = {r[0]: r for r in cur.fetchall()}

        assert "signal_type" in rows, "signal_type column missing from orders"
        assert "exit_reason" in rows, "exit_reason column missing from orders"
        assert "momentum" in (rows["signal_type"][1] or ""), (
            f"signal_type default should be 'momentum', got: {rows['signal_type'][1]}"
        )
        # client_order_id must no longer be UUID type
        with conn.cursor() as cur:
            cur.execute("""
                SELECT data_type FROM information_schema.columns
                WHERE table_name = 'orders' AND column_name = 'client_order_id'
            """)
            col_type = cur.fetchone()[0]
        assert col_type == "character varying", (
            f"client_order_id should be VARCHAR, got: {col_type}"
        )

        # Verify columns exist on positions
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'positions' AND column_name = 'signal_type'
            """)
            assert cur.fetchone() is not None, "signal_type column missing from positions"
    finally:
        conn.close()
