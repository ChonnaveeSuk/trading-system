# trading-system/strategy/tests/test_vix_filter.py
#
# Tests for the VIX (volatility) filter in MomentumStrategy.
#
# State logic under test:
#   CALM    (level < caution): no change to signals
#   CAUTION (caution ≤ level < panic): position-size halved on BUY orders
#   PANIC   (level ≥ panic): all new BUY suppressed → HOLD, score=0
#   SELL signals always pass (need to be able to exit positions in a panic)

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from src.signals import Direction
from src.signals.momentum import MomentumStrategy, MomentumConfig


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _recent_dates(n: int) -> pd.DatetimeIndex:
    """Daily dates ending today (UTC) so staleness checks never fire."""
    end = pd.Timestamp.today(tz="UTC").normalize()
    return pd.date_range(end=end, periods=n, freq="D")


def make_vixy(level: float, n: int = 60) -> pd.DataFrame:
    """Synthetic VIXY OHLCV with close ≈ `level` for a stable MA20."""
    dates = _recent_dates(n)
    close = np.full(n, level, dtype=float)
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99,
         "close": close, "volume": np.full(n, 1_000_000.0), "vwap": close},
        index=dates,
    )


def make_buy_scenario(n: int = 300) -> pd.DataFrame:
    """OHLCV that triggers an RSI-oversold BUY at the last bar."""
    dates = _recent_dates(n)
    close = np.concatenate([
        np.linspace(100, 130, n - 20),
        np.linspace(130, 90, 20),
    ])
    high = close * 1.005
    low = close * 0.995
    volume = np.full(n, 1_000_000.0)
    return pd.DataFrame(
        {"open": close, "high": high, "low": low,
         "close": close, "volume": volume, "vwap": close},
        index=dates,
    )


# ─────────────────────────────────────────────────────────────────────────────
# update_vix() classification
# ─────────────────────────────────────────────────────────────────────────────

class TestUpdateVix:
    """update_vix() must classify CALM / CAUTION / PANIC correctly."""

    def test_calm_below_caution_threshold(self):
        s = MomentumStrategy(MomentumConfig(vix_ma_period=20))
        assert s.update_vix(make_vixy(15.0)) == "CALM"
        assert s.current_vix_state == "CALM"

    def test_caution_between_thresholds(self):
        s = MomentumStrategy(MomentumConfig(vix_ma_period=20))
        assert s.update_vix(make_vixy(25.0)) == "CAUTION"

    def test_panic_above_panic_threshold(self):
        s = MomentumStrategy(MomentumConfig(vix_ma_period=20))
        assert s.update_vix(make_vixy(35.0)) == "PANIC"

    def test_caution_threshold_inclusive(self):
        """level == vix_caution_threshold → CAUTION (≥, not >)."""
        s = MomentumStrategy(MomentumConfig(
            vix_caution_threshold=20.0, vix_panic_threshold=30.0, vix_ma_period=20,
        ))
        assert s.update_vix(make_vixy(20.0)) == "CAUTION"

    def test_panic_threshold_inclusive(self):
        s = MomentumStrategy(MomentumConfig(
            vix_caution_threshold=20.0, vix_panic_threshold=30.0, vix_ma_period=20,
        ))
        assert s.update_vix(make_vixy(30.0)) == "PANIC"

    def test_custom_thresholds(self):
        cfg = MomentumConfig(vix_caution_threshold=15.0, vix_panic_threshold=25.0, vix_ma_period=20)
        s = MomentumStrategy(cfg)
        assert s.update_vix(make_vixy(10.0)) == "CALM"
        assert s.update_vix(make_vixy(20.0)) == "CAUTION"
        assert s.update_vix(make_vixy(28.0)) == "PANIC"

    def test_insufficient_data_defaults_to_calm(self):
        # 10 bars at high VIX, but ma_period=20 → MA all NaN → CALM
        spy = make_vixy(40.0, n=10)
        s = MomentumStrategy(MomentumConfig(vix_ma_period=20))
        assert s.update_vix(spy) == "CALM"

    def test_empty_df_defaults_to_calm(self):
        s = MomentumStrategy()
        assert s.update_vix(pd.DataFrame()) == "CALM"

    def test_disabled_filter_returns_calm(self):
        """vix_filter=False → update_vix() always returns CALM."""
        s = MomentumStrategy(MomentumConfig(vix_filter=False))
        assert s.update_vix(make_vixy(50.0)) == "CALM"

    def test_level_and_price_cached(self):
        s = MomentumStrategy(MomentumConfig(vix_ma_period=20))
        s.update_vix(make_vixy(25.0))
        assert s._vix_level is not None
        assert s._vix_price is not None
        assert abs(s._vix_level - 25.0) < 0.01
        assert abs(s._vix_price - 25.0) < 0.01


# ─────────────────────────────────────────────────────────────────────────────
# generate_signal() — single-bar live path
# ─────────────────────────────────────────────────────────────────────────────

class TestGenerateSignalVix:
    """VIX filter must apply correctly in the single-bar live path."""

    def _strategy(self, vix_state: str) -> MomentumStrategy:
        s = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False,  # isolate VIX behavior
            vix_filter=True,
        ))
        s._vix_state = vix_state
        return s

    def test_panic_blocks_buy(self):
        df = make_buy_scenario()
        s = self._strategy("PANIC")
        # generate_signals_series is the easiest way to find a BUY in this scenario;
        # if the unfiltered run produced one, PANIC must produce none.
        unfiltered = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False, vix_filter=False,
        )).generate_signals_series("TEST", df)
        if not (unfiltered["direction"] == "BUY").any():
            pytest.skip("No BUY in unfiltered scenario — cannot verify suppression")
        sigs = s.generate_signals_series("TEST", df)
        assert (sigs["direction"] == "BUY").sum() == 0

    def test_panic_allows_sell(self):
        """SELL signals must pass through under PANIC (need to exit)."""
        df = make_buy_scenario()
        s_panic = self._strategy("PANIC")
        s_off   = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False, vix_filter=False,
        ))
        sigs_panic = s_panic.generate_signals_series("TEST", df)
        sigs_off   = s_off.generate_signals_series("TEST", df)
        assert (sigs_panic["direction"] == "SELL").sum() == (sigs_off["direction"] == "SELL").sum()

    def test_caution_halves_quantity_on_buy(self):
        """CAUTION must produce ≈ half the quantity vs CALM on the same scenario."""
        df = make_buy_scenario()
        s_calm    = self._strategy("CALM")
        s_caution = self._strategy("CAUTION")

        sig_calm    = s_calm.generate_signal("AAPL", df, portfolio_value=100_000.0)
        sig_caution = s_caution.generate_signal("AAPL", df, portfolio_value=100_000.0)

        if sig_calm.direction != Direction.BUY or sig_caution.direction != Direction.BUY:
            pytest.skip("Scenario didn't produce BUY — adjust fixture")

        qty_calm    = float(sig_calm.suggested_quantity or 0)
        qty_caution = float(sig_caution.suggested_quantity or 0)
        assert qty_caution > 0
        # Allow small tolerance for instrument rounding (whole shares)
        assert qty_caution <= qty_calm * 0.55, (
            f"CAUTION qty {qty_caution} should be ≤ 55% of CALM qty {qty_calm}"
        )
        assert qty_caution >= qty_calm * 0.45, (
            f"CAUTION qty {qty_caution} should be ≥ 45% of CALM qty {qty_calm}"
        )

    def test_calm_does_not_change_buy(self):
        """CALM must leave BUY signals untouched."""
        df = make_buy_scenario()
        s_calm = self._strategy("CALM")
        s_off  = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False, vix_filter=False,
        ))
        sigs_calm = s_calm.generate_signals_series("TEST", df)
        sigs_off  = s_off.generate_signals_series("TEST", df)
        pd.testing.assert_series_equal(
            sigs_calm["direction"], sigs_off["direction"], check_names=False,
        )

    def test_disabled_filter_passes_all_buys(self):
        """vix_filter=False → cached PANIC state is ignored."""
        df = make_buy_scenario()
        s = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False, vix_filter=False,
        ))
        s._vix_state = "PANIC"  # would block if filter were on
        sigs = s.generate_signals_series("TEST", df)
        ref = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False, vix_filter=False,
        )).generate_signals_series("TEST", df)
        pd.testing.assert_series_equal(sigs["direction"], ref["direction"], check_names=False)

    def test_vix_state_logged_in_features(self):
        df = make_buy_scenario()
        s = self._strategy("PANIC")
        sig = s.generate_signal("TEST", df, portfolio_value=100_000.0)
        assert sig.features is not None
        assert sig.features.get("vix_state") == "PANIC"


# ─────────────────────────────────────────────────────────────────────────────
# generate_signals_series() — bar-by-bar backtest path
# ─────────────────────────────────────────────────────────────────────────────

class TestGenerateSignalsSeriesVix:
    """vix_df bar-by-bar must override the cached vix state."""

    def _vixy_series(self, level: float, n: int) -> pd.DataFrame:
        return make_vixy(level, n=n)

    def _make_ohlcv(self, n: int = 400) -> pd.DataFrame:
        rng = np.random.default_rng(seed=11)
        dates = _recent_dates(n)
        # Trend up then sharp drop → produces both BUY (RSI oversold) and SELL bars
        base = np.concatenate([np.linspace(100, 140, n - 30),
                               np.linspace(140, 95, 30)])
        noise = rng.normal(0, 0.5, n)
        close = base + noise
        high = close * 1.005
        low  = close * 0.995
        return pd.DataFrame(
            {"open": close, "high": high, "low": low,
             "close": close, "volume": np.full(n, 5_000_000.0), "vwap": close},
            index=dates,
        )

    def test_panic_vix_df_blocks_buys(self):
        df = self._make_ohlcv(400)
        vix = self._vixy_series(40.0, n=400)
        s = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False, vix_filter=True, vix_ma_period=20,
        ))
        sigs = s.generate_signals_series("TEST", df, vix_df=vix)
        # After MA20 warm-up, every bar is in PANIC
        post_warmup = sigs.iloc[20:]
        panic_buys = (
            (post_warmup["direction"] == "BUY")
            & (post_warmup.get("vix_state", pd.Series(dtype=str)) == "PANIC")
        ).sum()
        assert panic_buys == 0

    def test_calm_vix_df_does_not_block(self):
        df = self._make_ohlcv(400)
        vix_calm = self._vixy_series(12.0, n=400)
        vix_panic = self._vixy_series(40.0, n=400)
        s_calm = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False, vix_filter=True, vix_ma_period=20,
        ))
        s_panic = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False, vix_filter=True, vix_ma_period=20,
        ))
        sigs_calm  = s_calm.generate_signals_series("TEST", df, vix_df=vix_calm)
        sigs_panic = s_panic.generate_signals_series("TEST", df, vix_df=vix_panic)
        buys_calm  = (sigs_calm["direction"]  == "BUY").sum()
        buys_panic = (sigs_panic["direction"] == "BUY").sum()
        assert buys_calm >= buys_panic
        # If CALM produced BUYs, PANIC must have produced strictly fewer.
        if buys_calm > 0:
            assert buys_panic < buys_calm

    def test_vix_state_column_populated(self):
        df = self._make_ohlcv(400)
        vix = self._vixy_series(25.0, n=400)
        s = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False, vix_filter=True, vix_ma_period=20,
        ))
        sigs = s.generate_signals_series("TEST", df, vix_df=vix)
        assert "vix_state" in sigs.columns
        # After MA20 warm-up, every bar must have a known state
        post = sigs.iloc[20:]
        assert post["vix_state"].isin(["CALM", "CAUTION", "PANIC"]).all()

    def test_vix_df_none_uses_cached_state(self):
        """When vix_df=None, falls back to cached self._vix_state."""
        df = self._make_ohlcv(300)
        s = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False, vix_filter=True,
        ))
        s._vix_state = "PANIC"
        sigs = s.generate_signals_series("TEST", df, vix_df=None)
        assert (sigs["direction"] == "BUY").sum() == 0

    def test_panic_does_not_change_sells(self):
        df = self._make_ohlcv(400)
        vix_panic = self._vixy_series(40.0, n=400)
        s_panic = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False, vix_filter=True, vix_ma_period=20,
        ))
        s_off = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False, vix_filter=False,
        ))
        sigs_panic = s_panic.generate_signals_series("TEST", df, vix_df=vix_panic)
        sigs_off   = s_off.generate_signals_series("TEST", df)
        sells_panic = (sigs_panic["direction"] == "SELL").sum()
        sells_off   = (sigs_off["direction"]   == "SELL").sum()
        assert sells_panic == sells_off
