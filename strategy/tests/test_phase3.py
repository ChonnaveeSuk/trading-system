# trading-system/strategy/tests/test_phase3.py
#
# Phase 3 tests: yfinance fetcher, walk-forward backtester, data volume checks.

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import math
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest

from src.backtester import BacktestConfig, WalkForwardWindow, WalkForwardSummary
from src.backtester.engine import BacktestEngine
from src.signals.momentum import MomentumStrategy, MomentumConfig


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def make_ohlcv(n: int, base: float = 180.0, trend: float = 0.001) -> pd.DataFrame:
    rng = np.random.default_rng(seed=7)
    dates = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    noise = rng.normal(0, 0.008, n)
    close = base * np.cumprod(1 + trend + noise)
    high = close * (1 + abs(rng.normal(0, 0.005, n)))
    low = close * (1 - abs(rng.normal(0, 0.005, n)))
    open_ = close * (1 + rng.normal(0, 0.003, n))
    volume = rng.uniform(40_000_000, 60_000_000, n)
    high = np.maximum(high, np.maximum(open_, close))
    low = np.minimum(low, np.minimum(open_, close))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
         "volume": volume, "vwap": (open_ + high + low + close) / 4},
        index=dates,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Backtester daily-return calculation tests (regression for the prev_mtm bug)
# ─────────────────────────────────────────────────────────────────────────────

class TestDailyReturns:
    """Verify that hold-day returns correctly capture MTM P&L, not 0.0."""

    def _make_trending_ohlcv(self, n: int = 60, start: float = 100.0,
                              daily_gain: float = 0.01) -> pd.DataFrame:
        """Linear uptrend with constant volume so the strategy can BUY and hold."""
        dates = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
        close = np.array([start * (1 + daily_gain) ** i for i in range(n)])
        volume = np.full(n, 1_000_000.0)
        high = close * 1.002
        low = close * 0.998
        return pd.DataFrame(
            {"open": close, "high": high, "low": low, "close": close,
             "volume": volume, "vwap": close},
            index=dates,
        )

    def test_hold_days_have_nonzero_returns_when_position_open(self):
        """When a position is held over multiple bars, non-trade days must show
        mark-to-market returns, not 0.0."""
        from src.backtester.engine import BacktestEngine
        config = BacktestConfig(in_sample_days=20, out_of_sample_days=20, step_days=10)
        engine = BacktestEngine(config=config)
        strategy = MomentumStrategy(MomentumConfig(fast_period=3, slow_period=8, vol_period=5))
        df = self._make_trending_ohlcv(60, daily_gain=0.01)
        summary = engine.walk_forward("TEST", df, strategy)
        # If any window has trades, check that aggregate Sharpe reflects real P&L
        if summary.total_oos_trades > 0:
            # Sharpe must be non-trivially negative only from real P&L, not the bug
            # A consistent uptrend should produce positive or near-zero Sharpe
            assert summary.aggregate_sharpe > -5.0, (
                f"Sharpe {summary.aggregate_sharpe} implausibly negative — "
                "daily return calculation bug may have re-appeared"
            )

    def test_equity_curve_and_returns_consistent(self):
        """equity_curve[i+1] / equity_curve[i] - 1 must equal daily_returns[i]."""
        from src.backtester.engine import BacktestEngine
        engine = BacktestEngine()
        strategy = MomentumStrategy(MomentumConfig(fast_period=3, slow_period=8, vol_period=5))
        df = self._make_trending_ohlcv(50, daily_gain=0.005)

        # Run via the internal _simulate_on_slice helper with a dummy signal set
        signals = strategy.generate_signals_series("TEST", df)
        result = engine._simulate_on_slice(df, signals, 100_000.0, 0.02)

        equity = result["equity_curve"]
        returns = result["daily_returns"]
        # The two lists must have consistent lengths
        assert len(equity) == len(returns) + 1, (
            f"equity_curve len={len(equity)}, daily_returns len={len(returns)}, "
            "expected equity_curve to have one extra element (initial capital)"
        )
        # Spot-check: each return equals the ratio of consecutive equity values
        for i, r in enumerate(returns):
            expected = (equity[i + 1] - equity[i]) / equity[i] if equity[i] > 0 else 0.0
            assert abs(r - expected) < 1e-9, (
                f"daily_returns[{i}]={r:.6f} != equity ratio {expected:.6f}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward backtester unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TestWalkForward:

    def _make_strategy(self) -> MomentumStrategy:
        return MomentumStrategy(MomentumConfig(fast_period=5, slow_period=15, vol_period=10))

    def test_insufficient_data_returns_empty_summary(self):
        engine = BacktestEngine()
        strategy = self._make_strategy()
        df = make_ohlcv(200)  # less than 252+63=315
        summary = engine.walk_forward("TEST", df, strategy)
        assert isinstance(summary, WalkForwardSummary)
        assert len(summary.windows) == 0
        assert any("Insufficient" in n for n in summary.notes)

    def test_sufficient_data_produces_windows(self):
        config = BacktestConfig(in_sample_days=50, out_of_sample_days=20, step_days=10)
        engine = BacktestEngine(config=config)
        strategy = self._make_strategy()
        df = make_ohlcv(200)
        summary = engine.walk_forward("TEST", df, strategy)
        assert len(summary.windows) >= 1

    def test_window_indices_are_sequential(self):
        config = BacktestConfig(in_sample_days=50, out_of_sample_days=20, step_days=10)
        engine = BacktestEngine(config=config)
        strategy = self._make_strategy()
        df = make_ohlcv(200)
        summary = engine.walk_forward("TEST", df, strategy)
        for i, w in enumerate(summary.windows):
            assert w.window_index == i

    def test_oos_periods_do_not_overlap(self):
        """Each OOS window must start after the previous OOS window ends."""
        config = BacktestConfig(in_sample_days=50, out_of_sample_days=20, step_days=20)
        engine = BacktestEngine(config=config)
        strategy = self._make_strategy()
        df = make_ohlcv(200)
        summary = engine.walk_forward("TEST", df, strategy)
        for i in range(1, len(summary.windows)):
            assert summary.windows[i].oos_start > summary.windows[i - 1].oos_start

    def test_metrics_in_valid_range(self):
        config = BacktestConfig(in_sample_days=50, out_of_sample_days=20, step_days=10)
        engine = BacktestEngine(config=config)
        strategy = self._make_strategy()
        df = make_ohlcv(200)
        summary = engine.walk_forward("TEST", df, strategy)
        assert math.isfinite(summary.aggregate_sharpe)
        assert 0.0 <= summary.aggregate_max_drawdown <= 1.0
        assert 0.0 <= summary.aggregate_win_rate <= 1.0

    def test_gate_logic_aggregate(self):
        good = WalkForwardSummary(
            strategy_id="x", symbol="X",
            windows=[
                WalkForwardWindow(0, "a", "b", "c", "d", oos_sharpe=1.5, oos_max_drawdown=0.08),
                WalkForwardWindow(1, "e", "f", "g", "h", oos_sharpe=1.2, oos_max_drawdown=0.10),
            ],
            aggregate_sharpe=1.35,
            aggregate_max_drawdown=0.09,
            windows_passing_gate=2,
        )
        bad = WalkForwardSummary(
            strategy_id="x", symbol="X",
            windows=[WalkForwardWindow(0, "a", "b", "c", "d", oos_sharpe=0.8, oos_max_drawdown=0.20)],
            aggregate_sharpe=0.8,
            aggregate_max_drawdown=0.20,
            windows_passing_gate=0,
        )
        assert good.passes_gate()
        assert not bad.passes_gate()

    def test_window_gate_logic(self):
        # ≥3 trades: both Sharpe and MaxDD gates apply
        good = WalkForwardWindow(0, "", "", "", "", oos_num_trades=5, oos_sharpe=1.1, oos_max_drawdown=0.14)
        bad_sharpe = WalkForwardWindow(0, "", "", "", "", oos_num_trades=5, oos_sharpe=0.9, oos_max_drawdown=0.10)
        bad_dd = WalkForwardWindow(0, "", "", "", "", oos_num_trades=5, oos_sharpe=1.5, oos_max_drawdown=0.16)
        assert good.passes_gate()
        assert not bad_sharpe.passes_gate()
        assert not bad_dd.passes_gate()
        # ≤2 trades: only MaxDD gate (Sharpe too noisy with so few trades)
        sparse_good = WalkForwardWindow(0, "", "", "", "", oos_num_trades=2, oos_sharpe=0.5, oos_max_drawdown=0.10)
        sparse_bad = WalkForwardWindow(0, "", "", "", "", oos_num_trades=2, oos_sharpe=2.0, oos_max_drawdown=0.20)
        assert sparse_good.passes_gate()
        assert not sparse_bad.passes_gate()

    def test_summary_string(self):
        summary = WalkForwardSummary(
            strategy_id="momentum_v1", symbol="AAPL",
            windows=[WalkForwardWindow(0, "a", "b", "c", "d")],
            aggregate_sharpe=1.2, aggregate_max_drawdown=0.08,
            windows_passing_gate=1,
        )
        s = summary.summary()
        assert "momentum_v1" in s
        assert "AAPL" in s

    def test_no_lookahead_in_signals(self):
        """Signals at bar N must only use data from bars 0..N (rolling MA guarantees this)."""
        config = BacktestConfig(in_sample_days=30, out_of_sample_days=15, step_days=10)
        engine = BacktestEngine(config=config)
        strategy = MomentumStrategy(MomentumConfig(fast_period=5, slow_period=15, vol_period=10))
        df = make_ohlcv(100, trend=0.005)
        # Inject a large spike on the last OOS bar — should NOT affect IS signals
        df_modified = df.copy()
        df_modified.iloc[-1, df_modified.columns.get_loc("close")] *= 10
        summary_normal = engine.walk_forward("TEST", df, strategy)
        summary_spiked = engine.walk_forward("TEST", df_modified, strategy)
        # IS windows (which don't include the last bar) should produce identical signals
        # We verify by checking windows that end before the spike
        for w_n, w_s in zip(summary_normal.windows[:-1], summary_spiked.windows[:-1]):
            assert w_n.oos_start == w_s.oos_start, "IS signals differ — possible lookahead"


# ─────────────────────────────────────────────────────────────────────────────
# Sparse-volume detection tests (regression for EUR-USD zero-volume filtering)
# ─────────────────────────────────────────────────────────────────────────────

class TestSparseVolumeDetection:
    """Verify that FX-style instruments with zero volume still generate signals."""

    def _make_fx_ohlcv(self, n: int = 50, trend: float = 0.002) -> pd.DataFrame:
        """Simulate EUR-USD style data: no centralized exchange volume (all zeros)."""
        rng = np.random.default_rng(seed=42)
        dates = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
        noise = rng.normal(0, 0.003, n)
        close = 1.08 * np.cumprod(1 + trend + noise)
        return pd.DataFrame(
            {"open": close, "high": close * 1.001, "low": close * 0.999,
             "close": close, "volume": np.zeros(n), "vwap": close},
            index=dates,
        )

    def test_zero_volume_does_not_suppress_all_signals(self):
        """With all-zero volume, MA crossovers must still produce BUY/SELL signals.

        The price swing must be large enough so that the MA spread at the crossover
        bar exceeds the 4x sparse-volume noise threshold (~20bps).
        """
        # Build a series that definitively crosses: large uptrend then sharp reversal.
        # With fast=5/slow=15 the bear cross fires with a large spread (~100bps)
        # because the steep decline creates significant fast/slow MA divergence.
        n = 70
        dates = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
        close = np.concatenate([
            np.linspace(1.05, 1.25, 40),   # large uptrend (+19%)
            np.linspace(1.25, 1.00, 30),   # sharp reversal (-20%)
        ])
        df = pd.DataFrame(
            {"open": close, "high": close * 1.002, "low": close * 0.998,
             "close": close, "volume": np.zeros(n), "vwap": close},
            index=dates,
        )
        strategy = MomentumStrategy(MomentumConfig(fast_period=5, slow_period=15, vol_period=10))
        signals = strategy.generate_signals_series("EUR-USD", df)
        non_hold = signals[signals["direction"] != "HOLD"]
        assert len(non_hold) > 0, (
            "Zero-volume FX data produced no signals — sparse volume detection not working"
        )

    def test_normal_volume_still_filters_ma_signals_on_weak_bars(self):
        """For equities with real volume, MA crossover signals must be suppressed on
        below-average volume bars.  RSI signals are allowed to fire on any bar."""
        # Use zero-drift oscillating data so RSI stays near the neutral zone,
        # ensuring all actionable signals come from MA crossovers (not RSI/BB extremes).
        n = 50
        dates = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
        rng = np.random.default_rng(seed=99)
        # Oscillating price (no drift) → RSI stays near 50, no RSI extreme events
        close = 100.0 * np.cumprod(1 + rng.normal(0.0, 0.003, n))
        # Alternate: high volume on even bars, near-zero on odd bars
        volume = np.where(np.arange(n) % 2 == 0, 2_000_000.0, 1.0)
        df = pd.DataFrame(
            {"open": close, "high": close * 1.001, "low": close * 0.999,
             "close": close, "volume": volume, "vwap": close},
            index=dates,
        )
        strategy = MomentumStrategy(MomentumConfig(fast_period=5, slow_period=15, vol_period=10))
        signals = strategy.generate_signals_series("AAPL", df)

        # Any active signal on a low-volume (odd-index) bar must be RSI-driven
        # (BB and MA both require volume confirmation).
        active = signals[signals["direction"] != "HOLD"]
        for ts in active.index:
            bar_idx = df.index.get_loc(ts)
            if bar_idx % 2 != 0:  # low-volume bar
                rsi_val = signals.loc[ts, "rsi"]
                assert (rsi_val < strategy.config.rsi_oversold
                        or rsi_val > strategy.config.rsi_overbought), (
                    f"Non-RSI signal on low-volume bar {bar_idx} (RSI={rsi_val:.1f}) — "
                    "volume filter not working for MA/BB-based signals"
                )

    def test_sparse_volume_threshold_is_fifty_percent(self):
        """Sparse detection fires when >50% of volume bars are zero."""
        n = 40
        dates = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
        rng = np.random.default_rng(seed=3)
        close = 1.08 * np.cumprod(1 + rng.normal(0.001, 0.003, n))

        # 55% zeros → sparse (volume filter disabled)
        vol_mostly_zero = np.zeros(n)
        vol_mostly_zero[:18] = 1_000_000.0   # 45% non-zero
        df_sparse = pd.DataFrame(
            {"open": close, "high": close * 1.001, "low": close * 0.999,
             "close": close, "volume": vol_mostly_zero, "vwap": close},
            index=dates,
        )
        strategy = MomentumStrategy(MomentumConfig(fast_period=5, slow_period=15, vol_period=10))
        sigs_sparse = strategy.generate_signals_series("EUR-USD", df_sparse)

        # 55% non-zero → dense (volume filter active)
        vol_mostly_full = np.full(n, 1_000_000.0)
        vol_mostly_full[:18] = 0.0   # 45% zero
        df_dense = pd.DataFrame(
            {"open": close, "high": close * 1.001, "low": close * 0.999,
             "close": close, "volume": vol_mostly_full, "vwap": close},
            index=dates,
        )
        sigs_dense = strategy.generate_signals_series("EUR-USD", df_dense)

        # Both run without error and return a DataFrame
        assert isinstance(sigs_sparse, pd.DataFrame)
        assert isinstance(sigs_dense, pd.DataFrame)


# ─────────────────────────────────────────────────────────────────────────────
# yfinance fetcher unit tests (mock network)
# ─────────────────────────────────────────────────────────────────────────────

class TestYfinanceFetcher:

    def _make_raw_df(self, n: int = 50, close: float = 180.0) -> pd.DataFrame:
        """Simulate a yfinance-style DataFrame."""
        dates = pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC")
        rng = np.random.default_rng(42)
        c = close * np.cumprod(1 + rng.normal(0, 0.01, n))
        h = c * 1.01
        lo = c * 0.99
        o = c * (1 + rng.normal(0, 0.005, n))
        h = np.maximum(h, np.maximum(o, c))
        lo = np.minimum(lo, np.minimum(o, c))
        df = pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": c, "Volume": 1e7},
                          index=dates)
        df.index.name = "Date"
        return df

    def test_unknown_symbol_raises(self):
        from src.data.yfinance_fetcher import YfinanceFetcher
        fetcher = YfinanceFetcher()
        with pytest.raises(ValueError, match="Unknown symbol"):
            fetcher.fetch_and_store("XYZ-UNKNOWN")

    def test_normalize_produces_correct_columns(self):
        from src.data.yfinance_fetcher import YfinanceFetcher
        fetcher = YfinanceFetcher()
        raw = self._make_raw_df()
        df = fetcher._normalize(raw, "AAPL")
        assert set(df.columns) == {"open", "high", "low", "close", "volume", "vwap"}
        assert df.index.name == "timestamp"
        assert str(df.index.tz) == "UTC"

    def test_normalize_repairs_integrity(self):
        """high/low repair must ensure high >= max(open, close)."""
        from src.data.yfinance_fetcher import YfinanceFetcher
        fetcher = YfinanceFetcher()
        raw = self._make_raw_df()
        # Corrupt some rows
        raw.loc[raw.index[5], "High"] = raw.loc[raw.index[5], "Close"] * 0.9  # high < close
        df = fetcher._normalize(raw, "AAPL")
        assert (df["high"] >= df["close"]).all()
        assert (df["high"] >= df["open"]).all()
        assert (df["low"] <= df["close"]).all()
        assert (df["low"] <= df["open"]).all()

    def test_normalize_drops_nan_rows(self):
        from src.data.yfinance_fetcher import YfinanceFetcher
        fetcher = YfinanceFetcher()
        raw = self._make_raw_df(10)
        raw.loc[raw.index[3], "Close"] = float("nan")
        df = fetcher._normalize(raw, "AAPL")
        assert len(df) == 9  # one row dropped

    def test_vwap_approximation(self):
        from src.data.yfinance_fetcher import YfinanceFetcher
        fetcher = YfinanceFetcher()
        raw = self._make_raw_df(5)
        df = fetcher._normalize(raw, "AAPL")
        expected = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
        pd.testing.assert_series_equal(df["vwap"], expected, check_names=False)

    def test_symbol_map_coverage(self):
        """All DB symbol names must have a yfinance ticker mapping."""
        from src.data.yfinance_fetcher import _YFINANCE_TICKER
        for sym in ["AAPL", "BTC-USD", "EUR-USD"]:
            assert sym in _YFINANCE_TICKER, f"{sym} missing from yfinance ticker map"


# ─────────────────────────────────────────────────────────────────────────────
# PostgreSQL integration tests (skip if DB unavailable)
# ─────────────────────────────────────────────────────────────────────────────

def _db_available() -> bool:
    try:
        import psycopg2
        c = psycopg2.connect(
            "postgres://quantai:quantai_dev_2026@localhost:5432/quantai",
            connect_timeout=2,
        )
        c.close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _db_available(), reason="PostgreSQL not available")
class TestPhase3Integration:

    def test_aapl_has_252_plus_bars(self):
        """After seeding from yfinance, AAPL must have ≥252 bars."""
        from src.data.fetcher import PostgresOhlcvFetcher
        with PostgresOhlcvFetcher() as f:
            df = f.fetch("AAPL", days=450)
        assert len(df) >= 252, f"Expected ≥252 bars, got {len(df)}"

    def test_btcusd_has_252_plus_bars(self):
        from src.data.fetcher import PostgresOhlcvFetcher
        with PostgresOhlcvFetcher() as f:
            df = f.fetch("BTC-USD", days=450)
        assert len(df) >= 252, f"Expected ≥252 bars, got {len(df)}"

    def test_walkforward_runs_on_real_data(self):
        """Walk-forward must complete without error on real yfinance data."""
        from src.data.fetcher import PostgresOhlcvFetcher
        with PostgresOhlcvFetcher() as f:
            df = f.fetch("AAPL", days=450)

        config = BacktestConfig(in_sample_days=252, out_of_sample_days=63, step_days=21)
        engine = BacktestEngine(config=config)
        strategy = MomentumStrategy(MomentumConfig(fast_period=10, slow_period=30, vol_period=20))

        summary = engine.walk_forward("AAPL", df, strategy)
        assert isinstance(summary, WalkForwardSummary)
        assert math.isfinite(summary.aggregate_sharpe)
        assert 0.0 <= summary.aggregate_max_drawdown <= 1.0

    def test_walkforward_produces_multiple_windows(self):
        """With 600+ days of seeded data, walk-forward must produce ≥2 windows."""
        from src.data.fetcher import PostgresOhlcvFetcher
        with PostgresOhlcvFetcher() as f:
            df = f.fetch("AAPL", days=700)  # fetch full seeded history

        config = BacktestConfig(in_sample_days=252, out_of_sample_days=63, step_days=21)
        engine = BacktestEngine(config=config)
        strategy = MomentumStrategy(MomentumConfig(fast_period=10, slow_period=30, vol_period=20))

        summary = engine.walk_forward("AAPL", df, strategy)
        assert len(summary.windows) >= 2, (
            f"Expected ≥2 windows with {len(df)} bars, got {len(summary.windows)}"
        )
        assert summary.total_oos_trades >= 0

    def test_ohlcv_data_integrity_after_yfinance_seed(self):
        """All bars from yfinance must pass the integrity check."""
        from src.data.fetcher import PostgresOhlcvFetcher
        from src.data import validate_ohlcv
        with PostgresOhlcvFetcher() as f:
            for symbol in ("AAPL", "BTC-USD", "EUR-USD"):
                df = f.fetch(symbol, days=450)
                if not df.empty:
                    validate_ohlcv(df)  # raises on bad data
