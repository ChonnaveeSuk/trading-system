# trading-system/strategy/tests/test_economic_calendar.py
#
# Tests for the EconomicCalendar blackout filter.

from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from src.filters.economic_calendar import (
    EconomicCalendar, EconomicEvent, EventKind,
)
from src.signals import Direction
from src.signals.momentum import MomentumStrategy, MomentumConfig


# ─────────────────────────────────────────────────────────────────────────────
# EconomicCalendar API
# ─────────────────────────────────────────────────────────────────────────────

class TestEconomicCalendar:
    def test_fomc_2026_present(self):
        cal = EconomicCalendar()
        # FOMC May 7 2026 is the rate-decision day in our calendar
        ev = cal.event_on(date(2026, 5, 7))
        assert ev is not None
        assert ev.kind == EventKind.FOMC

    def test_blackout_event_day_itself(self):
        cal = EconomicCalendar(blackout_days_before=1)
        assert cal.is_blackout_day(date(2026, 5, 7)) is True   # FOMC day

    def test_blackout_one_day_before(self):
        cal = EconomicCalendar(blackout_days_before=1)
        assert cal.is_blackout_day(date(2026, 5, 6)) is True   # day before FOMC

    def test_blackout_two_days_before_with_window_2(self):
        cal = EconomicCalendar(blackout_days_before=2)
        assert cal.is_blackout_day(date(2026, 5, 5)) is True

    def test_no_blackout_well_before(self):
        cal = EconomicCalendar(blackout_days_before=1)
        # Apr 20 is well before any May event and after the Apr 14 CPI window
        assert cal.is_blackout_day(date(2026, 4, 20)) is False

    def test_get_next_event_returns_distance(self):
        cal = EconomicCalendar()
        # On May 1 the next FOMC is May 7 (6 days away)
        nxt = cal.get_next_event(date(2026, 5, 1))
        assert nxt is not None
        ev, days_away = nxt
        assert days_away == (ev.event_date - date(2026, 5, 1)).days

    def test_get_next_event_zero_distance_on_event_day(self):
        cal = EconomicCalendar()
        nxt = cal.get_next_event(date(2026, 5, 7))
        assert nxt is not None
        _, days_away = nxt
        assert days_away == 0

    def test_blackout_reason_is_human_readable(self):
        cal = EconomicCalendar(blackout_days_before=1)
        assert "FOMC" in (cal.blackout_reason(date(2026, 5, 7)) or "")
        assert "tomorrow" in (cal.blackout_reason(date(2026, 5, 6)) or "").lower()

    def test_first_friday_nfp_auto_generated(self):
        cal = EconomicCalendar()
        # First Friday of May 2026 is May 1
        ev = cal.event_on(date(2026, 5, 1))
        assert ev is not None
        assert ev.kind == EventKind.NFP

    def test_extend_year_adds_nfp(self):
        cal = EconomicCalendar()
        # Without extension, no events in 2027
        assert cal.event_on(date(2027, 1, 1)) is None
        cal.extend_year(2027, fomc_dates=[date(2027, 1, 28)])
        # First Friday of Jan 2027 is Jan 1
        ev = cal.event_on(date(2027, 1, 1))
        assert ev is not None and ev.kind == EventKind.NFP
        # FOMC manually added
        ev2 = cal.event_on(date(2027, 1, 28))
        assert ev2 is not None and ev2.kind == EventKind.FOMC

    def test_extra_events_constructor(self):
        custom = EconomicEvent(date(2026, 5, 13), EventKind.CPI, "custom override")
        cal = EconomicCalendar(extra_events=[custom])
        ev = cal.event_on(date(2026, 5, 13))
        # Multiple events on same day → first match returned (insertion order
        # after sort).  Just assert the date is blacked out.
        assert ev is not None
        assert cal.is_blackout_day(date(2026, 5, 13))

    def test_events_sorted(self):
        cal = EconomicCalendar()
        dates = [ev.event_date for ev in cal.all_events]
        assert dates == sorted(dates)


# ─────────────────────────────────────────────────────────────────────────────
# Integration with MomentumStrategy
# ─────────────────────────────────────────────────────────────────────────────

def _ohlcv_ending_on(end_date: date, n: int = 250) -> pd.DataFrame:
    """OHLCV with the final bar dated `end_date`, RSI-oversold at the end."""
    dates = pd.date_range(end=pd.Timestamp(end_date, tz="UTC"), periods=n, freq="D")
    close = np.concatenate([
        np.linspace(100, 130, n - 20),
        np.linspace(130, 90, 20),
    ])
    high = close * 1.005
    low  = close * 0.995
    return pd.DataFrame(
        {"open": close, "high": high, "low": low,
         "close": close, "volume": np.full(n, 1_000_000.0), "vwap": close},
        index=dates,
    )


class TestCalendarFilterIntegration:
    """Calendar blackout suppresses BUY in single-bar and bar-by-bar paths."""

    def _strategy(self, calendar_filter: bool = True) -> MomentumStrategy:
        return MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False, vix_filter=False,
            calendar_filter=calendar_filter, calendar_blackout_days=1,
        ))

    def test_buy_blocked_on_fomc_day(self):
        """A scenario that would BUY produces HOLD on FOMC May 7."""
        df = _ohlcv_ending_on(date(2026, 5, 7))
        s_off = self._strategy(calendar_filter=False)
        s_on  = self._strategy(calendar_filter=True)
        sig_off = s_off.generate_signal("AAPL", df, portfolio_value=100_000.0)
        sig_on  = s_on.generate_signal("AAPL", df,  portfolio_value=100_000.0)
        if sig_off.direction != Direction.BUY:
            pytest.skip("Scenario didn't produce BUY when filter is off")
        assert sig_on.direction == Direction.HOLD
        assert sig_on.score == 0.0
        assert "FOMC" in (sig_on.features or {}).get("calendar_blackout", "")

    def test_buy_blocked_day_before_fomc(self):
        df = _ohlcv_ending_on(date(2026, 5, 6))   # day before FOMC May 7
        s_off = self._strategy(calendar_filter=False)
        s_on  = self._strategy(calendar_filter=True)
        sig_off = s_off.generate_signal("AAPL", df, portfolio_value=100_000.0)
        sig_on  = s_on.generate_signal("AAPL", df,  portfolio_value=100_000.0)
        if sig_off.direction != Direction.BUY:
            pytest.skip("Scenario didn't produce BUY when filter is off")
        assert sig_on.direction == Direction.HOLD

    def test_sell_passes_on_blackout(self):
        """SELL signals must not be blocked even on FOMC day."""
        # Build a scenario where SELL fires (RSI overbought at the end)
        n = 250
        end = pd.Timestamp(date(2026, 5, 7), tz="UTC")
        dates = pd.date_range(end=end, periods=n, freq="D")
        close = np.concatenate([
            np.linspace(100, 80, n - 20),    # downtrend
            np.linspace(80, 130, 20),         # parabolic ramp → RSI overbought
        ])
        high = close * 1.005
        low  = close * 0.995
        df = pd.DataFrame(
            {"open": close, "high": high, "low": low,
             "close": close, "volume": np.full(n, 1_000_000.0), "vwap": close},
            index=dates,
        )
        s_on = self._strategy(calendar_filter=True)
        s_off = self._strategy(calendar_filter=False)
        sig_off = s_off.generate_signal("AAPL", df, portfolio_value=100_000.0)
        sig_on  = s_on.generate_signal("AAPL", df,  portfolio_value=100_000.0)
        if sig_off.direction != Direction.SELL:
            pytest.skip("Scenario didn't produce SELL when filter is off")
        assert sig_on.direction == Direction.SELL  # SELL must still fire

    def test_disabled_filter_does_not_block(self):
        df = _ohlcv_ending_on(date(2026, 5, 7))
        s = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False, vix_filter=False, calendar_filter=False,
        ))
        # BUY should fire if the scenario is otherwise BUY-able
        sig = s.generate_signal("AAPL", df, portfolio_value=100_000.0)
        # Either BUY or HOLD (depending on signal stack), but the calendar
        # must not be the gating reason — calendar_blackout is None
        assert (sig.features or {}).get("calendar_blackout") is None

    def test_series_path_blocks_event_bars(self):
        # OHLCV that reliably produces some BUY signals across May 2026
        n = 300
        end = pd.Timestamp(date(2026, 5, 31), tz="UTC")
        dates = pd.date_range(end=end, periods=n, freq="D")
        close = np.concatenate([np.linspace(100, 140, n - 30),
                                np.linspace(140, 90, 30)])
        high = close * 1.005
        low  = close * 0.995
        df = pd.DataFrame(
            {"open": close, "high": high, "low": low,
             "close": close, "volume": np.full(n, 5_000_000.0), "vwap": close},
            index=dates,
        )
        s_off = self._strategy(calendar_filter=False)
        s_on  = self._strategy(calendar_filter=True)
        sigs_off = s_off.generate_signals_series("TEST", df)
        sigs_on  = s_on.generate_signals_series("TEST", df)

        # No BUY may fire on FOMC May 7 in the filtered series
        assert "calendar_blackout" in sigs_on.columns
        assert sigs_on.loc[
            sigs_on.index.date == date(2026, 5, 7), "direction"
        ].eq("BUY").sum() == 0

        # Filtered series has ≤ BUYs of the unfiltered series
        assert (sigs_on["direction"] == "BUY").sum() <= (sigs_off["direction"] == "BUY").sum()
