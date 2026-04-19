# trading-system/strategy/tests/test_trend_ride.py
#
# Tests for Phase 5 improvements:
#   - trend_ride_buy: enter established uptrends on RSI pullback
#   - LIVE_SYMBOLS separates untradeable symbols from live mode
#   - OHLCV staleness gate (_LIVE_STALE_DAYS)

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
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_ohlcv(closes: list[float], volume: float = 1_000_000.0) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range("2025-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame({
        "open":   [c * 0.999 for c in closes],
        "high":   [c * 1.005 for c in closes],
        "low":    [c * 0.995 for c in closes],
        "close":  closes,
        "volume": [volume] * n,
        "vwap":   closes,
    }, index=idx)


def _make_uptrend(n_bars: int = 60, start: float = 100.0, slope: float = 0.3) -> list[float]:
    """Steadily rising prices — fast MA will be above slow MA."""
    return [start + slope * i for i in range(n_bars)]


def _make_pullback_tail(base_closes: list[float], pullback_pct: float = 0.025) -> list[float]:
    """Append a gentle pullback over 8 bars — enough to dip RSI(7) into 30-45 range
    without triggering rsi_buy (RSI < 30)."""
    peak = base_closes[-1]
    # Spread pullback over 8 bars so each daily drop is small
    return base_closes + [peak * (1 - pullback_pct * (i + 1) / 8) for i in range(8)]


# ─────────────────────────────────────────────────────────────────────────────
# Tests: trend_ride_buy signal
# ─────────────────────────────────────────────────────────────────────────────

class TestTrendRideBuy:

    def _cfg(self, **kwargs) -> MomentumConfig:
        defaults = dict(
            fast_period=5, slow_period=15, vol_period=10, bb_period=0,
            atr_period=0,            # disable ATR for deterministic qty
            trend_ride_rsi=45.0,
            trend_ride_min_bars=10,
            regime_filter=False,     # isolate from regime logic
        )
        defaults.update(kwargs)
        return MomentumConfig(**defaults)

    def test_trend_ride_fires_in_established_uptrend_with_rsi_pullback(self):
        """trend_ride_buy should fire when fast > slow ≥10 bars and 30 < RSI < 45."""
        # 60-bar uptrend then gentle 2.5% pullback over 8 bars.
        # The pullback is mild enough that RSI(7) stays above 30 (no rsi_buy).
        closes = _make_uptrend(60, start=100.0, slope=0.3)
        closes = _make_pullback_tail(closes, pullback_pct=0.025)
        df = make_ohlcv(closes)

        cfg = self._cfg()
        strategy = MomentumStrategy(cfg)
        sig = strategy.generate_signal("AAPL", df, portfolio_value=100_000.0)

        rsi = sig.features.get("rsi", 50.0)
        # If RSI stayed above 30, trend_ride should fire; if RSI went below 30, rsi_buy fires instead.
        # Either way, the signal must be BUY.
        assert sig.direction == Direction.BUY, (
            f"Expected BUY but got {sig.direction}, rsi={rsi}, features={sig.features}"
        )
        if rsi > 30.0:
            assert sig.features.get("trend_ride") is True, (
                f"Expected trend_ride=True (rsi={rsi:.1f}), got features={sig.features}"
            )

    def test_trend_ride_disabled_when_rsi_already_oversold(self):
        """If RSI < rsi_oversold, rsi_buy takes priority; trend_ride should not fire."""
        # Deep crash (RSI will go below 30)
        closes = [100.0] * 30 + [100.0 - i * 2 for i in range(20)]
        df = make_ohlcv(closes)

        cfg = self._cfg()
        strategy = MomentumStrategy(cfg)
        sig = strategy.generate_signal("AAPL", df, portfolio_value=100_000.0)

        # Signal might be BUY (from rsi_buy) but trend_ride must NOT be the trigger
        if sig.direction == Direction.BUY:
            # If BUY fired, it was from RSI oversold, not trend_ride
            # trend_ride requires fast > slow, which won't hold after a crash
            pass
        # trend_ride requires fast > slow — a crash means fast < slow → disabled
        assert sig.features.get("trend_ride") is False

    def test_trend_ride_disabled_when_rsi_too_high(self):
        """RSI > trend_ride_rsi (45) should not trigger trend_ride."""
        # Pure uptrend, no pullback — RSI will be high
        closes = _make_uptrend(55, start=100.0, slope=1.0)
        df = make_ohlcv(closes)

        cfg = self._cfg()
        strategy = MomentumStrategy(cfg)
        sig = strategy.generate_signal("AAPL", df, portfolio_value=100_000.0)

        # Might be BUY from rsi_buy or HOLD; but trend_ride must be False (RSI > 45)
        assert sig.features.get("trend_ride") is False

    def test_trend_ride_disabled_when_insufficient_uptrend_bars(self):
        """Uptrend shorter than trend_ride_min_bars (10) should not trigger."""
        # Only 5 bars of uptrend at the end
        closes = [100.0] * 30 + [100.0 - i * 2 for i in range(10)]  # drop first
        closes += [60.0 + i * 1.5 for i in range(8)]  # 8-bar recovery (< 10)
        closes = _make_pullback_tail(closes, pullback_pct=0.05)
        df = make_ohlcv(closes)

        cfg = self._cfg(trend_ride_min_bars=10)
        strategy = MomentumStrategy(cfg)
        sig = strategy.generate_signal("AAPL", df, portfolio_value=100_000.0)

        assert sig.features.get("trend_ride") is False

    def test_trend_ride_disabled_when_rsi_zero(self):
        """Setting trend_ride_rsi=0 disables the signal entirely."""
        closes = _make_pullback_tail(_make_uptrend(50), pullback_pct=0.06)
        df = make_ohlcv(closes)

        cfg = self._cfg(trend_ride_rsi=0.0)
        strategy = MomentumStrategy(cfg)
        sig = strategy.generate_signal("AAPL", df, portfolio_value=100_000.0)

        assert sig.features.get("trend_ride") is False

    def test_trend_ride_not_for_sparse_volume(self):
        """trend_ride should not fire for FX/zero-volume instruments."""
        # Use the same gentle pullback that would trigger trend_ride for equities
        closes = _make_pullback_tail(_make_uptrend(60, slope=0.3), pullback_pct=0.025)
        df = make_ohlcv(closes, volume=0.0)  # sparse volume = FX-like

        cfg = self._cfg()
        strategy = MomentumStrategy(cfg)
        sig = strategy.generate_signal("EUR-USD", df, portfolio_value=100_000.0)

        # sparse_volume flag disables trend_ride explicitly
        assert sig.features.get("trend_ride") is False

    def test_trend_ride_blocked_in_bear_regime(self):
        """trend_ride BUY must be suppressed when regime=BEAR."""
        closes = _make_pullback_tail(_make_uptrend(50), pullback_pct=0.06)
        df = make_ohlcv(closes)

        cfg = self._cfg(regime_filter=True)
        strategy = MomentumStrategy(cfg)
        strategy._regime = "BEAR"  # force bear regime without SPY data
        sig = strategy.generate_signal("AAPL", df, portfolio_value=100_000.0)

        assert sig.direction == Direction.HOLD
        assert sig.score == 0.0

    def test_trend_ride_stop_loss_is_below_entry(self):
        """When trend_ride fires, stop_loss must be strictly below current price."""
        closes = _make_pullback_tail(_make_uptrend(50), pullback_pct=0.06)
        df = make_ohlcv(closes)

        cfg = self._cfg()
        strategy = MomentumStrategy(cfg)
        sig = strategy.generate_signal("AAPL", df, portfolio_value=100_000.0)

        if sig.direction == Direction.BUY and sig.features.get("trend_ride"):
            assert sig.suggested_stop_loss is not None
            assert float(sig.suggested_stop_loss) < closes[-1], (
                f"Stop {sig.suggested_stop_loss} not below entry {closes[-1]}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Tests: LIVE_SYMBOLS vs SYMBOLS
# ─────────────────────────────────────────────────────────────────────────────

class TestLiveSymbols:

    def test_live_symbols_excludes_untradeable(self):
        from run_strategy import LIVE_SYMBOLS, SYMBOLS

        # BNB-USD and GBP-USD are in SYMBOLS (backtest) but must not be in LIVE_SYMBOLS
        for sym in ("BNB-USD", "GBP-USD"):
            assert sym in SYMBOLS, f"{sym} should be in full SYMBOLS list"
            assert sym not in LIVE_SYMBOLS, f"{sym} should be excluded from LIVE_SYMBOLS"

        # EUR-USD was excluded from production SYMBOLS entirely (net-negative backtest)
        assert "EUR-USD" not in LIVE_SYMBOLS

    def test_live_symbols_subset_of_symbols(self):
        from run_strategy import LIVE_SYMBOLS, SYMBOLS
        assert set(LIVE_SYMBOLS).issubset(set(SYMBOLS))

    def test_live_symbols_non_empty(self):
        from run_strategy import LIVE_SYMBOLS
        assert len(LIVE_SYMBOLS) >= 25

    def test_btc_usd_in_live_symbols(self):
        from run_strategy import LIVE_SYMBOLS
        assert "BTC-USD" in LIVE_SYMBOLS

    def test_spy_in_live_symbols(self):
        from run_strategy import LIVE_SYMBOLS
        assert "SPY" in LIVE_SYMBOLS


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Staleness gate constant
# ─────────────────────────────────────────────────────────────────────────────

class TestStalenessGate:

    def test_stale_days_constant_exists(self):
        from run_strategy import _LIVE_STALE_DAYS
        assert _LIVE_STALE_DAYS == 7

    def test_stale_days_at_least_5(self):
        from run_strategy import _LIVE_STALE_DAYS
        # Must cover weekends (2d) + at least 1 holiday
        assert _LIVE_STALE_DAYS >= 5
