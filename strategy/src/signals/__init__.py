# trading-system/strategy/src/signals/__init__.py
"""
Signal generators. Each strategy produces a SignalResult that the gRPC bridge
converts into an Order inside the Rust execution engine.

Signal quality rules (non-negotiable):
  - Min 252 trading days lookback before trusting any signal
  - Walk-forward validation only — never backtest on the full dataset
  - Always include commission + slippage in backtest P&L
  - Max 3 free parameters per strategy to prevent overfitting
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class SignalResult:
    """Output of a signal generator.

    The Rust bridge reads this and converts it to an Order after passing
    through the risk engine. A HOLD signal is discarded at the bridge layer.
    """

    strategy_id: str
    symbol: str
    direction: Direction
    score: float  # [0.0, 1.0] — must be >= 0.55 or risk engine rejects
    suggested_stop_loss: Decimal | None = None
    suggested_quantity: Decimal | None = None
    features: dict | None = None  # Feature vector logged to BigQuery for audit

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"score must be in [0.0, 1.0], got {self.score}")


from .momentum import MomentumStrategy, MomentumConfig  # noqa: F401  (available for import)
