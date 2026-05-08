import os
import json
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal
import requests

from strategy.src.signals import SignalResult, Direction
from strategy.src.bridge.alpaca_direct import AlpacaDirectClient, StopLossResult

# ── Risk Gap 1: Database Fallback ───────────────────────────────────────────
def test_record_order_pg_fallback_to_jsonl_on_db_failure(tmp_path, monkeypatch):
    """
    If psycopg2 fails to insert an order, verify the order is correctly
    appended to the JSONL fallback log to prevent audit blindness.
    """
    jsonl_path = tmp_path / "failed_orders.jsonl"
    monkeypatch.setattr("strategy.src.bridge.alpaca_direct._FAILED_ORDERS_LOG", str(jsonl_path))

    # Mock psycopg2 to raise an exception
    monkeypatch.setitem(os.environ, "DATABASE_URL", "dummy_url")
    import psycopg2
    monkeypatch.setattr(psycopg2, "connect", MagicMock(side_effect=Exception("DB Down!")))

    client = AlpacaDirectClient()
    client._record_order_pg(
        client_order_id="test_client_id",
        broker_order_id="test_broker_id",
        symbol="AAPL",
        side="BUY",
        qty=Decimal("10"),
        stop_loss=Decimal("140.0"),
        signal_score=0.9,
        strategy_id="momentum_v1",
    )

    assert jsonl_path.exists(), "Fallback JSONL file was not created."
    with open(jsonl_path, "r") as f:
        lines = f.readlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["client_order_id"] == "test_client_id"
        assert record["broker_order_id"] == "test_broker_id"
        assert "db_connect_failed: DB Down!" in record["failure_reason"]

# ── Risk Gap 2: Partial Execution in Stop Loss Loop ──────────────────────────
def test_check_and_trigger_stops_continues_on_individual_error(monkeypatch):
    """
    If one symbol fails to close with an HTTP error during the stop loss loop,
    verify the loop continues and successfully evaluates/closes the remaining symbols.
    """
    client = AlpacaDirectClient()

    mock_positions = {
        "AAPL": {"symbol": "AAPL", "unrealized_plpc": "-0.06"},  # Needs stop (fails)
        "MSFT": {"symbol": "MSFT", "unrealized_plpc": "-0.07"},  # Needs stop (succeeds)
        "TSLA": {"symbol": "TSLA", "unrealized_plpc": "0.01"},   # No stop
    }
    monkeypatch.setattr(client, "_get_positions", lambda: mock_positions)

    # Mock close_position to fail on AAPL but succeed on MSFT
    def mock_close(symbol):
        if symbol == "AAPL":
            resp = MagicMock()
            resp.text = "API Limit Exceeded"
            raise requests.HTTPError(response=resp)
        return {"id": f"closed_{symbol}"}

    monkeypatch.setattr(client, "_close_position", mock_close)

    # Run
    results = client.check_and_trigger_stops(stop_loss_pct=0.05)

    assert len(results) == 2  # TSLA is ignored as positive

    aapl_res = next(r for r in results if r.alpaca_symbol == "AAPL")
    assert aapl_res.triggered is False
    assert "API Limit Exceeded" in aapl_res.error

    msft_res = next(r for r in results if r.alpaca_symbol == "MSFT")
    assert msft_res.triggered is True
    assert msft_res.order_id == "closed_MSFT"

# ── Risk Gap 3: Malformed Payload in Sector Concentration ─────────────────────
def test_sector_concentration_handles_malformed_position_data(monkeypatch):
    """
    Verify the sector concentration guard does not crash if position API
    payloads contain malformed or missing 'market_value' fields.
    """
    client = AlpacaDirectClient()

    mock_positions = {
        "AAPL": {"symbol": "AAPL", "market_value": "1000.0"},
        "MSFT": {"symbol": "MSFT", "market_value": None},         # Malformed
        "NVDA": {"symbol": "NVDA"},                               # Missing
        "GOOGL": {"symbol": "GOOGL", "market_value": "invalid"},  # Malformed
    }

    monkeypatch.setattr(client, "_get_account", lambda: {"equity": "10000"})
    monkeypatch.setattr(client, "_get_positions", lambda: mock_positions)
    monkeypatch.setattr(client, "_get_open_orders", lambda x: [])

    # Attempt to buy another big_tech to trigger the count limit (max 3)
    # We already have 4 big_tech (AAPL, MSFT, NVDA, GOOGL). It should reject.
    signal = SignalResult(
        strategy_id="momentum",
        symbol="META",
        direction=Direction.BUY,
        score=0.9,
        suggested_stop_loss=Decimal("100"),
        suggested_quantity=Decimal("1")
    )

    resp = client.submit_signal(signal, current_price=100.0)
    assert resp.accepted is False
    assert "at 4/3 position limit" in resp.message

# ── Risk Gap 4: Missing Unrealized PLPC in Stop Loss ──────────────────────────
def test_stop_loss_ignores_missing_unrealized_plpc(monkeypatch):
    """
    Verify check_and_trigger_stops does not crash if a position payload
    is completely missing the 'unrealized_plpc' field.
    """
    client = AlpacaDirectClient()

    mock_positions = {
        "AAPL": {"symbol": "AAPL"},  # Missing unrealized_plpc
        "MSFT": {"symbol": "MSFT", "unrealized_plpc": "invalid_string"},
    }
    monkeypatch.setattr(client, "_get_positions", lambda: mock_positions)

    results = client.check_and_trigger_stops()
    assert len(results) == 0  # Both should be gracefully skipped

# ── Risk Gap 5: Precision Rounding for Equities vs Crypto ─────────────────────
def test_submit_signal_rounds_crypto_quantity_correctly(monkeypatch):
    """
    Verify that equity quantities are rounded to whole units while crypto
    pairs (-USD) are rounded to 4 decimal places.
    """
    client = AlpacaDirectClient()
    monkeypatch.setattr(client, "_get_account", lambda: {"equity": "100000"})
    monkeypatch.setattr(client, "_get_positions", lambda: {})
    monkeypatch.setattr(client, "_get_open_orders", lambda x: [])
    monkeypatch.setattr(client, "_record_order_pg", lambda *args, **kwargs: None)

    # Mock actual POST
    submitted_qty = None
    def mock_post(*args, **kwargs):
        nonlocal submitted_qty
        submitted_qty = kwargs['json']['qty']
        resp = MagicMock()
        resp.json.return_value = {"id": "123", "status": "accepted"}
        return resp

    session = MagicMock()
    session.post = mock_post
    client._session = session

    # Equity test
    signal = SignalResult(
        strategy_id="momentum",
        symbol="AAPL",
        direction=Direction.BUY,
        score=0.9,
        suggested_stop_loss=Decimal("100"),
    )

    # 5000 / 150.1234 = 33.3059... -> Should round to 33
    client.submit_signal(signal, current_price=150.1234, quantity_override=Decimal("33.3059"))
    assert submitted_qty == "33"

    # Crypto test
    signal_crypto = SignalResult(
        strategy_id="momentum",
        symbol="BTC-USD",
        direction=Direction.BUY,
        score=0.9,
        suggested_stop_loss=Decimal("100"),
    )

    # 5000 / 60000 = 0.08333333 -> Should round to 0.0833
    client.submit_signal(signal_crypto, current_price=60000.0, quantity_override=Decimal("0.08333333"))
    assert submitted_qty == "0.0833"
