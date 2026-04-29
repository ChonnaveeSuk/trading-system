# trading-system/strategy/tests/test_alpaca_direct.py
#
# Unit tests for AlpacaDirectClient.
#
# All tests are offline — HTTP and DB calls are mocked.
# No Alpaca credentials or PostgreSQL required.

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from decimal import Decimal
from unittest.mock import MagicMock, patch, call
import pytest
import requests

from src.signals import Direction, SignalResult
from src.bridge.alpaca_direct import (
    AlpacaDirectClient,
    _to_alpaca_symbol,
    _ALPACA_UNSUPPORTED,
    _MAX_POSITION_PCT,
    _MAX_SECTOR_POSITIONS,
    _MAX_SECTOR_PCT,
    _MIN_SIGNAL_SCORE,
)
from src.bridge.client import BridgeResponse, HealthStatus


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _buy_signal(
    symbol: str = "AAPL",
    score: float = 0.75,
    qty: Decimal = Decimal("5"),
    stop: Decimal = Decimal("170"),
) -> SignalResult:
    return SignalResult(
        strategy_id="momentum_v1",
        symbol=symbol,
        direction=Direction.BUY,
        score=score,
        suggested_stop_loss=stop,
        suggested_quantity=qty,
    )


def _sell_signal(
    symbol: str = "AAPL",
    score: float = 0.65,
    stop: Decimal = Decimal("190"),
) -> SignalResult:
    return SignalResult(
        strategy_id="momentum_v1",
        symbol=symbol,
        direction=Direction.SELL,
        score=score,
        suggested_stop_loss=stop,
        suggested_quantity=Decimal("5"),
    )


def _hold_signal() -> SignalResult:
    return SignalResult(
        strategy_id="momentum_v1",
        symbol="AAPL",
        direction=Direction.HOLD,
        score=0.3,
    )


def _make_client() -> AlpacaDirectClient:
    """Return a connected AlpacaDirectClient with a mocked session."""
    client = AlpacaDirectClient()
    client._endpoint = "https://paper-api.alpaca.markets/v2"
    client._session = MagicMock(spec=requests.Session)
    return client


def _mock_account(equity: str = "100000") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "id": "test-account-id",
        "status": "ACTIVE",
        "equity": equity,
        "cash": equity,
        "buying_power": str(float(equity) * 2),
    }
    resp.raise_for_status.return_value = None
    return resp


def _mock_clock(is_open: bool = False) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "is_open": is_open,
        "next_open": "2026-04-17T09:30:00-04:00",
    }
    resp.raise_for_status.return_value = None
    return resp


def _mock_positions(positions: list[dict] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = positions or []
    resp.raise_for_status.return_value = None
    return resp


def _mock_order_response(
    order_id: str = "alpaca-order-123",
    status: str = "pending_new",
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"id": order_id, "status": status}
    resp.raise_for_status.return_value = None
    return resp


# ── Symbol translation ────────────────────────────────────────────────────────

class TestToAlpacaSymbol:
    def test_stock_unchanged(self):
        assert _to_alpaca_symbol("AAPL") == "AAPL"
        assert _to_alpaca_symbol("SPY") == "SPY"
        assert _to_alpaca_symbol("GLD") == "GLD"

    def test_crypto_removes_dash(self):
        assert _to_alpaca_symbol("BTC-USD") == "BTCUSD"

    def test_unsupported_returns_none(self):
        for sym in _ALPACA_UNSUPPORTED:
            assert _to_alpaca_symbol(sym) is None

    def test_gbpusd_unsupported(self):
        assert _to_alpaca_symbol("GBP-USD") is None

    def test_eurusd_unsupported(self):
        assert _to_alpaca_symbol("EUR-USD") is None

    def test_bnbusd_unsupported(self):
        assert _to_alpaca_symbol("BNB-USD") is None


# ── Health check ──────────────────────────────────────────────────────────────

class TestHealthCheck:
    def test_healthy_account(self):
        client = _make_client()
        client._session.get.side_effect = [
            _mock_account("100000"),
            _mock_clock(False),
            _mock_positions([]),
        ]
        status = client.health_check()
        assert status.healthy is True
        assert status.paper_mode is True
        assert status.portfolio_value == "100000"
        assert status.open_orders == 0

    def test_with_open_positions(self):
        client = _make_client()
        client._session.get.side_effect = [
            _mock_account("95000"),
            _mock_clock(True),
            _mock_positions([
                {"symbol": "AAPL", "qty": "10"},
                {"symbol": "BTCUSD", "qty": "0.1"},
            ]),
        ]
        status = client.health_check()
        assert status.open_orders == 2
        assert status.healthy is True


# ── HOLD signal ────────────────────────────────────────────────────────────────

class TestHoldSignal:
    def test_hold_returns_none(self):
        client = _make_client()
        result = client.submit_signal(_hold_signal(), current_price=180.0)
        assert result is None
        client._session.get.assert_not_called()


# ── Unsupported symbols ────────────────────────────────────────────────────────

class TestUnsupportedSymbols:
    @pytest.mark.parametrize("symbol", list(_ALPACA_UNSUPPORTED))
    def test_unsupported_returns_none(self, symbol):
        client = _make_client()
        signal = _buy_signal(symbol=symbol)
        result = client.submit_signal(signal, current_price=1.0)
        assert result is None
        client._session.get.assert_not_called()


# ── Risk gate: score ──────────────────────────────────────────────────────────

class TestScoreGate:
    def test_score_below_minimum_rejected(self):
        client = _make_client()
        signal = _buy_signal(score=0.54)
        result = client.submit_signal(signal, current_price=180.0)
        assert result is not None
        assert result.accepted is False
        assert "score" in result.message
        client._session.get.assert_not_called()

    def test_score_at_minimum_passes_gate(self):
        """Score exactly at threshold should proceed to API calls."""
        client = _make_client()
        client._session.get.side_effect = [
            _mock_account("100000"),
            _mock_positions([]),
        ]
        client._session.post.return_value = _mock_order_response()
        signal = _buy_signal(score=_MIN_SIGNAL_SCORE)
        with patch.object(client, "_record_order_pg"):
            result = client.submit_signal(signal, current_price=180.0)
        assert result is not None
        assert result.accepted is True


# ── Risk gate: stop_loss ──────────────────────────────────────────────────────

class TestStopLossGate:
    def test_missing_stop_loss_rejected(self):
        client = _make_client()
        signal = SignalResult(
            strategy_id="momentum_v1",
            symbol="AAPL",
            direction=Direction.BUY,
            score=0.75,
            suggested_stop_loss=None,
            suggested_quantity=Decimal("5"),
        )
        result = client.submit_signal(signal, current_price=180.0)
        assert result is not None
        assert result.accepted is False
        assert "stop_loss" in result.message


# ── Risk gate: position sizing ────────────────────────────────────────────────

class TestPositionSizing:
    def test_qty_capped_at_5pct_portfolio(self):
        """Signal requests 1000 shares; max allowed is 5% of $10,000 / $100 = 5 shares."""
        client = _make_client()
        client._session.get.side_effect = [
            _mock_account("10000"),   # equity = $10,000
            _mock_positions([]),
        ]
        submitted_payload = {}

        def capture_post(url, json=None, **kwargs):
            submitted_payload.update(json or {})
            return _mock_order_response()

        client._session.post.side_effect = capture_post
        signal = _buy_signal(qty=Decimal("1000"))  # asks for 1000 shares

        with patch.object(client, "_record_order_pg"):
            result = client.submit_signal(signal, current_price=100.0)

        assert result is not None
        assert result.accepted is True
        submitted_qty = Decimal(submitted_payload["qty"])
        max_expected = Decimal("10000") * _MAX_POSITION_PCT / Decimal("100")
        assert submitted_qty <= max_expected

    def test_zero_qty_rejected(self):
        client = _make_client()
        signal = _buy_signal(qty=Decimal("0"))
        result = client.submit_signal(signal, current_price=180.0)
        assert result is not None
        assert result.accepted is False


# ── Open position limit ────────────────────────────────────────────────────────

class TestPositionLimit:
    def test_max_positions_reached_rejected(self):
        client = _make_client()
        # 10 open positions — at limit
        client._session.get.side_effect = [
            _mock_account("100000"),
            _mock_positions([{"symbol": f"SYM{i}", "qty": "1"} for i in range(10)]),
        ]
        result = client.submit_signal(_buy_signal(), current_price=180.0)
        assert result is not None
        assert result.accepted is False
        assert "max open positions" in result.message


# ── BUY: already long ─────────────────────────────────────────────────────────

class TestBuyAlreadyLong:
    def test_skip_if_already_long(self):
        client = _make_client()
        client._session.get.side_effect = [
            _mock_account("100000"),
            _mock_positions([{"symbol": "AAPL", "qty": "10"}]),
        ]
        result = client.submit_signal(_buy_signal("AAPL"), current_price=180.0)
        assert result is not None
        assert result.accepted is False
        assert result.status == "SKIPPED"
        assert "already long" in result.message
        client._session.post.assert_not_called()


# ── SELL: no position ─────────────────────────────────────────────────────────

class TestSellNoPosition:
    def test_skip_if_no_position(self):
        client = _make_client()
        client._session.get.side_effect = [
            _mock_account("100000"),
            _mock_positions([]),
        ]
        result = client.submit_signal(_sell_signal("AAPL"), current_price=180.0)
        assert result is not None
        assert result.accepted is False
        assert result.status == "SKIPPED"
        assert "no position" in result.message
        client._session.delete.assert_not_called()


# ── Successful BUY ────────────────────────────────────────────────────────────

class TestSuccessfulBuy:
    def test_buy_submits_market_order(self):
        client = _make_client()
        client._session.get.side_effect = [
            _mock_account("100000"),
            _mock_positions([]),
        ]
        client._session.post.return_value = _mock_order_response(
            order_id="alpaca-abc123", status="pending_new"
        )

        with patch.object(client, "_record_order_pg") as mock_record:
            result = client.submit_signal(_buy_signal(), current_price=180.0)

        assert result is not None
        assert result.accepted is True
        assert result.order_id == "alpaca-abc123"
        assert result.status == "pending_new"
        # Verify order payload
        post_call = client._session.post.call_args
        payload = post_call[1]["json"]
        assert payload["symbol"] == "AAPL"
        assert payload["side"] == "buy"
        assert payload["type"] == "market"
        assert payload["time_in_force"] == "day"
        # DB record was called
        mock_record.assert_called_once()

    def test_btc_symbol_translated(self):
        client = _make_client()
        client._session.get.side_effect = [
            _mock_account("100000"),
            _mock_positions([]),
        ]
        client._session.post.return_value = _mock_order_response()

        with patch.object(client, "_record_order_pg"):
            result = client.submit_signal(
                _buy_signal(symbol="BTC-USD", qty=Decimal("0.01")),
                current_price=67000.0,
            )

        assert result is not None
        assert result.accepted is True
        payload = client._session.post.call_args[1]["json"]
        assert payload["symbol"] == "BTCUSD"


# ── Successful SELL ───────────────────────────────────────────────────────────

class TestSuccessfulSell:
    def test_sell_closes_position(self):
        client = _make_client()
        client._session.get.side_effect = [
            _mock_account("100000"),
            _mock_positions([{"symbol": "AAPL", "qty": "10"}]),
        ]
        client._session.delete.return_value = _mock_order_response(
            order_id="alpaca-close-456", status="pending_new"
        )

        with patch.object(client, "_record_order_pg") as mock_record:
            result = client.submit_signal(_sell_signal("AAPL"), current_price=185.0)

        assert result is not None
        assert result.accepted is True
        assert result.order_id == "alpaca-close-456"
        delete_url = client._session.delete.call_args[0][0]
        assert "/positions/AAPL" in delete_url
        mock_record.assert_called_once()


# ── API error handling ────────────────────────────────────────────────────────

class TestApiErrors:
    def test_http_error_on_buy_returns_error_response(self):
        client = _make_client()
        client._session.get.side_effect = [
            _mock_account("100000"),
            _mock_positions([]),
        ]
        http_err = requests.HTTPError(response=MagicMock(status_code=422, text="insufficient funds"))
        client._session.post.side_effect = http_err

        result = client.submit_signal(_buy_signal(), current_price=180.0)
        assert result is not None
        assert result.accepted is False
        assert result.status == "ERROR"

    def test_account_api_failure_returns_error_response(self):
        client = _make_client()
        client._session.get.side_effect = requests.ConnectionError("no network")

        result = client.submit_signal(_buy_signal(), current_price=180.0)
        assert result is not None
        assert result.accepted is False
        assert result.status == "ERROR"


# ── Sector concentration gate ─────────────────────────────────────────────────

def _pos(symbol: str, qty: str = "10", market_value: str = "5000") -> dict:
    """Build a fake Alpaca /v2/positions entry."""
    return {
        "symbol": symbol,
        "qty": qty,
        "market_value": market_value,
        "avg_entry_price": "100",
    }


class TestSectorGate:
    """Sector concentration limit (added 2026-04-28 incident response).

    big_tech is the densest sector in the post-rebalance universe (5 names),
    so it's the canonical saturation fixture. SPY (broad_market) stands in
    for the unrelated-sector control.
    """

    # All big_tech symbols (5-name sector — densest in tech-focus universe).
    _BIGTECH_SYMBOLS = ("AAPL", "MSFT", "NVDA", "GOOGL", "META")

    def test_sector_at_count_limit_rejects_buy(self):
        """3 big_tech positions already → 4th BUY (META) rejected."""
        client = _make_client()
        positions = [_pos(s, market_value="1000") for s in self._BIGTECH_SYMBOLS[:3]]
        client._session.get.side_effect = [
            _mock_account("100000"),
            _mock_positions(positions),
        ]
        signal = _buy_signal(symbol="META", qty=Decimal("5"))

        result = client.submit_signal(signal, current_price=500.0)

        assert result is not None
        assert result.accepted is False
        assert result.status == "REJECTED"
        assert "big_tech" in result.message
        assert f"{_MAX_SECTOR_POSITIONS}" in result.message
        client._session.post.assert_not_called()

    def test_sector_under_count_limit_passes(self):
        """2 big_tech positions → 3rd BUY proceeds (count under cap)."""
        client = _make_client()
        positions = [_pos(s, market_value="500") for s in self._BIGTECH_SYMBOLS[:2]]
        # Open-orders call is wrapped in try/except, so an empty list reply suffices.
        client._session.get.side_effect = [
            _mock_account("100000"),
            _mock_positions(positions),
            _mock_positions([]),  # open_orders → []
        ]
        client._session.post.return_value = _mock_order_response()

        signal = _buy_signal(symbol="GOOGL", qty=Decimal("5"))
        with patch.object(client, "_record_order_pg"):
            result = client.submit_signal(signal, current_price=160.0)

        assert result is not None
        assert result.accepted is True

    def test_sector_pct_breach_rejects_buy(self):
        """1 huge big_tech position pushes sector ≥30% → next BUY rejected."""
        client = _make_client()
        # Single existing position with $30,000 market_value on $100k equity =
        # exactly 30% of book before any new BUY. Adding even a $1 position
        # tips us over the cap.
        client._session.get.side_effect = [
            _mock_account("100000"),
            _mock_positions([_pos("AAPL", market_value="30000")]),
        ]
        signal = _buy_signal(symbol="MSFT", qty=Decimal("100"))  # large enough to matter

        result = client.submit_signal(signal, current_price=400.0)

        assert result is not None
        assert result.accepted is False
        assert result.status == "REJECTED"
        assert "big_tech" in result.message
        assert f"{int(float(_MAX_SECTOR_PCT) * 100)}%" in result.message
        client._session.post.assert_not_called()

    def test_other_sector_unaffected_by_saturated_sector(self):
        """big_tech at 3/3 must not block SPY (broad_market sector)."""
        client = _make_client()
        positions = [_pos(s, market_value="1000") for s in self._BIGTECH_SYMBOLS[:3]]
        client._session.get.side_effect = [
            _mock_account("100000"),
            _mock_positions(positions),
            _mock_positions([]),  # open_orders
        ]
        client._session.post.return_value = _mock_order_response()

        signal = _buy_signal(symbol="SPY", qty=Decimal("5"))
        with patch.object(client, "_record_order_pg"):
            result = client.submit_signal(signal, current_price=550.0)

        assert result is not None
        assert result.accepted is True

    def test_unmapped_symbol_skips_sector_check(self):
        """Symbol not in SYMBOL_TO_SECTOR must not block the trade — only warn."""
        client = _make_client()
        client._session.get.side_effect = [
            _mock_account("100000"),
            _mock_positions([]),
            _mock_positions([]),  # open_orders
        ]
        client._session.post.return_value = _mock_order_response()

        # ZZZZ is not in SYMBOL_TO_SECTOR → sector_for() returns "other"
        signal = _buy_signal(symbol="ZZZZ", qty=Decimal("5"))
        with patch.object(client, "_record_order_pg"):
            result = client.submit_signal(signal, current_price=50.0)

        assert result is not None
        assert result.accepted is True


# ── Hard stop loss ────────────────────────────────────────────────────────────

def _pos_with_pl(symbol: str, plpc: str, qty: str = "10") -> dict:
    """Position payload with explicit unrealized_plpc (decimal fraction)."""
    return {
        "symbol": symbol,
        "qty": qty,
        "market_value": "1000",
        "avg_entry_price": "100",
        "unrealized_plpc": plpc,
    }


class TestStopLoss:
    """check_and_trigger_stops liquidates positions past -stop_loss_pct."""

    def test_no_positions_returns_empty(self):
        client = _make_client()
        client._session.get.return_value = _mock_positions([])
        results = client.check_and_trigger_stops(stop_loss_pct=0.05)
        assert results == []
        client._session.delete.assert_not_called()

    def test_position_above_warn_skipped(self):
        """Position at -1% (above -3% warn) should not appear in results."""
        client = _make_client()
        client._session.get.return_value = _mock_positions([
            _pos_with_pl("AAPL", "-0.01"),
        ])
        results = client.check_and_trigger_stops(stop_loss_pct=0.05, warn_pct=0.03)
        assert results == []
        client._session.delete.assert_not_called()

    def test_position_at_warn_logs_only(self):
        """Position at -3.5% should be warned but not closed."""
        client = _make_client()
        client._session.get.return_value = _mock_positions([
            _pos_with_pl("AAPL", "-0.035"),
        ])
        results = client.check_and_trigger_stops(stop_loss_pct=0.05, warn_pct=0.03)
        assert len(results) == 1
        r = results[0]
        assert r.symbol == "AAPL"
        assert r.warned is True
        assert r.triggered is False
        client._session.delete.assert_not_called()

    def test_position_at_stop_triggers_close(self):
        """Position at -6.23% should fire DELETE /positions/AAPL."""
        client = _make_client()
        client._session.get.return_value = _mock_positions([
            _pos_with_pl("AAPL", "-0.0623"),
        ])
        client._session.delete.return_value = _mock_order_response(
            order_id="stop-order-123", status="pending_new",
        )
        results = client.check_and_trigger_stops(stop_loss_pct=0.05, warn_pct=0.03)
        assert len(results) == 1
        r = results[0]
        assert r.triggered is True
        assert r.warned is False
        assert r.order_id == "stop-order-123"
        delete_url = client._session.delete.call_args[0][0]
        assert "/positions/AAPL" in delete_url

    def test_telegram_alert_fired_on_trigger(self):
        client = _make_client()
        client._session.get.return_value = _mock_positions([
            _pos_with_pl("WPM", "-0.07"),
        ])
        client._session.delete.return_value = _mock_order_response()
        alerts: list[tuple[str, str]] = []

        def _alert(msg: str, level: str = "INFO") -> bool:
            alerts.append((msg, level))
            return True

        client.check_and_trigger_stops(
            stop_loss_pct=0.05, warn_pct=0.03, telegram_alert=_alert,
        )
        assert len(alerts) == 1
        msg, level = alerts[0]
        assert "Stop Loss Triggered" in msg
        assert "WPM" in msg
        assert level == "CRITICAL"

    def test_btc_symbol_reverse_translated(self):
        """Alpaca returns BTCUSD; result.symbol should be BTC-USD."""
        client = _make_client()
        client._session.get.return_value = _mock_positions([
            _pos_with_pl("BTCUSD", "-0.06", qty="0.1"),
        ])
        client._session.delete.return_value = _mock_order_response()
        results = client.check_and_trigger_stops(stop_loss_pct=0.05)
        assert len(results) == 1
        assert results[0].symbol == "BTC-USD"
        assert results[0].alpaca_symbol == "BTCUSD"

    def test_close_position_failure_recorded(self):
        client = _make_client()
        client._session.get.return_value = _mock_positions([
            _pos_with_pl("AAPL", "-0.10"),
        ])
        http_err = requests.HTTPError(
            response=MagicMock(status_code=422, text="market closed"),
        )
        client._session.delete.side_effect = http_err
        results = client.check_and_trigger_stops(stop_loss_pct=0.05)
        assert len(results) == 1
        r = results[0]
        assert r.triggered is False
        assert r.error is not None
        assert "market closed" in r.error

    def test_missing_unrealized_plpc_skipped(self):
        client = _make_client()
        client._session.get.return_value = _mock_positions([
            {"symbol": "AAPL", "qty": "10"},  # no unrealized_plpc
        ])
        results = client.check_and_trigger_stops(stop_loss_pct=0.05)
        assert results == []

    def test_positions_fetch_failure_returns_empty(self):
        client = _make_client()
        client._session.get.side_effect = requests.ConnectionError("offline")
        results = client.check_and_trigger_stops(stop_loss_pct=0.05)
        assert results == []
        client._session.delete.assert_not_called()


# ── Context manager ───────────────────────────────────────────────────────────

class TestContextManager:
    def test_context_manager_connects_and_disconnects(self):
        with patch("src.bridge.alpaca_direct._load_credentials",
                   return_value=("https://paper-api.alpaca.markets/v2", "KEY", "SECRET")):
            with AlpacaDirectClient() as client:
                assert client._session is not None
            assert client._session is None
