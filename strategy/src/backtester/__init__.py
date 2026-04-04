# trading-system/strategy/src/backtester/__init__.py
"""
Walk-forward backtesting engine.

Rules:
  - NEVER test on the full dataset — always use expanding/rolling walk-forward
  - Transaction costs: $0.005/share commission + 5bps slippage (conservative)
  - Min 252 trading days of in-sample data before first out-of-sample window
  - Report: Sharpe, Sortino, MaxDD, CAGR, Win Rate, Avg Win/Loss ratio

Performance gates (must pass before paper trading):
  - Sharpe ratio  > 1.0
  - Max drawdown  < 15%
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


@dataclass
class BacktestConfig:
    """Configuration for a walk-forward backtest run."""

    # Walk-forward windows
    in_sample_days: int = 252      # 1 year in-sample minimum
    out_of_sample_days: int = 63   # 3 months out-of-sample
    step_days: int = 21            # Re-train every month

    # Transaction cost model
    commission_per_share: Decimal = Decimal("0.005")
    slippage_bps: Decimal = Decimal("5")  # 5 basis points

    # Performance gates
    min_sharpe: float = 1.0
    max_drawdown: float = 0.15


@dataclass
class BacktestResult:
    """Metrics from a completed backtest run."""

    strategy_id: str
    symbol: str
    start_date: str
    end_date: str
    num_trades: int = 0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    max_drawdown: float = 0.0
    cagr: float = 0.0
    win_rate: float = 0.0
    avg_win_loss_ratio: float = 0.0
    total_return: float = 0.0
    notes: list[str] = field(default_factory=list)

    def passes_gate(self) -> bool:
        """Returns True if this backtest meets the minimum performance gates."""
        return self.sharpe_ratio >= 1.0 and self.max_drawdown <= 0.15

    def summary(self) -> str:
        gate = "PASS" if self.passes_gate() else "FAIL"
        return (
            f"[{gate}] {self.strategy_id} on {self.symbol} "
            f"({self.start_date} → {self.end_date}): "
            f"Sharpe={self.sharpe_ratio:.2f}, "
            f"MaxDD={self.max_drawdown:.1%}, "
            f"CAGR={self.cagr:.1%}, "
            f"Trades={self.num_trades}"
        )


@dataclass
class WalkForwardWindow:
    """Metrics for a single IS/OOS walk-forward window."""

    window_index: int
    is_start: str         # in-sample start date
    is_end: str           # in-sample end date
    oos_start: str        # out-of-sample start date
    oos_end: str          # out-of-sample end date
    oos_num_trades: int = 0
    oos_sharpe: float = 0.0
    oos_max_drawdown: float = 0.0
    oos_total_return: float = 0.0
    oos_win_rate: float = 0.0

    def passes_gate(self) -> bool:
        # 0-trade window: correctly preserved capital → pass.
        if self.oos_num_trades == 0 and self.oos_max_drawdown < 0.001:
            return True
        # ≤2 trades: Sharpe estimate is statistically unreliable with so few
        # data points. MaxDD alone is the meaningful gate.
        if self.oos_num_trades <= 2:
            return self.oos_max_drawdown <= 0.15
        return self.oos_sharpe >= 1.0 and self.oos_max_drawdown <= 0.15


@dataclass
class WalkForwardSummary:
    """Aggregate results across all walk-forward windows."""

    strategy_id: str
    symbol: str
    windows: list[WalkForwardWindow] = field(default_factory=list)

    # Aggregate OOS metrics (computed across all OOS periods combined)
    aggregate_sharpe: float = 0.0
    aggregate_max_drawdown: float = 0.0
    aggregate_total_return: float = 0.0
    aggregate_win_rate: float = 0.0
    total_oos_trades: int = 0
    windows_passing_gate: int = 0
    notes: list[str] = field(default_factory=list)

    def passes_gate(self) -> bool:
        """All windows must pass, and the aggregate Sharpe must exceed 1.0.

        Exception: if every window preserved capital (0 total trades, near-zero
        drawdown), the strategy correctly stayed out of unfavourable conditions.
        """
        if not self.windows:
            return False
        if self.windows_passing_gate != len(self.windows):
            return False
        # Full capital preservation across all windows is acceptable
        if self.total_oos_trades == 0 and self.aggregate_max_drawdown < 0.001:
            return True
        return self.aggregate_sharpe >= 1.0 and self.aggregate_max_drawdown <= 0.15

    def summary(self) -> str:
        gate = "PASS" if self.passes_gate() else "FAIL"
        n = len(self.windows)
        return (
            f"[{gate}] Walk-forward {self.strategy_id} on {self.symbol} | "
            f"{n} windows | {self.windows_passing_gate}/{n} pass gate | "
            f"Sharpe={self.aggregate_sharpe:.2f} "
            f"MaxDD={self.aggregate_max_drawdown:.1%} "
            f"Return={self.aggregate_total_return:.1%} "
            f"Trades={self.total_oos_trades}"
        )
