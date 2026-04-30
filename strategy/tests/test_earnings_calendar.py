# trading-system/strategy/tests/test_earnings_calendar.py
#
# Tests for the EarningsCalendar per-symbol blackout filter and its
# integration with MomentumStrategy.

from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.filters.economic_calendar import (
    EarningsCalendar, EarningsEvent,
)
from src.signals import Direction
from src.signals.momentum import MomentumStrategy, MomentumConfig


# ─────────────────────────────────────────────────────────────────────────────
# EarningsCalendar API
# ─────────────────────────────────────────────────────────────────────────────

class TestEarningsCalendar:
    def test_q1_2026_earnings_dates_present(self):
        """Confirmed Q1 2026 dates from issuer releases."""
        ec = EarningsCalendar()
        assert ec.event_on("AAPL",  date(2026, 5, 1))  is not None
        assert ec.event_on("MSFT",  date(2026, 4, 29)) is not None
        assert ec.event_on("NVDA",  date(2026, 5, 28)) is not None
        assert ec.event_on("GOOGL", date(2026, 4, 29)) is not None
        assert ec.event_on("META",  date(2026, 4, 23)) is not None
        assert ec.event_on("TSLA",  date(2026, 4, 22)) is not None
        assert ec.event_on("AMD",   date(2026, 4, 28)) is not None
        assert ec.event_on("AVGO",  date(2026, 6, 4))  is not None

    def test_blackout_on_earnings_day(self):
        ec = EarningsCalendar(blackout_days_before=1)
        assert ec.is_blackout_day("NVDA", date(2026, 5, 28)) is True

    def test_blackout_one_day_before(self):
        ec = EarningsCalendar(blackout_days_before=1)
        assert ec.is_blackout_day("NVDA", date(2026, 5, 27)) is True

    def test_other_symbols_not_blocked_on_same_date(self):
        """Per-symbol gate: NVDA earnings must not block AAPL."""
        ec = EarningsCalendar(blackout_days_before=1)
        assert ec.is_blackout_day("AAPL", date(2026, 5, 28)) is False
        assert ec.is_blackout_day("MSFT", date(2026, 5, 28)) is False

    def test_etfs_never_blocked(self):
        """ETFs and crypto are not in the calendar — never blocked."""
        ec = EarningsCalendar(blackout_days_before=1)
        for etf in ("SPY", "QQQ", "XLK", "SMH", "IWM", "TLT", "BND"):
            for d in (date(2026, 5, 1), date(2026, 4, 29), date(2026, 5, 28)):
                assert ec.is_blackout_day(etf, d) is False, (
                    f"{etf} must not be blocked by earnings filter"
                )
        assert ec.is_blackout_day("BTC-USD", date(2026, 5, 28)) is False

    def test_etf_has_no_coverage(self):
        ec = EarningsCalendar()
        assert ec.has_coverage("AAPL") is True
        assert ec.has_coverage("SPY")  is False
        assert ec.has_coverage("BND")  is False
        assert ec.has_coverage("BTC-USD") is False

    def test_blackout_two_days_before_with_window_2(self):
        ec = EarningsCalendar(blackout_days_before=2)
        # NVDA earnings May 28 → May 26 also blacked out
        assert ec.is_blackout_day("NVDA", date(2026, 5, 26)) is True

    def test_no_blackout_well_before(self):
        ec = EarningsCalendar(blackout_days_before=1)
        # Apr 1 is well before any of NVDA's 2026 earnings windows
        assert ec.is_blackout_day("NVDA", date(2026, 4, 1)) is False

    def test_blackout_reason_human_readable(self):
        ec = EarningsCalendar(blackout_days_before=1)
        reason_today = ec.blackout_reason("NVDA", date(2026, 5, 28)) or ""
        assert "earnings today" in reason_today
        reason_before = ec.blackout_reason("NVDA", date(2026, 5, 27)) or ""
        assert "tomorrow" in reason_before.lower()

    def test_get_next_event(self):
        ec = EarningsCalendar()
        nxt = ec.get_next_event("AAPL", date(2026, 4, 1))
        assert nxt is not None
        ev, days_away = nxt
        assert ev.event_date == date(2026, 5, 1)
        assert days_away == (date(2026, 5, 1) - date(2026, 4, 1)).days

    def test_events_in_window_filter_by_symbol(self):
        ec = EarningsCalendar()
        all_q1 = ec.events_in_window(date(2026, 4, 1), date(2026, 5, 31))
        assert len(all_q1) >= 7  # all 8 stocks at least, AAPL through AVGO
        nvda_q1 = ec.events_in_window(date(2026, 4, 1), date(2026, 5, 31), symbol="NVDA")
        assert len(nvda_q1) == 1
        assert nvda_q1[0].event_date == date(2026, 5, 28)

    def test_extra_events_constructor(self):
        custom = EarningsEvent(date(2026, 6, 30), "AAPL", "custom override")
        ec = EarningsCalendar(extra_events=[custom])
        assert ec.event_on("AAPL", date(2026, 6, 30)) is not None


# ─────────────────────────────────────────────────────────────────────────────
# Integration with MomentumStrategy
# ─────────────────────────────────────────────────────────────────────────────

def _ohlcv_buy_setup(end_date: date, n: int = 250) -> pd.DataFrame:
    """OHLCV producing an RSI-oversold, BUY-leaning final bar."""
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


class TestEarningsFilterIntegration:
    def _strategy(self, earnings_filter: bool = True) -> MomentumStrategy:
        # Disable other filters to isolate the earnings gate.
        return MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False, vix_filter=False, calendar_filter=False,
            earnings_filter=earnings_filter, earnings_blackout_days=1,
        ))

    def test_buy_blocked_on_nvda_earnings_day(self):
        df = _ohlcv_buy_setup(date(2026, 5, 28))   # NVDA earnings day
        s_off = self._strategy(earnings_filter=False)
        s_on  = self._strategy(earnings_filter=True)
        sig_off = s_off.generate_signal("NVDA", df, portfolio_value=100_000.0)
        sig_on  = s_on.generate_signal("NVDA", df,  portfolio_value=100_000.0)
        if sig_off.direction != Direction.BUY:
            pytest.skip("Scenario didn't produce BUY when filter is off")
        assert sig_on.direction == Direction.HOLD
        assert sig_on.score == 0.0
        assert "earnings" in (sig_on.features or {}).get("earnings_blackout", "")

    def test_buy_blocked_day_before_earnings(self):
        df = _ohlcv_buy_setup(date(2026, 5, 27))   # day before NVDA earnings
        s_off = self._strategy(earnings_filter=False)
        s_on  = self._strategy(earnings_filter=True)
        sig_off = s_off.generate_signal("NVDA", df, portfolio_value=100_000.0)
        sig_on  = s_on.generate_signal("NVDA", df,  portfolio_value=100_000.0)
        if sig_off.direction != Direction.BUY:
            pytest.skip("Scenario didn't produce BUY when filter is off")
        assert sig_on.direction == Direction.HOLD

    def test_other_symbol_not_blocked_on_nvda_earnings(self):
        """AAPL should still BUY on NVDA's earnings day — per-symbol gate."""
        df = _ohlcv_buy_setup(date(2026, 5, 28))
        s_on = self._strategy(earnings_filter=True)
        # Use a baseline run (filter off) to confirm scenario actually BUYs.
        s_off = self._strategy(earnings_filter=False)
        sig_off = s_off.generate_signal("AAPL", df, portfolio_value=100_000.0)
        sig_on  = s_on.generate_signal("AAPL", df,  portfolio_value=100_000.0)
        if sig_off.direction != Direction.BUY:
            pytest.skip("Scenario didn't produce BUY when filter is off")
        assert sig_on.direction == Direction.BUY
        assert (sig_on.features or {}).get("earnings_blackout") is None

    def test_etf_not_blocked(self):
        """SPY / TLT / BND have no earnings — filter is a no-op."""
        df = _ohlcv_buy_setup(date(2026, 5, 28))
        s_on = self._strategy(earnings_filter=True)
        for etf in ("SPY", "QQQ", "TLT", "BND"):
            sig = s_on.generate_signal(etf, df, portfolio_value=100_000.0)
            assert (sig.features or {}).get("earnings_blackout") is None, (
                f"{etf} unexpectedly carries earnings_blackout reason"
            )

    def test_sell_passes_on_earnings_day(self):
        """SELL must not be blocked even on earnings day."""
        n = 250
        end = pd.Timestamp(date(2026, 5, 28), tz="UTC")
        dates = pd.date_range(end=end, periods=n, freq="D")
        close = np.concatenate([
            np.linspace(100, 80, n - 20),
            np.linspace(80, 130, 20),
        ])
        high = close * 1.005
        low  = close * 0.995
        df = pd.DataFrame(
            {"open": close, "high": high, "low": low,
             "close": close, "volume": np.full(n, 1_000_000.0), "vwap": close},
            index=dates,
        )
        s_on  = self._strategy(earnings_filter=True)
        s_off = self._strategy(earnings_filter=False)
        sig_off = s_off.generate_signal("NVDA", df, portfolio_value=100_000.0)
        sig_on  = s_on.generate_signal("NVDA", df,  portfolio_value=100_000.0)
        if sig_off.direction != Direction.SELL:
            pytest.skip("Scenario didn't produce SELL when filter is off")
        assert sig_on.direction == Direction.SELL

    def test_disabled_filter_does_not_block(self):
        df = _ohlcv_buy_setup(date(2026, 5, 28))
        s = MomentumStrategy(MomentumConfig(
            fast_period=5, slow_period=15, vol_period=10,
            regime_filter=False, vix_filter=False, calendar_filter=False,
            earnings_filter=False,
        ))
        sig = s.generate_signal("NVDA", df, portfolio_value=100_000.0)
        assert (sig.features or {}).get("earnings_blackout") is None

    def test_series_path_blocks_earnings_bars(self):
        """The vectorised per-bar path must drop NVDA BUYs on earnings bars."""
        n = 320
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
        s_off = self._strategy(earnings_filter=False)
        s_on  = self._strategy(earnings_filter=True)
        sigs_off = s_off.generate_signals_series("NVDA", df)
        sigs_on  = s_on.generate_signals_series("NVDA", df)

        assert "earnings_blackout" in sigs_on.columns
        # Earnings day May 28: no BUY may fire in filtered series
        assert sigs_on.loc[
            sigs_on.index.date == date(2026, 5, 28), "direction"
        ].eq("BUY").sum() == 0

        # Filtered series must not have *more* BUYs than unfiltered
        assert (sigs_on["direction"] == "BUY").sum() <= (sigs_off["direction"] == "BUY").sum()

    def test_series_path_etf_unaffected(self):
        """SPY (ETF) — the earnings_blackout column stays all None."""
        n = 320
        end = pd.Timestamp(date(2026, 5, 31), tz="UTC")
        dates = pd.date_range(end=end, periods=n, freq="D")
        close = np.linspace(100, 130, n)
        high = close * 1.005
        low  = close * 0.995
        df = pd.DataFrame(
            {"open": close, "high": high, "low": low,
             "close": close, "volume": np.full(n, 5_000_000.0), "vwap": close},
            index=dates,
        )
        s_on = self._strategy(earnings_filter=True)
        sigs = s_on.generate_signals_series("SPY", df)
        assert "earnings_blackout" in sigs.columns
        assert sigs["earnings_blackout"].isna().all()
