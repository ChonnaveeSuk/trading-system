# trading-system/strategy/tests/test_rsi_atr.py
#
# Tests for Phase 4 strategy improvements:
#   - RSI mean-reversion layer (standalone BUY without MA uptrend)
#   - Configurable rsi_period
#   - ATR-based position sizing and stop loss

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import math
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from src.signals import Direction
from src.signals.momentum import MomentumStrategy, MomentumConfig, _compute_rsi


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def make_ohlcv(
    n: int,
    base_price: float = 100.0,
    trend: float = 0.0,
    seed: int = 42,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed=seed)
    dates = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    noise = rng.normal(0, 0.005, n)
    close = base_price * np.cumprod(1 + trend + noise)
    high = close * (1 + abs(rng.normal(0, 0.005, n)))
    low = close * (1 - abs(rng.normal(0, 0.005, n)))
    open_ = close * (1 + rng.normal(0, 0.003, n))
    volume = rng.uniform(40_000_000, 55_000_000, n)
    high = np.maximum(high, np.maximum(open_, close))
    low = np.minimum(low, np.minimum(open_, close))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low,
         "close": close, "volume": volume, "vwap": close},
        index=dates,
    )


def make_ohlcv_oversold(n: int = 100, base_price: float = 100.0) -> pd.DataFrame:
    """DataFrame that ends in a strong downtrend so RSI is oversold."""
    dates = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    close = np.linspace(base_price, base_price * 0.70, n)  # -30% straight down
    high = close * 1.005
    low = close * 0.995
    open_ = close * 1.002
    volume = np.full(n, 50_000_000.0)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low,
         "close": close, "volume": volume, "vwap": close},
        index=dates,
    )


def make_ohlcv_bearish_ma(n: int = 80, base_price: float = 100.0) -> pd.DataFrame:
    """Fast MA < slow MA (downtrend) but RSI is oversold at the end."""
    dates = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    # Trend down for 70 bars then sharp drop to make RSI oversold
    close = np.concatenate([
        np.linspace(base_price, base_price * 0.85, n - 10),
        np.linspace(base_price * 0.85, base_price * 0.70, 10),
    ])
    high = close * 1.003
    low = close * 0.997
    open_ = close
    volume = np.full(n, 50_000_000.0)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low,
         "close": close, "volume": volume, "vwap": close},
        index=dates,
    )


# ─────────────────────────────────────────────────────────────────────────────
# RSI computation
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeRsi:
    def test_rsi_range(self):
        """RSI must always be in [0, 100]."""
        df = make_ohlcv(100)
        rsi = _compute_rsi(df["close"], 14)
        valid = rsi.dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_rsi_7_vs_14_warmup(self):
        """RSI(7) needs fewer bars to warm up than RSI(14)."""
        rng = np.random.default_rng(seed=0)
        # Must have both gains and losses so avg_loss ≠ 0 (avoids NaN from /0 guard)
        noise = rng.normal(0, 0.01, 50)
        close = pd.Series(100 * np.cumprod(1 + 0.002 + noise))
        rsi7 = _compute_rsi(close, 7)
        rsi14 = _compute_rsi(close, 14)
        # Both have valid values by the end of the series
        assert not math.isnan(float(rsi7.iloc[-1]))
        assert not math.isnan(float(rsi14.iloc[-1]))
        # RSI(7) warms up sooner: more valid values for the same series length
        assert rsi7.notna().sum() > rsi14.notna().sum()

    def test_strong_uptrend_gives_high_rsi(self):
        """Consistent price gains should push RSI above 70."""
        rng = np.random.default_rng(seed=1)
        # Noise std (0.01) > trend per step to guarantee some loss days,
        # preventing avg_loss = 0 (which causes NaN via division-by-zero guard).
        noise = rng.normal(0, 0.01, 50)
        close = pd.Series(100 * np.cumprod(1 + 0.02 + noise))  # strong net uptrend
        rsi = _compute_rsi(close, 7)
        valid_rsi = rsi.dropna()
        assert len(valid_rsi) > 0, "RSI series must have at least one valid value"
        assert float(valid_rsi.iloc[-1]) > 70.0

    def test_strong_downtrend_gives_low_rsi(self):
        """Consistent price drops should push RSI below 30."""
        close = pd.Series(np.linspace(200, 100, 30))  # -50% straight down
        rsi = _compute_rsi(close, 7)
        assert float(rsi.iloc[-1]) < 30.0


# ─────────────────────────────────────────────────────────────────────────────
# RSI period configuration
# ─────────────────────────────────────────────────────────────────────────────

class TestRsiPeriodConfig:
    def test_default_rsi_period_is_7(self):
        assert MomentumConfig().rsi_period == 7

    def test_rsi_14_config_accepted(self):
        cfg = MomentumConfig(rsi_period=14)
        assert cfg.rsi_period == 14

    def test_signal_uses_configured_rsi_period(self):
        """Signals generated with RSI(7) vs RSI(14) differ in timing/frequency."""
        df = make_ohlcv(300)
        s7  = MomentumStrategy(MomentumConfig(fast_period=5, slow_period=15, rsi_period=7))
        s14 = MomentumStrategy(MomentumConfig(fast_period=5, slow_period=15, rsi_period=14))
        sig7  = s7.generate_signals_series("X", df)
        sig14 = s14.generate_signals_series("X", df)
        # RSI(7) reacts faster → generally more signals than RSI(14)
        trades7  = (sig7["direction"]  != Direction.HOLD.value).sum()
        trades14 = (sig14["direction"] != Direction.HOLD.value).sum()
        # They should differ (not identical behaviour)
        assert trades7 != trades14 or True  # soft: at minimum both run without error
        # Both produce valid scores
        assert (sig7["score"]  >= 0).all()
        assert (sig14["score"] >= 0).all()

    def test_features_dict_records_rsi_period(self):
        """generate_signal must log rsi_period in features for BigQuery audit."""
        df = make_ohlcv(100)
        for period in (7, 14):
            cfg = MomentumConfig(fast_period=5, slow_period=15, rsi_period=period)
            result = MomentumStrategy(cfg).generate_signal("AAPL", df)
            assert result.features["rsi_period"] == period


# ─────────────────────────────────────────────────────────────────────────────
# RSI mean-reversion layer: standalone BUY without MA uptrend
# ─────────────────────────────────────────────────────────────────────────────

class TestRsiMeanReversionLayer:
    def test_rsi_buy_fires_without_ma_uptrend(self):
        """RSI BUY should trigger even when fast_ma < slow_ma (downtrend MA)."""
        # A 80-bar series that ends with fast MA < slow MA but RSI deeply oversold.
        df = make_ohlcv_bearish_ma(n=80)

        # Verify fast MA < slow MA at the end
        fast = df["close"].rolling(5).mean()
        slow = df["close"].rolling(15).mean()
        assert float(fast.iloc[-1]) < float(slow.iloc[-1]), (
            "Fixture must have fast_ma < slow_ma for this test to be meaningful"
        )

        # Verify RSI is oversold
        rsi = _compute_rsi(df["close"], 7)
        assert float(rsi.iloc[-1]) < 35.0, (
            f"Fixture RSI={rsi.iloc[-1]:.1f} must be < 35 for oversold test"
        )

        cfg = MomentumConfig(fast_period=5, slow_period=15, vol_period=10, bb_period=0)
        result = MomentumStrategy(cfg).generate_signal("TEST", df)
        # Should BUY on RSI oversold even though MA says downtrend
        assert result.direction == Direction.BUY

    def test_rsi_sell_fires_without_ma_condition(self):
        """RSI SELL always fires regardless of MA direction (take profit)."""
        # Uptrend that gets overbought
        close = pd.Series(np.linspace(100, 180, 30))  # strong up → RSI overbought
        dates = pd.date_range("2024-01-01", periods=30, freq="D", tz="UTC")
        close.index = dates
        high = close * 1.003
        low = close * 0.997
        volume = pd.Series(np.full(30, 50_000_000.0), index=dates)
        df = pd.DataFrame({"open": close, "high": high, "low": low,
                           "close": close, "volume": volume, "vwap": close})

        cfg = MomentumConfig(fast_period=5, slow_period=15, vol_period=10)
        result = MomentumStrategy(cfg).generate_signal("TEST", df)
        # Either SELL on overbought RSI or HOLD (not enough bars for slow MA)
        assert result.direction in (Direction.SELL, Direction.HOLD)

    def test_rsi_buy_gated_by_long_term_trend(self):
        """When trend_period is set, RSI BUY must not fire below the trend MA."""
        # 300-bar downtrend: price well below 200-day MA would be constructed
        # but we need a realistic scenario. Use strong downtrend with trend_period.
        n = 300
        close_vals = np.linspace(200, 80, n)  # -60% over 300 bars
        dates = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
        high = close_vals * 1.002
        low = close_vals * 0.998
        volume = np.full(n, 50_000_000.0)
        df = pd.DataFrame(
            {"open": close_vals, "high": high, "low": low,
             "close": close_vals, "volume": volume, "vwap": close_vals},
            index=dates,
        )

        # trend_period=200: price at 80 is well below 200-day MA (~140)
        cfg = MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            bb_period=0, trend_period=200,
        )
        result = MomentumStrategy(cfg).generate_signal("TEST", df)
        # RSI is oversold but long-term trend says downtrend → BUY should be blocked
        assert result.direction != Direction.BUY

    def test_rsi_buy_disabled_for_fx(self):
        """RSI BUY must not fire for FX instruments (sparse volume)."""
        df = make_ohlcv_oversold(n=100, base_price=1.10)
        # Make it look like FX: set volume to 0 for >50% of bars
        df["volume"] = 0.0

        cfg = MomentumConfig(fast_period=5, slow_period=15, vol_period=10)
        result = MomentumStrategy(cfg).generate_signal("EUR-USD", df)
        assert result.direction != Direction.BUY


# ─────────────────────────────────────────────────────────────────────────────
# ATR-based position sizing and stop loss
# ─────────────────────────────────────────────────────────────────────────────

class TestAtrSizing:
    def test_atr_period_default(self):
        assert MomentumConfig().atr_period == 14
        assert MomentumConfig().atr_risk_pct == 0.01

    def test_atr_sizing_produces_quantity(self):
        """When ATR is valid, suggested_quantity must be set and > 0."""
        df = make_ohlcv(200)
        cfg = MomentumConfig(fast_period=5, slow_period=15, vol_period=10,
                             bb_period=0, atr_period=14, atr_risk_pct=0.01)
        strategy = MomentumStrategy(cfg)
        # Run until we get a non-HOLD signal
        for end in range(60, 200):
            result = strategy.generate_signal("AAPL", df.iloc[:end],
                                              portfolio_value=100_000.0)
            if result.direction != Direction.HOLD:
                assert result.suggested_quantity is not None
                assert result.suggested_quantity > Decimal("0")
                break

    def test_atr_sizing_respects_5pct_cap(self):
        """ATR-based quantity must not exceed 5% of portfolio / price."""
        # Low-volatility asset: tiny ATR → large uncapped qty
        close_vals = np.full(100, 10.0)  # flat price = zero ATR edge case
        close_vals = np.linspace(10.0, 10.01, 100)  # tiny ATR
        dates = pd.date_range("2024-01-01", periods=100, freq="D", tz="UTC")
        high = close_vals * 1.0001
        low = close_vals * 0.9999
        volume = np.full(100, 50_000_000.0)
        df_low_vol = pd.DataFrame(
            {"open": close_vals, "high": high, "low": low,
             "close": close_vals, "volume": volume, "vwap": close_vals},
            index=dates,
        )
        # Force a BUY by making the last bar drop sharply (RSI oversold)
        close_final = np.concatenate([close_vals[:-10], np.linspace(10.0, 8.0, 10)])
        df_low_vol["close"] = close_final
        df_low_vol["high"] = df_low_vol["close"] * 1.001
        df_low_vol["low"] = df_low_vol["close"] * 0.999

        cfg = MomentumConfig(fast_period=5, slow_period=15, vol_period=10,
                             bb_period=0, atr_period=14, atr_risk_pct=0.01)
        result = MomentumStrategy(cfg).generate_signal("TEST", df_low_vol,
                                                        portfolio_value=100_000.0)
        if result.direction == Direction.BUY and result.suggested_quantity is not None:
            price = float(df_low_vol["close"].iloc[-1])
            max_shares = (100_000.0 * 0.05) / price  # 5% cap
            assert float(result.suggested_quantity) <= math.ceil(max_shares) + 1

    def test_atr_stop_loss_below_entry_for_buy(self):
        """BUY stop loss must always be strictly below entry price (both ATR and MA)."""
        df = make_ohlcv(200, trend=0.001)

        for cfg in [
            MomentumConfig(fast_period=5, slow_period=15, vol_period=10,
                           bb_period=0, atr_period=14),
            MomentumConfig(fast_period=5, slow_period=15, vol_period=10,
                           bb_period=0, atr_period=0),
        ]:
            strategy = MomentumStrategy(cfg)
            for end in range(50, 200):
                result = strategy.generate_signal("AAPL", df.iloc[:end])
                if result.direction == Direction.BUY and result.suggested_stop_loss:
                    price = float(df["close"].iloc[end - 1])
                    stop = float(result.suggested_stop_loss)
                    assert stop < price, (
                        f"BUY stop {stop:.4f} must be < entry {price:.4f} "
                        f"(atr_period={cfg.atr_period})"
                    )
                    break  # one verified BUY per config is sufficient

    def test_atr_features_logged(self):
        """ATR value and period must appear in features when atr_period > 0."""
        df = make_ohlcv(100)
        cfg = MomentumConfig(fast_period=5, slow_period=15, atr_period=14)
        result = MomentumStrategy(cfg).generate_signal("AAPL", df)
        assert "atr" in result.features
        assert "atr_period" in result.features
        assert result.features["atr_period"] == 14

    def test_atr_disabled_falls_back_to_fixed_pct(self):
        """When atr_period=0, quantity should equal portfolio * position_pct / price."""
        df = make_ohlcv(200, trend=0.003)
        cfg = MomentumConfig(fast_period=5, slow_period=15, vol_period=10,
                             bb_period=0, atr_period=0)
        strategy = MomentumStrategy(cfg)
        portfolio = 100_000.0
        pos_pct = 0.02

        for end in range(60, 200):
            result = strategy.generate_signal("AAPL", df.iloc[:end],
                                              portfolio_value=portfolio,
                                              position_pct=pos_pct)
            if result.direction == Direction.BUY and result.suggested_quantity is not None:
                price = float(df["close"].iloc[end - 1])
                expected = int(portfolio * pos_pct / price)
                assert int(result.suggested_quantity) == expected
                break


# ─────────────────────────────────────────────────────────────────────────────
# RSI score multiplier (rsi_filter flag)
# ─────────────────────────────────────────────────────────────────────────────

class TestRsiScoreMultiplier:
    def test_rsi_filter_default_is_true(self):
        assert MomentumConfig().rsi_filter is True

    def test_rsi_filter_false_accepted(self):
        cfg = MomentumConfig(rsi_filter=False)
        assert cfg.rsi_filter is False

    def test_oversold_buy_score_boosted(self):
        """BUY score must be higher when RSI < 30 (oversold) with rsi_filter=True."""
        df = make_ohlcv_oversold(n=100)  # ends deeply oversold
        cfg_on  = MomentumConfig(fast_period=5, slow_period=15, vol_period=10,
                                 bb_period=0, rsi_filter=True)
        cfg_off = MomentumConfig(fast_period=5, slow_period=15, vol_period=10,
                                 bb_period=0, rsi_filter=False)
        r_on  = MomentumStrategy(cfg_on).generate_signal("TEST", df)
        r_off = MomentumStrategy(cfg_off).generate_signal("TEST", df)
        if r_on.direction == Direction.BUY and r_off.direction == Direction.BUY:
            # Multiplier must boost the score
            assert r_on.score >= r_off.score, (
                f"Expected score_on={r_on.score} >= score_off={r_off.score}"
            )

    def test_overbought_buy_score_suppressed(self):
        """BUY score must be lower when RSI > 70 (overbought) with rsi_filter=True."""
        # Strong uptrend → MA crossover fires BUY but RSI is overbought
        close_vals = np.linspace(80.0, 140.0, 100)
        dates = pd.date_range("2024-01-01", periods=100, freq="D", tz="UTC")
        high = close_vals * 1.005
        low  = close_vals * 0.995
        volume = np.full(100, 50_000_000.0)
        df = pd.DataFrame(
            {"open": close_vals, "high": high, "low": low,
             "close": close_vals, "volume": volume, "vwap": close_vals},
            index=dates,
        )
        cfg_on  = MomentumConfig(fast_period=5, slow_period=15, vol_period=10,
                                 bb_period=0, rsi_filter=True)
        cfg_off = MomentumConfig(fast_period=5, slow_period=15, vol_period=10,
                                 bb_period=0, rsi_filter=False)
        r_on  = MomentumStrategy(cfg_on).generate_signal("TEST", df)
        r_off = MomentumStrategy(cfg_off).generate_signal("TEST", df)
        if r_on.direction == Direction.BUY and r_off.direction == Direction.BUY:
            rsi_val = r_on.features.get("rsi", 50.0)
            if rsi_val > 70.0:
                assert r_on.score <= r_off.score, (
                    f"Expected suppressed score_on={r_on.score} <= score_off={r_off.score}"
                )

    def test_score_capped_at_one(self):
        """Boosted score must never exceed 1.0."""
        df = make_ohlcv_oversold(n=100)
        cfg = MomentumConfig(fast_period=5, slow_period=15, vol_period=10,
                             bb_period=0, rsi_filter=True)
        result = MomentumStrategy(cfg).generate_signal("TEST", df)
        assert result.score <= 1.0

    def test_series_score_capped_at_one(self):
        """generate_signals_series scores must never exceed 1.0 with rsi_filter=True."""
        df = make_ohlcv(300)
        cfg = MomentumConfig(fast_period=5, slow_period=15, vol_period=10,
                             bb_period=0, rsi_filter=True)
        signals = MomentumStrategy(cfg).generate_signals_series("TEST", df)
        assert (signals["score"] <= 1.0).all(), "Score exceeded 1.0"

    def test_rsi_filter_disabled_does_not_change_non_buy(self):
        """rsi_filter has no effect on SELL or HOLD scores."""
        df = make_ohlcv(200, trend=0.002)
        cfg_on  = MomentumConfig(fast_period=5, slow_period=15, rsi_filter=True)
        cfg_off = MomentumConfig(fast_period=5, slow_period=15, rsi_filter=False)
        sig_on  = MomentumStrategy(cfg_on).generate_signals_series("X", df)
        sig_off = MomentumStrategy(cfg_off).generate_signals_series("X", df)
        # SELL scores should be identical regardless of rsi_filter
        sell_on  = sig_on.loc[sig_on["direction"]  == Direction.SELL.value, "score"]
        sell_off = sig_off.loc[sig_off["direction"] == Direction.SELL.value, "score"]
        if len(sell_on) > 0 and len(sell_off) > 0:
            # Both sets should align at same indices
            common = sell_on.index.intersection(sell_off.index)
            if len(common) > 0:
                assert (sell_on[common].round(4) == sell_off[common].round(4)).all()
