# trading-system/strategy/src/backtester/engine.py
#
# Paper trading backtester — simulates the strategy on historical OHLCV data.
#
# Transaction cost model:
#   - Commission: $0.005/share (IBKR fixed-rate), $1.00 minimum, 0.5% cap
#   - Slippage: 0.5 bps one-way directional (matches PaperBroker in Rust)
#
# NOTE on 30-day data limitation:
#   The design requires ≥252 trading days before trusting any backtest.
#   With only 30 days seeded, Sharpe/MaxDD results are INDICATIVE only.
#   This engine flags results as development-only when bars < 252.
#
# Walk-forward logic:
#   For ≥252 bars:  rolling in-sample=252, out-of-sample=63, step=21
#   For <252 bars:  single in-sample pass across all bars (dev mode)

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

import numpy as np
import pandas as pd

from . import BacktestConfig, BacktestResult, WalkForwardWindow, WalkForwardSummary
from ..signals.momentum import MomentumStrategy

logger = logging.getLogger(__name__)

# Transaction cost constants (mirror PaperBroker in Rust)
COMMISSION_PER_SHARE = 0.005
MIN_COMMISSION = 1.00
MAX_COMMISSION_PCT = 0.005
SLIPPAGE_BPS = 0.5


def _calc_commission(qty: float, price: float) -> float:
    raw = qty * COMMISSION_PER_SHARE
    floored = max(raw, MIN_COMMISSION)
    cap = qty * price * MAX_COMMISSION_PCT
    return min(floored, cap)


def _apply_slippage(price: float, is_buy: bool) -> float:
    factor = SLIPPAGE_BPS / 10_000
    return price * (1 + factor) if is_buy else price * (1 - factor)


def _compute_atr_series(price_df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Rolling ATR(period) aligned to price_df index.

    Uses simple rolling mean of True Range (mirrors atr_from_bars in Rust).
    Returns NaN for the first `period` bars before the window is warm.
    """
    high = price_df["high"].astype(float)
    low  = price_df["low"].astype(float)
    close = price_df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


class BacktestEngine:
    """Simulates a strategy on OHLCV data and computes performance metrics.

    Usage::

        engine = BacktestEngine(config=BacktestConfig())
        result = engine.run("AAPL", df, MomentumStrategy())
        print(result.summary())
    """

    def __init__(self, config: BacktestConfig = BacktestConfig()) -> None:
        self.config = config

    def run(
        self,
        symbol: str,
        df: pd.DataFrame,
        strategy: MomentumStrategy,
        starting_capital: float = 100_000.0,
        position_pct: float = 0.02,
        regime_df: Optional[pd.DataFrame] = None,
    ) -> BacktestResult:
        """Run a backtest on the given OHLCV DataFrame.

        Args:
            symbol:           Ticker symbol.
            df:               OHLCV DataFrame (index=timestamp ascending).
            strategy:         Signal generator to use.
            starting_capital: Starting portfolio value.
            position_pct:     Fraction of capital per trade.

        Returns:
            BacktestResult with full metrics.
        """
        if df.empty:
            return BacktestResult(
                strategy_id=strategy.config.__class__.__name__,
                symbol=symbol,
                start_date="",
                end_date="",
                notes=["No data provided"],
            )

        dev_mode = len(df) < 252
        if dev_mode:
            logger.warning(
                "Backtesting %s with only %d bars. Min 252 required for production. "
                "Results are INDICATIVE ONLY.",
                symbol,
                len(df),
            )

        # Generate signals for all bars (with optional bar-by-bar regime filter)
        signals = strategy.generate_signals_series(symbol, df, regime_df=regime_df)

        start_date = str(df.index[0].date())
        end_date = str(df.index[-1].date())

        # ── Simulate trades ───────────────────────────────────────────────────
        capital = starting_capital
        peak_capital = starting_capital
        position_qty = 0.0
        position_cost = 0.0
        equity_curve: list[float] = [capital]
        daily_returns: list[float] = []
        prev_mtm = starting_capital

        trades: list[dict] = []

        # Pre-compute ATR for trailing stop (aligned to df index)
        atr_series_run: Optional[pd.Series] = (
            _compute_atr_series(df) if self.config.trailing_stop else None
        )
        trail_distance: float = 0.0
        trail_high: float = 0.0
        trail_stop: float = 0.0

        # Pre-extract arrays: eliminates per-iteration pandas .loc overhead.
        # signals covers a subset of df (after MA warmup); align prices to it.
        closes_run = df.loc[signals.index, "close"].to_numpy(dtype=float)
        dirs_run   = signals["direction"].to_numpy()
        scores_run = signals["score"].to_numpy(dtype=float)
        atr_run_vals = (
            atr_series_run.reindex(signals.index).to_numpy(dtype=float)
            if atr_series_run is not None else None
        )

        for i, ts in enumerate(signals.index):
            price     = closes_run[i]
            direction = str(dirs_run[i])
            score     = scores_run[i]

            # Update trailing stop ratchet before processing the signal
            stop_triggered = False
            if self.config.trailing_stop and position_qty > 0 and trail_distance > 0:
                trail_high = max(trail_high, price)
                new_stop = trail_high - trail_distance
                trail_stop = max(trail_stop, new_stop)
                if price <= trail_stop:
                    stop_triggered = True
                    direction = "SELL"

            if direction == "BUY" and position_qty == 0:
                notional = capital * position_pct
                qty = notional / price
                fill_price = _apply_slippage(price, is_buy=True)
                commission = _calc_commission(qty, fill_price)
                cost = qty * fill_price + commission

                if cost <= capital:
                    capital -= cost
                    position_qty = qty
                    position_cost = qty * fill_price

                    trail_high = fill_price
                    atr_raw = float("nan")
                    if atr_run_vals is not None:
                        v = atr_run_vals[i]
                        if not math.isnan(v) and v > 0:
                            atr_raw = v
                    trail_distance = (
                        self.config.trailing_stop_atr_mult * atr_raw
                        if not math.isnan(atr_raw)
                        else fill_price * 0.02
                    )
                    trail_stop = fill_price - trail_distance

                    trades.append({
                        "date": str(ts.date()),
                        "side": "BUY",
                        "qty": qty,
                        "price": fill_price,
                        "commission": commission,
                        "score": score,
                        "trail_stop_initial": round(trail_stop, 5),
                    })

            elif direction == "SELL" and position_qty > 0:
                fill_price = _apply_slippage(price, is_buy=False)
                commission = _calc_commission(position_qty, fill_price)
                proceeds = position_qty * fill_price - commission
                pnl = proceeds - position_cost
                capital += proceeds
                trades.append({
                    "date": str(ts.date()),
                    "side": "SELL (trail)" if stop_triggered else "SELL",
                    "qty": position_qty,
                    "price": fill_price,
                    "commission": commission,
                    "pnl": pnl,
                    "score": score,
                })
                position_qty = 0.0
                position_cost = 0.0
                trail_distance = 0.0
                trail_high = 0.0
                trail_stop = 0.0

            mtm_value = capital + position_qty * price
            equity_curve.append(mtm_value)
            peak_capital = max(peak_capital, mtm_value)
            daily_returns.append(
                (mtm_value - prev_mtm) / prev_mtm if prev_mtm > 0 else 0.0
            )
            prev_mtm = mtm_value

        # Close any open position at last close
        if position_qty > 0:
            last_price = float(df["close"].iloc[-1])
            fill_price = _apply_slippage(last_price, is_buy=False)
            commission = _calc_commission(position_qty, fill_price)
            proceeds = position_qty * fill_price - commission
            pnl = proceeds - position_cost
            capital += proceeds
            trades.append({
                "date": end_date,
                "side": "SELL (EOD close)",
                "qty": position_qty,
                "price": fill_price,
                "commission": commission,
                "pnl": pnl,
                "score": 0.0,
            })
            equity_curve[-1] = capital
            position_qty = 0.0

        # ── Metrics ───────────────────────────────────────────────────────────
        returns_arr = np.array(daily_returns)
        total_return = (capital - starting_capital) / starting_capital

        # Sharpe (annualized, risk-free = 0 for simplicity)
        if returns_arr.std() > 0:
            sharpe = float(returns_arr.mean() / returns_arr.std() * np.sqrt(252))
        else:
            sharpe = 0.0

        # Sortino (downside deviation)
        downside = returns_arr[returns_arr < 0]
        if len(downside) > 0 and downside.std() > 0:
            sortino = float(returns_arr.mean() / downside.std() * np.sqrt(252))
        else:
            sortino = 0.0

        # Max drawdown
        equity_arr = np.array(equity_curve)
        running_max = np.maximum.accumulate(equity_arr)
        drawdowns = (equity_arr - running_max) / running_max
        max_drawdown = float(abs(drawdowns.min())) if len(drawdowns) > 0 else 0.0

        # CAGR (annualized from calendar days)
        days_in_backtest = (df.index[-1] - df.index[0]).days
        if days_in_backtest > 0:
            cagr = float((1 + total_return) ** (365 / days_in_backtest) - 1)
        else:
            cagr = 0.0

        # Win rate and avg win/loss ratio
        closed_trades = [t for t in trades if "pnl" in t]
        if closed_trades:
            wins = [t["pnl"] for t in closed_trades if t["pnl"] > 0]
            losses = [abs(t["pnl"]) for t in closed_trades if t["pnl"] <= 0]
            win_rate = len(wins) / len(closed_trades)
            avg_win = float(np.mean(wins)) if wins else 0.0
            avg_loss = float(np.mean(losses)) if losses else 1.0
            avg_win_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0.0
        else:
            win_rate = 0.0
            avg_win_loss_ratio = 0.0

        notes = []
        if dev_mode:
            notes.append(
                f"DEV MODE: only {len(df)} bars available. "
                f"Results not suitable for live trading decisions. "
                f"Minimum 252 bars required."
            )
        if not closed_trades:
            notes.append("No closed trades — insufficient crossover signals in this window.")

        result = BacktestResult(
            strategy_id="momentum_v1",
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            num_trades=len(closed_trades),
            sharpe_ratio=round(sharpe, 4),
            sortino_ratio=round(sortino, 4),
            max_drawdown=round(max_drawdown, 4),
            cagr=round(cagr, 4),
            win_rate=round(win_rate, 4),
            avg_win_loss_ratio=round(avg_win_loss_ratio, 4),
            total_return=round(total_return, 4),
            notes=notes,
        )

        logger.info(
            "Backtest complete: %s | %s",
            symbol,
            result.summary(),
        )
        self._print_trade_log(symbol, trades, starting_capital, capital)
        return result

    @staticmethod
    def _print_trade_log(
        symbol: str,
        trades: list[dict],
        starting_capital: float,
        ending_capital: float,
    ) -> None:
        if not trades:
            logger.info("%s: no trades executed.", symbol)
            return

        print(f"\n  Trade log — {symbol}")
        print(f"  {'Date':<14} {'Side':<18} {'Qty':>8} {'Price':>10} {'Comm':>8} {'PnL':>10}")
        print("  " + "-" * 72)
        for t in trades:
            pnl_str = f"${t['pnl']:+.2f}" if "pnl" in t else ""
            print(
                f"  {t['date']:<14} {t['side']:<18} {t['qty']:>8.4f} "
                f"${t['price']:>9.4f} ${t['commission']:>6.2f} {pnl_str:>10}"
            )
        total_pnl = ending_capital - starting_capital
        print(f"\n  Net P&L: ${total_pnl:+.2f}  |  Starting: ${starting_capital:,.0f}  |  Ending: ${ending_capital:,.2f}")

    # ── Walk-forward backtester ───────────────────────────────────────────────

    def walk_forward(
        self,
        symbol: str,
        df: pd.DataFrame,
        strategy: MomentumStrategy,
        starting_capital: float = 100_000.0,
        position_pct: float = 0.02,
        regime_df: Optional[pd.DataFrame] = None,
    ) -> WalkForwardSummary:
        """Run a rolling walk-forward backtest.

        Slides an IS/OOS window across the full dataset:
          - in-sample (IS):     config.in_sample_days  (default 252)
          - out-of-sample (OOS): config.out_of_sample_days (default 63)
          - step:               config.step_days (default 21)

        Signals are generated on IS+OOS data combined (rolling MAs prevent
        any lookahead), then evaluated only on the OOS period.

        Args:
            symbol:           Ticker symbol.
            df:               Full OHLCV DataFrame (ascending).
            strategy:         MomentumStrategy instance.
            starting_capital: Initial capital for each OOS window.
            position_pct:     Fraction of capital per trade.

        Returns:
            WalkForwardSummary with per-window and aggregate metrics.
        """
        is_days = self.config.in_sample_days
        oos_days = self.config.out_of_sample_days
        step = self.config.step_days
        min_bars = is_days + oos_days

        summary = WalkForwardSummary(strategy_id="momentum_v1", symbol=symbol)

        if len(df) < min_bars:
            summary.notes.append(
                f"Insufficient data: {len(df)} bars < {min_bars} required "
                f"(IS={is_days} + OOS={oos_days})."
            )
            return summary

        all_oos_returns: list[float] = []         # all windows (for drawdown / equity)
        trading_oos_returns: list[float] = []    # windows with ≥1 trade (for Sharpe)
        all_oos_equity: list[float] = []
        all_trades: list[dict] = []
        window_idx = 0

        i = 0
        while i + min_bars <= len(df):
            window_df = df.iloc[i : i + min_bars]
            oos_df = df.iloc[i + is_days : i + min_bars]

            # Generate signals on the combined window (rolling MA, no lookahead).
            # Pass regime_df for bar-by-bar regime filtering; None = no filter.
            try:
                all_signals = strategy.generate_signals_series(
                    symbol, window_df, regime_df=regime_df
                )
            except ValueError:
                i += step
                continue

            # Slice only the OOS signals by matching timestamps
            oos_signals = all_signals.loc[all_signals.index.isin(oos_df.index)]
            if oos_signals.empty:
                i += step
                continue

            # Simulate trades on the OOS period
            oos_result = self._simulate_on_slice(
                oos_df, oos_signals, starting_capital, position_pct,
                trailing_stop=self.config.trailing_stop,
                trailing_atr_mult=self.config.trailing_stop_atr_mult,
            )

            returns_arr = np.array(oos_result["daily_returns"])
            equity_arr = np.array(oos_result["equity_curve"])

            sharpe = (
                float(returns_arr.mean() / returns_arr.std() * np.sqrt(252))
                if len(returns_arr) > 1 and returns_arr.std() > 0 else 0.0
            )

            running_max = np.maximum.accumulate(equity_arr)
            drawdowns = (equity_arr - running_max) / running_max
            max_dd = float(abs(drawdowns.min())) if len(drawdowns) > 0 else 0.0

            total_return = (oos_result["capital"] - starting_capital) / starting_capital

            closed = [t for t in oos_result["trades"] if "pnl" in t]
            wins = [t["pnl"] for t in closed if t["pnl"] > 0]
            win_rate = len(wins) / len(closed) if closed else 0.0

            window = WalkForwardWindow(
                window_index=window_idx,
                is_start=str(window_df.index[0].date()),
                is_end=str(window_df.index[is_days - 1].date()),
                oos_start=str(oos_df.index[0].date()),
                oos_end=str(oos_df.index[-1].date()),
                oos_num_trades=len(closed),
                oos_sharpe=round(sharpe, 4),
                oos_max_drawdown=round(max_dd, 4),
                oos_total_return=round(total_return, 4),
                oos_win_rate=round(win_rate, 4),
            )
            summary.windows.append(window)
            all_oos_returns.extend(oos_result["daily_returns"])
            if len(closed) > 0:
                trading_oos_returns.extend(oos_result["daily_returns"])
            all_oos_equity.extend(oos_result["equity_curve"])
            all_trades.extend(oos_result["trades"])

            window_idx += 1
            i += step

        # ── Aggregate across all OOS periods ─────────────────────────────────
        # Sharpe is computed only from windows that had at least one trade.
        # 0-trade windows (cash preservation) correctly don't contribute to the
        # return distribution — including them would unfairly dilute the Sharpe
        # of an active signal.
        if all_oos_returns:
            sharpe_arr = np.array(trading_oos_returns) if trading_oos_returns else np.array(all_oos_returns)
            arr = np.array(all_oos_returns)
            summary.aggregate_sharpe = round(
                float(sharpe_arr.mean() / sharpe_arr.std() * np.sqrt(252))
                if sharpe_arr.std() > 0 else 0.0, 4
            )
            eq_arr = np.array(all_oos_equity)
            run_max = np.maximum.accumulate(eq_arr)
            dds = (eq_arr - run_max) / run_max
            summary.aggregate_max_drawdown = round(float(abs(dds.min())), 4)
            summary.aggregate_total_return = round(
                sum(w.oos_total_return for w in summary.windows) / len(summary.windows), 4
            )
            closed_all = [t for t in all_trades if "pnl" in t]
            wins_all = [t["pnl"] for t in closed_all if t["pnl"] > 0]
            summary.aggregate_win_rate = round(
                len(wins_all) / len(closed_all) if closed_all else 0.0, 4
            )
            summary.total_oos_trades = len(closed_all)
            summary.windows_passing_gate = sum(1 for w in summary.windows if w.passes_gate())

        return summary

    @staticmethod
    def _simulate_on_slice(
        price_df: pd.DataFrame,
        signals: pd.DataFrame,
        starting_capital: float,
        position_pct: float,
        trailing_stop: bool = False,
        trailing_atr_mult: float = 2.0,
    ) -> dict:
        """Simulate trades for a single OOS slice. Returns raw metrics dict.

        Trailing stop (when trailing_stop=True):
          - Distance fixed at entry: trail_distance = trailing_atr_mult × ATR(14)
          - High watermark ratchets up bar-by-bar; stop = watermark − trail_distance
          - Stop only moves up (one-way ratchet for longs)
          - Fires a synthetic SELL when close ≤ trail_stop
          - Falls back to 2 % of entry price when ATR is unavailable
        """
        capital = starting_capital
        position_qty = 0.0
        position_cost = 0.0
        equity_curve: list[float] = [capital]
        daily_returns: list[float] = []
        trades: list[dict] = []
        prev_mtm = starting_capital

        # Pre-compute ATR series for the full price slice (used at BUY entry)
        atr_series: Optional[pd.Series] = (
            _compute_atr_series(price_df) if trailing_stop else None
        )

        # Pre-extract arrays: eliminates per-iteration pandas .loc overhead.
        # Signals cover a subset of price_df (after MA warmup); align both to
        # price_df.index so every bar has a direction/score/close/atr value.
        sig_dir   = signals["direction"].reindex(price_df.index, fill_value="HOLD")
        sig_score = signals["score"].reindex(price_df.index, fill_value=0.0)
        closes    = price_df["close"].to_numpy(dtype=float)
        dirs      = sig_dir.to_numpy()
        scores    = sig_score.to_numpy(dtype=float)
        atr_vals  = (
            atr_series.reindex(price_df.index).to_numpy(dtype=float)
            if atr_series is not None else None
        )

        # Per-position trailing stop state (reset on each new BUY)
        trail_distance: float = 0.0   # fixed at entry bar's ATR × mult
        trail_high: float = 0.0       # running high watermark since entry
        trail_stop: float = 0.0       # current stop price (ratchets up only)

        for i, ts in enumerate(price_df.index):
            price     = closes[i]
            direction = str(dirs[i])
            score     = scores[i]

            # ── Update trailing stop ratchet ───────────────────────────────
            # Must happen before signal processing so the stop reflects the
            # current bar's price before we decide whether to sell.
            stop_triggered = False
            if trailing_stop and position_qty > 0 and trail_distance > 0:
                trail_high = max(trail_high, price)
                new_stop = trail_high - trail_distance
                trail_stop = max(trail_stop, new_stop)  # one-way ratchet
                if price <= trail_stop:
                    stop_triggered = True

            # Trailing stop overrides any signal
            if stop_triggered:
                direction = "SELL"

            # ── Execute trade ──────────────────────────────────────────────
            if direction == "BUY" and position_qty == 0:
                notional = capital * position_pct
                qty = notional / price
                fill_price = _apply_slippage(price, is_buy=True)
                commission = _calc_commission(qty, fill_price)
                cost = qty * fill_price + commission
                if cost <= capital:
                    capital -= cost
                    position_qty = qty
                    position_cost = qty * fill_price

                    # Initialise trailing stop for this position
                    trail_high = fill_price
                    atr_raw = float("nan")
                    if atr_vals is not None:
                        v = atr_vals[i]
                        if not math.isnan(v) and v > 0:
                            atr_raw = v
                    trail_distance = (
                        trailing_atr_mult * atr_raw
                        if not math.isnan(atr_raw)
                        else fill_price * 0.02   # 2% fallback when ATR unavailable
                    )
                    trail_stop = fill_price - trail_distance

                    trades.append({
                        "date": str(ts.date()), "side": "BUY",
                        "qty": qty, "price": fill_price,
                        "commission": commission, "score": score,
                        "trail_stop_initial": round(trail_stop, 5),
                    })

            elif direction == "SELL" and position_qty > 0:
                fill_price = _apply_slippage(price, is_buy=False)
                commission = _calc_commission(position_qty, fill_price)
                proceeds = position_qty * fill_price - commission
                pnl = proceeds - position_cost
                capital += proceeds
                side_label = "SELL (trail)" if stop_triggered else "SELL"
                trades.append({
                    "date": str(ts.date()), "side": side_label,
                    "qty": position_qty, "price": fill_price,
                    "commission": commission, "pnl": pnl, "score": score,
                })
                # Reset position state
                position_qty = 0.0
                position_cost = 0.0
                trail_distance = 0.0
                trail_high = 0.0
                trail_stop = 0.0

            mtm = capital + position_qty * price
            equity_curve.append(mtm)
            daily_returns.append(
                (mtm - prev_mtm) / prev_mtm if prev_mtm > 0 else 0.0
            )
            prev_mtm = mtm

        # Close any open position at the last bar's close
        if position_qty > 0 and not price_df.empty:
            last_price = float(price_df["close"].iloc[-1])
            fill_price = _apply_slippage(last_price, is_buy=False)
            commission = _calc_commission(position_qty, fill_price)
            proceeds = position_qty * fill_price - commission
            capital += proceeds
            trades.append({
                "date": str(price_df.index[-1].date()), "side": "SELL (EOD)",
                "qty": position_qty, "price": fill_price,
                "commission": commission, "pnl": proceeds - position_cost, "score": 0.0,
            })

        return {
            "capital": capital,
            "daily_returns": daily_returns,
            "equity_curve": equity_curve,
            "trades": trades,
        }
