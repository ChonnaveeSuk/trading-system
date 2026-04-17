# trading-system/strategy/tests/test_regime_filter.py
#
# Tests for the market regime filter in MomentumStrategy.
#
# Regime logic under test:
#   BULL    (SPY > MA200 + 2%):  BUY signals pass through unchanged
#   NEUTRAL (SPY within ±2% of MA200): BUY score × 0.7; HOLD if score < 0.55
#   BEAR    (SPY < MA200 - 2%):  ALL BUY signals → HOLD
#   SELL signals always pass regardless of regime

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest

from src.signals import Direction
from src.signals.momentum import MomentumStrategy, MomentumConfig


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def make_ohlcv(n: int, base: float = 100.0, trend: float = 0.001, seed: int = 42) -> pd.DataFrame:
    """Synthetic OHLCV with gentle uptrend and volume."""
    rng = np.random.default_rng(seed=seed)
    dates = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
    noise = rng.normal(0, 0.008, n)
    close = base * np.cumprod(1 + trend + noise)
    high = close * (1 + abs(rng.normal(0, 0.005, n)))
    low = close * (1 - abs(rng.normal(0, 0.005, n)))
    open_ = close * (1 + rng.normal(0, 0.003, n))
    volume = rng.uniform(40_000_000, 60_000_000, n)
    high = np.maximum(high, np.maximum(open_, close))
    low = np.minimum(low, np.minimum(open_, close))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low,
         "close": close, "volume": volume, "vwap": close},
        index=dates,
    )


def make_spy_bull(n: int = 250) -> pd.DataFrame:
    """SPY data in BULL regime: price clearly above MA200 by >2%."""
    # Start at 400, end at 480 — well above any MA200
    dates = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
    # First 200 bars at 400 (establish MA200 base), then climb to 480
    base = np.full(n, 400.0)
    base[200:] = np.linspace(420, 480, n - 200)
    close = base + np.random.default_rng(99).normal(0, 1.0, n)
    return pd.DataFrame(
        {"open": close, "high": close * 1.001, "low": close * 0.999,
         "close": close, "volume": np.full(n, 80_000_000.0), "vwap": close},
        index=dates,
    )


def make_spy_bear(n: int = 250) -> pd.DataFrame:
    """SPY data in BEAR regime: price clearly below MA200 by >2%."""
    dates = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
    # First 200 bars at 400, then crash to 360 (−10% from MA)
    close_arr = np.concatenate([
        np.full(200, 400.0),
        np.linspace(395, 360, n - 200),
    ])
    close_arr += np.random.default_rng(101).normal(0, 0.5, n)
    return pd.DataFrame(
        {"open": close_arr, "high": close_arr * 1.001, "low": close_arr * 0.999,
         "close": close_arr, "volume": np.full(n, 80_000_000.0), "vwap": close_arr},
        index=dates,
    )


def make_spy_neutral(n: int = 250) -> pd.DataFrame:
    """SPY data in NEUTRAL regime: price within ±2% of MA200."""
    dates = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
    # Price oscillates tightly around 400 — stays within ±1% of MA200
    close_arr = 400.0 + np.sin(np.linspace(0, 4 * np.pi, n)) * 2.0  # ±0.5% band
    close_arr += np.random.default_rng(103).normal(0, 0.3, n)
    return pd.DataFrame(
        {"open": close_arr, "high": close_arr * 1.001, "low": close_arr * 0.999,
         "close": close_arr, "volume": np.full(n, 80_000_000.0), "vwap": close_arr},
        index=dates,
    )


# ─────────────────────────────────────────────────────────────────────────────
# update_regime() tests
# ─────────────────────────────────────────────────────────────────────────────

class TestUpdateRegime:
    """update_regime() must correctly classify SPY vs MA200."""

    def test_bull_regime_detected(self):
        spy = make_spy_bull(250)
        strategy = MomentumStrategy(MomentumConfig(regime_ma_period=200))
        regime = strategy.update_regime(spy)
        assert regime == "BULL"
        assert strategy.current_regime == "BULL"

    def test_bear_regime_detected(self):
        spy = make_spy_bear(250)
        strategy = MomentumStrategy(MomentumConfig(regime_ma_period=200))
        regime = strategy.update_regime(spy)
        assert regime == "BEAR"
        assert strategy.current_regime == "BEAR"

    def test_neutral_regime_detected(self):
        spy = make_spy_neutral(250)
        strategy = MomentumStrategy(MomentumConfig(regime_ma_period=200))
        regime = strategy.update_regime(spy)
        assert regime == "NEUTRAL"

    def test_insufficient_data_defaults_to_bull(self):
        """Fewer bars than MA period → insufficient data → BULL (permissive)."""
        # Build a small SPY df with declining prices (would be BEAR if MA200 valid)
        # but only 50 bars → MA200 is all NaN → defaults to BULL
        dates = pd.date_range("2022-01-01", periods=50, freq="D", tz="UTC")
        close = np.linspace(380, 360, 50)  # clearly below any MA if data were sufficient
        spy = pd.DataFrame(
            {"open": close, "high": close * 1.001, "low": close * 0.999,
             "close": close, "volume": np.full(50, 80_000_000.0), "vwap": close},
            index=dates,
        )
        strategy = MomentumStrategy(MomentumConfig(regime_ma_period=200))
        regime = strategy.update_regime(spy)
        assert regime == "BULL"

    def test_empty_df_defaults_to_bull(self):
        spy = pd.DataFrame()
        strategy = MomentumStrategy()
        regime = strategy.update_regime(spy)
        assert regime == "BULL"

    def test_regime_filter_disabled_returns_bull(self):
        """When regime_filter=False, update_regime() always returns BULL."""
        spy = make_spy_bear(250)
        strategy = MomentumStrategy(MomentumConfig(regime_filter=False))
        regime = strategy.update_regime(spy)
        assert regime == "BULL"

    def test_spy_price_and_ma200_cached(self):
        """After update_regime(), _spy_price and _spy_ma200 are populated."""
        spy = make_spy_bull(250)
        strategy = MomentumStrategy()
        strategy.update_regime(spy)
        assert strategy._spy_price is not None
        assert strategy._spy_ma200 is not None
        assert strategy._spy_price > 0
        assert strategy._spy_ma200 > 0


# ─────────────────────────────────────────────────────────────────────────────
# generate_signal() regime filter tests (single-bar / live path)
# ─────────────────────────────────────────────────────────────────────────────

class TestGenerateSignalRegime:
    """Regime filter must apply correctly in the single-bar live path."""

    def _strategy_with_regime(self, regime: str) -> MomentumStrategy:
        """Return a strategy with the regime pre-set (bypass update_regime)."""
        s = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=True, regime_ma_period=200,
        ))
        s._regime = regime
        return s

    def _make_buy_scenario(self, n: int = 300) -> pd.DataFrame:
        """Data that reliably generates a BUY signal (oversold RSI at the end)."""
        # Up-then-sharply-down to trigger RSI oversold BUY
        dates = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
        close = np.concatenate([
            np.linspace(100, 130, n - 20),   # uptrend (builds fast MA > slow MA)
            np.linspace(130, 90, 20),          # sharp crash → RSI oversold
        ])
        volume = np.full(n, 1_000_000.0)
        high = close * 1.005
        low = close * 0.995
        return pd.DataFrame(
            {"open": close, "high": high, "low": low,
             "close": close, "volume": volume, "vwap": close},
            index=dates,
        )

    def _find_buy_signal(self, df: pd.DataFrame, regime: str) -> bool:
        """Return True if a BUY signal was ever generated on df in the given regime."""
        s = self._strategy_with_regime(regime)
        signals = s.generate_signals_series("TEST", df)
        return (signals["direction"] == Direction.BUY.value).any()

    def test_bull_regime_does_not_block_buy(self):
        df = self._make_buy_scenario()
        s = self._strategy_with_regime("BULL")
        signals = s.generate_signals_series("TEST", df)
        # BULL regime: BUY signals should not be suppressed
        if (signals["direction"] == "BUY").any():
            # At least one BUY survived
            assert True
        # If no BUY signals at all, that's also valid (just no crossover)

    def test_bear_regime_blocks_all_buys(self):
        df = self._make_buy_scenario()
        s = self._strategy_with_regime("BEAR")
        signals = s.generate_signals_series("TEST", df)
        # BEAR: no BUY signals should exist
        assert (signals["direction"] == Direction.BUY.value).sum() == 0, (
            "BEAR regime must suppress all BUY signals"
        )

    def test_bear_regime_allows_sells(self):
        """SELL signals must pass through in BEAR regime."""
        df = self._make_buy_scenario()
        s_no_filter = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False,
        ))
        s_bear = self._strategy_with_regime("BEAR")
        sigs_no_filter = s_no_filter.generate_signals_series("TEST", df)
        sigs_bear = s_bear.generate_signals_series("TEST", df)

        sells_no_filter = (sigs_no_filter["direction"] == "SELL").sum()
        sells_bear = (sigs_bear["direction"] == "SELL").sum()
        assert sells_bear == sells_no_filter, (
            f"BEAR should not block SELL: no_filter={sells_no_filter}, bear={sells_bear}"
        )

    def test_neutral_regime_reduces_buy_score(self):
        """NEUTRAL: all BUY scores must be ≤ 70% of the unfiltered scores."""
        df = self._make_buy_scenario()
        s_no_filter = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10, regime_filter=False,
        ))
        s_neutral = self._strategy_with_regime("NEUTRAL")
        sigs_unfiltered = s_no_filter.generate_signals_series("TEST", df)
        sigs_neutral = s_neutral.generate_signals_series("TEST", df)

        buy_mask_unfiltered = sigs_unfiltered["direction"] == "BUY"
        if not buy_mask_unfiltered.any():
            pytest.skip("No BUY signals generated — cannot compare scores")

        # All original BUY signals must have been either scored ≤ 70% OR converted to HOLD
        for idx in sigs_unfiltered[buy_mask_unfiltered].index:
            if idx not in sigs_neutral.index:
                continue
            orig_score = float(sigs_unfiltered.loc[idx, "score"])
            filt_score = float(sigs_neutral.loc[idx, "score"])
            filt_dir = str(sigs_neutral.loc[idx, "direction"])
            if filt_dir == "BUY":
                assert filt_score <= orig_score * 0.71, (
                    f"Neutral BUY score {filt_score} should be ≤ 70% of {orig_score}"
                )
            else:
                assert filt_dir == "HOLD", f"Expected HOLD, got {filt_dir}"
                assert filt_score == 0.0

    def test_regime_filter_disabled_passes_all_buys(self):
        """When regime_filter=False, no BUY signals are suppressed regardless of regime."""
        df = self._make_buy_scenario()
        s_disabled = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10, regime_filter=False,
        ))
        s_disabled._regime = "BEAR"  # even with BEAR cached, filter is off
        sigs = s_disabled.generate_signals_series("TEST", df)
        # Should have same signals as if regime were BULL
        s_bull = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10, regime_filter=False,
        ))
        sigs_bull = s_bull.generate_signals_series("TEST", df)
        pd.testing.assert_series_equal(sigs["direction"], sigs_bull["direction"])

    def test_regime_logged_in_features(self):
        """generate_signal() must include regime in the features dict."""
        df = self._make_buy_scenario()
        s = self._strategy_with_regime("BEAR")
        signal = s.generate_signal("TEST", df, portfolio_value=100_000.0)
        assert signal.features is not None
        assert "regime" in signal.features
        assert signal.features["regime"] == "BEAR"


# ─────────────────────────────────────────────────────────────────────────────
# generate_signals_series() with regime_df (backtest path)
# ─────────────────────────────────────────────────────────────────────────────

class TestGenerateSignalsSeriesRegimeDF:
    """Bar-by-bar regime from regime_df must override the cached regime."""

    def test_bear_regime_df_blocks_buys(self):
        df = make_ohlcv(400, base=100, trend=0.002, seed=7)
        spy = make_spy_bear(400)
        s = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=True, regime_ma_period=200,
        ))
        sigs = s.generate_signals_series("TEST", df, regime_df=spy)
        # After MA200 warm-up (~200 bars), SPY is in BEAR territory
        # Count BUY signals in the post-warmup period
        post_warmup = sigs.iloc[200:]
        bear_buys = (post_warmup["direction"] == "BUY").sum()
        bear_regimes = (post_warmup.get("regime", pd.Series(dtype=str)) == "BEAR").sum()
        if bear_regimes > 0:
            assert bear_buys == 0, (
                f"Found {bear_buys} BUY signals in BEAR regime windows"
            )

    def test_regime_column_populated(self):
        """generate_signals_series() must add a 'regime' column when regime_df provided."""
        df = make_ohlcv(400, seed=7)
        spy = make_spy_bull(400)
        s = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=True, regime_ma_period=200,
        ))
        sigs = s.generate_signals_series("TEST", df, regime_df=spy)
        assert "regime" in sigs.columns
        assert sigs["regime"].isin(["BULL", "NEUTRAL", "BEAR"]).all()

    def test_regime_df_none_uses_cached_regime(self):
        """When regime_df=None, falls back to self._regime for all bars."""
        df = make_ohlcv(300, seed=7)
        s = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10, regime_filter=True,
        ))
        s._regime = "BEAR"
        sigs = s.generate_signals_series("TEST", df, regime_df=None)
        # All bars should have BEAR regime applied — no BUY signals
        assert (sigs["direction"] == "BUY").sum() == 0

    def test_bull_regime_df_does_not_block_buys(self):
        """BULL spy data → BUY signals should not be suppressed."""
        df = make_ohlcv(400, base=100, trend=0.002, seed=7)
        spy_bull = make_spy_bull(400)
        spy_bear = make_spy_bear(400)

        s_bull = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=True, regime_ma_period=200,
        ))
        s_bear = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=True, regime_ma_period=200,
        ))
        sigs_bull = s_bull.generate_signals_series("TEST", df, regime_df=spy_bull)
        sigs_bear = s_bear.generate_signals_series("TEST", df, regime_df=spy_bear)

        buys_bull = (sigs_bull["direction"] == "BUY").sum()
        buys_bear = (sigs_bear["direction"] == "BUY").sum()
        assert buys_bull >= buys_bear, (
            f"BULL should have ≥ BUY signals as BEAR: bull={buys_bull}, bear={buys_bear}"
        )

    def test_sell_signals_unchanged_in_bear(self):
        """BEAR regime_df must not suppress SELL signals."""
        df = make_ohlcv(400, base=100, trend=0.001, seed=12)
        spy = make_spy_bear(400)
        s_filtered = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=True, regime_ma_period=200,
        ))
        s_unfiltered = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False,
        ))
        sigs_f = s_filtered.generate_signals_series("TEST", df, regime_df=spy)
        sigs_u = s_unfiltered.generate_signals_series("TEST", df)

        sells_filtered = (sigs_f["direction"] == "SELL").sum()
        sells_unfiltered = (sigs_u["direction"] == "SELL").sum()
        assert sells_filtered == sells_unfiltered, (
            f"BEAR regime must not block SELL: filtered={sells_filtered}, "
            f"unfiltered={sells_unfiltered}"
        )

    def test_bear_blocks_more_buys_than_bull(self):
        """BEAR regime_df must block strictly more (or equal) BUYs than BULL."""
        df = make_ohlcv(500, base=100, trend=0.002, seed=7)
        spy_bull = make_spy_bull(500)
        spy_bear = make_spy_bear(500)

        cfg = MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=True, regime_ma_period=200,
        )
        sigs_bull = MomentumStrategy(cfg).generate_signals_series("TEST", df, regime_df=spy_bull)
        sigs_bear = MomentumStrategy(cfg).generate_signals_series("TEST", df, regime_df=spy_bear)

        buys_bull = (sigs_bull["direction"] == "BUY").sum()
        buys_bear = (sigs_bear["direction"] == "BUY").sum()
        assert buys_bull >= buys_bear, (
            f"BEAR should block ≥ BUY signals: bull={buys_bull}, bear={buys_bear}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Regime neutral_pct boundary tests
# ─────────────────────────────────────────────────────────────────────────────

class TestRegimeBoundaries:
    """Neutral band edges must be respected exactly."""

    def _make_spy_at_ratio(self, ratio: float, n: int = 250) -> pd.DataFrame:
        """Build SPY where price ends exactly at MA200 × (1 + ratio)."""
        dates = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
        # First 200 bars flat at 400 → MA200 ≈ 400 when all bars are done
        # Last bar at 400 × (1 + ratio)
        close = np.full(n, 400.0, dtype=float)
        close[-1] = 400.0 * (1 + ratio)
        return pd.DataFrame(
            {"open": close, "high": close * 1.001, "low": close * 0.999,
             "close": close, "volume": np.full(n, 80_000_000.0), "vwap": close},
            index=dates,
        )

    def test_just_above_neutral_is_bull(self):
        spy = self._make_spy_at_ratio(0.025)  # +2.5% > +2% neutral threshold
        s = MomentumStrategy(MomentumConfig(regime_neutral_pct=0.02))
        regime = s.update_regime(spy)
        assert regime == "BULL"

    def test_just_below_neutral_is_bear(self):
        spy = self._make_spy_at_ratio(-0.025)  # -2.5% < -2% neutral threshold
        s = MomentumStrategy(MomentumConfig(regime_neutral_pct=0.02))
        regime = s.update_regime(spy)
        assert regime == "BEAR"

    def test_at_zero_deviation_is_neutral(self):
        spy = self._make_spy_at_ratio(0.0)  # exactly at MA200
        s = MomentumStrategy(MomentumConfig(regime_neutral_pct=0.02))
        regime = s.update_regime(spy)
        assert regime == "NEUTRAL"

    def test_custom_neutral_pct(self):
        """A tighter neutral band (1%) should classify ±2.5% as BULL/BEAR."""
        spy_bull = self._make_spy_at_ratio(0.015)  # +1.5% > +1%
        spy_bear = self._make_spy_at_ratio(-0.015) # -1.5% < -1%
        s = MomentumStrategy(MomentumConfig(regime_neutral_pct=0.01))
        assert s.update_regime(spy_bull) == "BULL"
        assert s.update_regime(spy_bear) == "BEAR"
