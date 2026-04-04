# trading-system/strategy/src/data/__init__.py
"""
Data fetching and feature engineering pipeline.

Sources (in priority order):
  1. PostgreSQL (yfinance-seeded) — primary for backtesting and live strategy
  2. yfinance                    — used by scripts/seed_yfinance.py for ingestion
  3. BigQuery                    — historical archive (Phase 4)

Rules:
  - Never use float for price/volume columns — use Decimal or int
  - Always validate OHLC integrity: high >= low, high >= open/close
  - Survivorship bias: note that yfinance data has it; use with caution
"""

from decimal import Decimal

import pandas as pd


def validate_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Validate OHLCV integrity. Raises ValueError on bad data.

    Args:
        df: DataFrame with columns: open, high, low, close, volume

    Returns:
        The same DataFrame if valid.

    Raises:
        ValueError: If any bar fails integrity checks.
    """
    required_cols = {"open", "high", "low", "close", "volume"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {missing}")

    if (df["high"] < df["low"]).any():
        raise ValueError("Found bars where high < low")

    if (df["high"] < df["open"]).any() or (df["high"] < df["close"]).any():
        raise ValueError("Found bars where high < open or high < close")

    if (df["low"] > df["open"]).any() or (df["low"] > df["close"]).any():
        raise ValueError("Found bars where low > open or low > close")

    if (df["volume"] < 0).any():
        raise ValueError("Found negative volume")

    return df


# Phase 4: class BigQueryDataFetcher  — pull historical fills/signals for ML training
# Phase 4: class FeatureEngineer    — ATR, rolling Sharpe, cross-asset correlation
