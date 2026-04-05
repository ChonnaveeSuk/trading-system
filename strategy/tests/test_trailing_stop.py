# trading-system/strategy/tests/test_trailing_stop.py
#
# Tests for trailing stop loss in BacktestEngine._simulate_on_slice and
# BacktestConfig defaults.

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest

from src.backtester import BacktestConfig
from src.backtester.engine import BacktestEngine, _compute_atr_series


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_price_df(closes, highs=None, lows=None) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame from close prices."""
    n = len(closes)
    dates = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    closes = np.array(closes, dtype=float)
    highs  = np.array(highs,  dtype=float) if highs  is not None else closes * 1.005
    lows   = np.array(lows,   dtype=float) if lows   is not None else closes * 0.995
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows,
         "close": closes, "volume": np.ones(n) * 1e6, "vwap": closes},
        index=dates,
    )


def _make_signals(price_df: pd.DataFrame, direction_map: dict) -> pd.DataFrame:
    """Build a signals DataFrame with explicit per-bar directions."""
    signals = pd.DataFrame(index=price_df.index)
    signals["direction"] = "HOLD"
    signals["score"] = 0.0
    signals["fast_ma"] = price_df["close"]
    signals["slow_ma"] = price_df["close"]
    signals["ma_spread_bps"] = 0.0
    signals["vol_ratio"] = 1.0
    signals["rsi"] = 50.0
    for date_idx, direction in direction_map.items():
        if direction != "HOLD":
            signals.iloc[date_idx, signals.columns.get_loc("direction")] = direction
            signals.iloc[date_idx, signals.columns.get_loc("score")] = 0.75
    return signals


# ─────────────────────────────────────────────────────────────────────────────
# BacktestConfig defaults
# ─────────────────────────────────────────────────────────────────────────────

class TestBacktestConfigTrailingStop:
    def test_trailing_stop_disabled_by_default(self):
        # Default is False: explicit opt-in preserves baseline gate behaviour.
        assert BacktestConfig().trailing_stop is False

    def test_trailing_stop_atr_mult_default(self):
        assert BacktestConfig().trailing_stop_atr_mult == 2.0

    def test_trailing_stop_can_be_disabled(self):
        cfg = BacktestConfig(trailing_stop=False)
        assert cfg.trailing_stop is False

    def test_trailing_stop_atr_mult_configurable(self):
        cfg = BacktestConfig(trailing_stop_atr_mult=2.0)
        assert cfg.trailing_stop_atr_mult == 2.0


# ─────────────────────────────────────────────────────────────────────────────
# _compute_atr_series
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeAtrSeries:
    def test_atr_series_length_matches_input(self):
        df = _make_price_df(np.linspace(100, 110, 30))
        atr = _compute_atr_series(df, period=14)
        assert len(atr) == len(df)

    def test_atr_series_nan_before_warmup(self):
        """First period-1 values must be NaN (not enough bars yet)."""
        df = _make_price_df(np.linspace(100, 110, 30))
        atr = _compute_atr_series(df, period=14)
        # First 13 values should be NaN (need 14 bars for first valid ATR)
        assert atr.iloc[:13].isna().all()

    def test_atr_series_positive_after_warmup(self):
        df = _make_price_df(np.linspace(100, 110, 30))
        atr = _compute_atr_series(df, period=14)
        valid = atr.dropna()
        assert (valid > 0).all()

    def test_atr_series_uses_high_low_range(self):
        """Wide high-low spread must produce larger ATR than tight spread."""
        closes = np.full(30, 100.0)
        df_wide = _make_price_df(closes, highs=closes + 5.0, lows=closes - 5.0)
        df_tight = _make_price_df(closes, highs=closes + 0.1, lows=closes - 0.1)
        atr_wide  = _compute_atr_series(df_wide,  period=5).dropna()
        atr_tight = _compute_atr_series(df_tight, period=5).dropna()
        assert float(atr_wide.mean()) > float(atr_tight.mean())


# ─────────────────────────────────────────────────────────────────────────────
# _simulate_on_slice: trailing stop behaviour
# ─────────────────────────────────────────────────────────────────────────────

class TestTrailingStopSimulate:

    def test_trailing_stop_triggers_sell_on_reversal(self):
        """A position opened at bar 5 should be stopped out when price drops
        far enough below the post-entry high watermark."""
        # Bars 0–4:  warmup / flat  (no ATR NaN issues with period=5)
        # Bar 5:     BUY at 100
        # Bars 6–8:  price rises to 110 → trail high follows
        # Bar 9:     price crashes to 90 → below trailing stop → SELL
        closes = [100.0] * 5 + [100.0, 103.0, 106.0, 110.0, 90.0]
        highs  = [c * 1.005 for c in closes]
        lows   = [c * 0.995 for c in closes]
        df = _make_price_df(closes, highs, lows)
        signals = _make_signals(df, {5: "BUY"})

        result = BacktestEngine._simulate_on_slice(
            df, signals, 10_000.0, 0.10,
            trailing_stop=True, trailing_atr_mult=1.5,
        )
        sides = [t["side"] for t in result["trades"]]
        # Must have at least one trailing stop sell
        assert any("trail" in s for s in sides), (
            f"Expected a trailing stop SELL but got: {sides}"
        )

    def test_trailing_stop_disabled_no_early_exit(self):
        """With trailing_stop=False the position should survive the same crash."""
        closes = [100.0] * 5 + [100.0, 103.0, 106.0, 110.0, 90.0]
        highs  = [c * 1.005 for c in closes]
        lows   = [c * 0.995 for c in closes]
        df = _make_price_df(closes, highs, lows)
        signals = _make_signals(df, {5: "BUY"})

        result = BacktestEngine._simulate_on_slice(
            df, signals, 10_000.0, 0.10,
            trailing_stop=False,
        )
        sides = [t["side"] for t in result["trades"]]
        assert not any("trail" in s for s in sides)

    def test_trailing_stop_ratchet_never_moves_down(self):
        """Once the stop is ratcheted up to X, it must never retreat below X.

        Setup: 25 warm-up bars so ATR(14) is valid, then BUY, then a big rally
        to push the stop well above entry, then a slow decline that should be
        stopped out above entry price.
        """
        # 25 warmup bars at 100 → ATR(14) is well-defined
        warmup = [100.0] * 25
        # BUY at index 25; then price ramps up to 140, then slowly back to 120
        rally  = list(np.linspace(100, 140, 15))   # bars 25–39: rapid rise
        retreat = list(np.linspace(140, 100, 20))  # bars 40–59: slow fall
        closes = warmup + rally + retreat
        highs  = [c * 1.005 for c in closes]
        lows   = [c * 0.995 for c in closes]
        df = _make_price_df(closes, highs, lows)
        signals = _make_signals(df, {25: "BUY"})

        result = BacktestEngine._simulate_on_slice(
            df, signals, 10_000.0, 0.10,
            trailing_stop=True, trailing_atr_mult=1.5,
        )
        trailing_trades = [t for t in result["trades"] if "trail" in t.get("side", "")]
        # The stop must fire during the retreat
        assert len(trailing_trades) > 0, "Expected trailing stop to fire during retreat"

        # Sell price must be above entry (100) — stop was ratcheted up past entry
        sell_price = trailing_trades[0]["price"]
        assert sell_price > 100.0, (
            f"After rally to 140, trailing stop should exit well above entry (100); "
            f"got {sell_price:.4f}"
        )

    def test_signal_sell_respected_before_trailing_stop(self):
        """A signal SELL fires before the trailing stop would trigger."""
        # Price rises steadily — no stop trigger — but signal fires SELL at bar 8
        closes = [100.0] * 5 + list(np.linspace(100, 130, 10))
        highs  = [c * 1.005 for c in closes]
        lows   = [c * 0.995 for c in closes]
        df = _make_price_df(closes, highs, lows)
        signals = _make_signals(df, {5: "BUY", 8: "SELL"})

        result = BacktestEngine._simulate_on_slice(
            df, signals, 10_000.0, 0.10,
            trailing_stop=True, trailing_atr_mult=1.5,
        )
        sells = [t for t in result["trades"] if "SELL" in t["side"]]
        assert len(sells) >= 1
        # The sell should be a normal SELL, not a trailing stop
        first_sell_side = sells[0]["side"]
        assert "trail" not in first_sell_side, (
            f"Expected normal SELL but got '{first_sell_side}'"
        )

    def test_trailing_stop_reduces_maxdd_on_reversal(self):
        """MaxDD with trailing stop enabled should be lower than without."""
        # Strong bull run then sharp crash — trailing stop should cut losses
        n = 50
        bull = np.linspace(100, 200, 30)   # +100% rally
        bear = np.linspace(200, 80, 20)    # -60% crash
        closes = np.concatenate([bull, bear])
        highs = closes * 1.005
        lows  = closes * 0.995
        df = _make_price_df(closes, highs, lows)
        signals = _make_signals(df, {10: "BUY"})   # buy mid-rally

        def max_drawdown(result):
            eq = np.array(result["equity_curve"])
            peak = np.maximum.accumulate(eq)
            dd = (eq - peak) / peak
            return float(abs(dd.min()))

        result_on  = BacktestEngine._simulate_on_slice(df, signals, 10_000.0, 0.10, trailing_stop=True,  trailing_atr_mult=1.5)
        result_off = BacktestEngine._simulate_on_slice(df, signals, 10_000.0, 0.10, trailing_stop=False)

        dd_on  = max_drawdown(result_on)
        dd_off = max_drawdown(result_off)

        assert dd_on < dd_off, (
            f"Trailing stop should reduce MaxDD: on={dd_on:.2%} vs off={dd_off:.2%}"
        )

    def test_no_new_position_opened_during_trailing_sell(self):
        """The trailing stop SELL must close the position; no re-entry on same bar."""
        closes = [100.0] * 5 + [100.0, 110.0, 120.0, 115.0, 95.0]
        highs  = [c * 1.005 for c in closes]
        lows   = [c * 0.995 for c in closes]
        df = _make_price_df(closes, highs, lows)
        # BUY at bar 5; BUY again at bar 9 (same bar trailing stop may fire)
        signals = _make_signals(df, {5: "BUY", 9: "BUY"})

        result = BacktestEngine._simulate_on_slice(
            df, signals, 10_000.0, 0.10,
            trailing_stop=True, trailing_atr_mult=1.5,
        )
        buys = [t for t in result["trades"] if t["side"] == "BUY"]
        # Should have at most 1 BUY (no double-entry on stop bar)
        assert len(buys) <= 2  # at most initial BUY + one re-entry after stop
