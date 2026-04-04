# trading-system/strategy/src/bridge/client.py
#
# gRPC client: sends trading signals from Python strategy to Rust OMS.
#
# Usage::
#
#     with TradingBridgeClient() as client:
#         health = client.health_check()
#         response = client.submit_signal(signal, current_price=180.00)
#
# The client converts SignalResult → SignalRequest proto → gRPC call → parses response.
# HOLD signals are filtered here — the Rust server expects only BUY/SELL.

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

import grpc

from ..signals import Direction, SignalResult
from . import trading_pb2, trading_pb2_grpc

logger = logging.getLogger(__name__)


class BridgeResponse:
    """Parsed response from the Rust execution engine."""

    def __init__(self, accepted: bool, order_id: str, status: str, message: str) -> None:
        self.accepted = accepted
        self.order_id = order_id
        self.status = status
        self.message = message

    def __repr__(self) -> str:
        return (
            f"BridgeResponse(accepted={self.accepted}, status={self.status!r}, "
            f"order_id={self.order_id!r}, message={self.message!r})"
        )


class HealthStatus:
    """Engine health snapshot."""

    def __init__(self, healthy: bool, paper_mode: bool, portfolio_value: str,
                 open_orders: int, pubsub_active: bool) -> None:
        self.healthy = healthy
        self.paper_mode = paper_mode
        self.portfolio_value = portfolio_value
        self.open_orders = open_orders
        self.pubsub_active = pubsub_active

    def __repr__(self) -> str:
        return (
            f"HealthStatus(healthy={self.healthy}, paper_mode={self.paper_mode}, "
            f"portfolio=${self.portfolio_value}, open_orders={self.open_orders})"
        )


class TradingBridgeClient:
    """gRPC client for the Rust TradingBridge service.

    Thread-safe: a single instance can be shared across coroutines
    (grpc channels are multiplexed internally).

    Args:
        host: Rust engine host. Default: localhost.
        port: gRPC port. Default: 50051.
        timeout: Per-call deadline in seconds.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 50051,
        timeout: float = 5.0,
    ) -> None:
        self._target = f"{host}:{port}"
        self._timeout = timeout
        self._channel: Optional[grpc.Channel] = None
        self._stub: Optional[trading_pb2_grpc.TradingBridgeStub] = None

    # ── Connection management ─────────────────────────────────────────────────

    def connect(self) -> None:
        """Open the gRPC channel."""
        self._channel = grpc.insecure_channel(self._target)
        self._stub = trading_pb2_grpc.TradingBridgeStub(self._channel)
        logger.info("TradingBridgeClient: connected to %s", self._target)

    def disconnect(self) -> None:
        """Close the gRPC channel."""
        if self._channel:
            self._channel.close()
            self._channel = None
            self._stub = None
            logger.info("TradingBridgeClient: disconnected")

    def __enter__(self) -> "TradingBridgeClient":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()

    def _get_stub(self) -> trading_pb2_grpc.TradingBridgeStub:
        if self._stub is None:
            self.connect()
        return self._stub  # type: ignore[return-value]

    # ── API ───────────────────────────────────────────────────────────────────

    def health_check(self) -> HealthStatus:
        """Ping the Rust engine and return its current state."""
        stub = self._get_stub()
        try:
            resp = stub.HealthCheck(
                trading_pb2.HealthRequest(),
                timeout=self._timeout,
            )
            return HealthStatus(
                healthy=resp.healthy,
                paper_mode=resp.paper_mode,
                portfolio_value=resp.portfolio_value,
                open_orders=resp.open_orders,
                pubsub_active=resp.pubsub_active,
            )
        except grpc.RpcError as e:
            logger.error("HealthCheck failed: %s", e)
            raise

    def submit_signal(
        self,
        signal: SignalResult,
        current_price: float,
        quantity_override: Optional[Decimal] = None,
    ) -> Optional[BridgeResponse]:
        """Send a trading signal to the Rust execution engine.

        Args:
            signal:            SignalResult from the strategy.
            current_price:     Latest market price (for risk check in Rust).
            quantity_override: Override signal's suggested_quantity if set.

        Returns:
            BridgeResponse if signal was BUY/SELL.
            None if signal was HOLD (filtered client-side, not sent).

        Raises:
            grpc.RpcError: On network or server error.
        """
        if signal.direction == Direction.HOLD:
            logger.debug("%s: HOLD signal — not sent to Rust bridge.", signal.symbol)
            return None

        if signal.suggested_stop_loss is None:
            logger.warning(
                "%s: BUY/SELL signal missing stop_loss — not sent.", signal.symbol
            )
            return None

        qty = quantity_override or signal.suggested_quantity
        if qty is None or qty <= Decimal("0"):
            logger.warning("%s: invalid quantity %s — not sent.", signal.symbol, qty)
            return None

        req = trading_pb2.SignalRequest(
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            direction=signal.direction.value,
            score=signal.score,
            stop_loss=str(signal.suggested_stop_loss),
            quantity=str(qty),
            current_price=str(round(current_price, 8)),
        )

        logger.info(
            "Sending signal: %s %s @ $%.4f  score=%.4f  stop=%s  qty=%s",
            signal.direction.value,
            signal.symbol,
            current_price,
            signal.score,
            signal.suggested_stop_loss,
            qty,
        )

        stub = self._get_stub()
        try:
            resp = stub.SubmitSignal(req, timeout=self._timeout)
        except grpc.RpcError as e:
            logger.error("SubmitSignal RPC failed: %s", e)
            raise

        result = BridgeResponse(
            accepted=resp.accepted,
            order_id=resp.order_id,
            status=resp.status,
            message=resp.message,
        )

        if result.accepted:
            logger.info(
                "Signal ACCEPTED: order_id=%s  status=%s",
                result.order_id,
                result.status,
            )
        else:
            logger.warning(
                "Signal REJECTED: status=%s  reason=%s",
                result.status,
                result.message,
            )

        return result
