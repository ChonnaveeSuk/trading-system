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


# TODO Phase 2: implement WalkForwardBacktester class
