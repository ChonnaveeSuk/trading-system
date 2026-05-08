# trading-system/scripts/tests/test_monitoring.py
#
# Unit tests for monitoring and reporting logic in morning_report.py.
# Focuses on data processing, formatting, and edge-case handling.

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from morning_report import (
    ReportData,
    _format_obsidian_note,
    _query_stop_loss_risk,
    _query_sector_concentration,
)

# ── 1. Stop Loss Risk Logic ───────────────────────────────────────────────────

def test_query_stop_loss_risk_calculation():
    """Test that unrealized_pct and breached flag are calculated correctly."""
    # Mock database rows: (symbol, quantity, average_cost, unrealized_pnl)
    mock_rows = [
        ("AAPL", 10, 150.0, -150.0),  # -10% of $1500 cost basis
        ("TSLA", 5, 200.0, -20.0),    # -2% of $1000 cost basis
        ("NVDA", 20, 100.0, -60.0),   # -3% of $2000 cost basis
    ]

    with patch("morning_report._connect") as mock_connect:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchall.return_value = mock_rows

        # stop_pct=0.05 (5%), warn_pct=0.03 (3%)
        results = _query_stop_loss_risk(stop_pct=0.05, warn_pct=0.03)

        # TSLA (-2%) should be filtered out because it hasn't hit warn_pct (3%)
        assert len(results) == 2

        # Sorted worst-first: AAPL (-10%) then NVDA (-3%)
        assert results[0]["symbol"] == "AAPL"
        assert results[0]["unrealized_pct"] == -0.10
        assert results[0]["breached"] is True

        assert results[1]["symbol"] == "NVDA"
        assert results[1]["unrealized_pct"] == -0.03
        assert results[1]["breached"] is False

# ── 2. Sector Concentration Logic ─────────────────────────────────────────────

def test_query_sector_concentration_logic():
    """Test that sector notional and percentages are computed correctly."""
    # Mock database rows: (symbol, quantity, average_cost, unrealized_pnl)
    # AAPL, MSFT, NVDA are 'big_tech'
    # TSLA is 'growth'
    mock_rows = [
        ("AAPL", 10, 150.0, 0.0),   # $1500
        ("MSFT", 5, 300.0, 0.0),    # $1500
        ("NVDA", 1, 1000.0, 0.0),   # $1000 -> Big Tech Total: $4000
        ("TSLA", 5, 200.0, 0.0),    # $1000 -> Growth Total: $1000
    ]

    with patch("morning_report._connect") as mock_connect:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchall.return_value = mock_rows

        results = _query_sector_concentration()

        assert results["total_notional"] == 5000.0
        assert results["by_sector"]["big_tech"]["count"] == 3
        assert results["by_sector"]["big_tech"]["notional"] == 4000.0
        assert results["by_sector"]["growth"]["count"] == 1

        assert results["largest_sector"] == ("big_tech", 80.0)

# ── 3. Obsidian Note Formatting ───────────────────────────────────────────────

def test_format_obsidian_note():
    """Test the markdown output includes correct frontmatter and body."""
    data = ReportData(
        message="Test Message",
        level="SUMMARY",
        today=date(2026, 5, 7),
        regime="BULL",
        sharpe=1.23,
        trades=15,
        pnl_today=450.75
    )

    note = _format_obsidian_note(data)

    # Check frontmatter
    assert "date: 2026-05-07" in note
    assert "regime: BULL" in note
    assert "sharpe: 1.23" in note
    assert "trades: 15" in note
    assert 'pnl_today: "+$450.75"' in note

    # Check body
    assert "# 2026-05-07 \u2014 Morning Report" in note
    assert "```text\nTest Message\n```" in note
    assert "[[2026-05-06]]" in note

# ── 4. Regime Staleness Logic (Regression test for MomentumStrategy) ──────────

def test_regime_staleness_warning(caplog):
    """Test that MomentumStrategy warns when SPY data is stale."""
    from src.signals.momentum import MomentumStrategy, MomentumConfig
    import pandas as pd
    import numpy as np

    # Create stale data (8 days old)
    stale_date = date.today() - timedelta(days=8)
    ts = pd.to_datetime(stale_date)

    # Need at least 200 bars for regime filter
    dates = [ts - timedelta(days=i) for i in range(250)]
    df = pd.DataFrame({
        "close": [100.0] * 250,
        "volume": [1000] * 250
    }, index=reversed(dates))

    strat = MomentumStrategy(MomentumConfig(regime_ma_period=200))

    with caplog.at_level("WARNING"):
        regime = strat.update_regime(df)
        assert "stale" in caplog.text
        assert regime == "NEUTRAL" # Price 100, MA200 100 -> NEUTRAL
