# trading-system/strategy/src/data/yfinance_fetcher.py
#
# yfinance → PostgreSQL UPSERT.
#
# Used ONLY for initial backtesting data (Phase 3).
# In Phase 4 (live), data comes from IBKR TWS.
#
# Symbol mapping:
#   AAPL    → "AAPL"      (equity)
#   BTC-USD → "BTC-USD"   (crypto, 7 days/week)
#   ETH-USD → "ETH-USD"   (crypto, 7 days/week)
#   EUR-USD → "EURUSD=X"  (forex pair — yfinance uses =X suffix)
#   GBP-USD → "GBPUSD=X"  (forex pair)
#   SPY     → "SPY"       (S&P 500 ETF)
#   QQQ     → "QQQ"       (Nasdaq 100 ETF)
#   NVDA    → "NVDA"      (equity)
#   MSFT    → "MSFT"      (equity)
#
# Warning: yfinance data has survivorship bias. Do NOT use for live decisions.

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.extras

from . import validate_ohlcv

logger = logging.getLogger(__name__)

_DEFAULT_DSN = os.environ.get(
    "DATABASE_URL",
    "postgres://quantai:quantai_dev_2026@localhost:5432/quantai",
)

# Internal yfinance tickers for each symbol stored in the DB
_YFINANCE_TICKER: dict[str, str] = {
    "AAPL":    "AAPL",
    "BTC-USD": "BTC-USD",
    "ETH-USD": "ETH-USD",
    "EUR-USD": "EURUSD=X",
    "GBP-USD": "GBPUSD=X",
    "SPY":     "SPY",
    "QQQ":     "QQQ",
    "NVDA":    "NVDA",
    "MSFT":    "MSFT",
    "META":    "META",
    "AMZN":    "AMZN",
    "TSLA":    "TSLA",
    "GLD":     "GLD",
    "SLV":     "SLV",
    "XLE":     "XLE",
    "XLK":     "XLK",
    "XLF":     "XLF",
    "TLT":     "TLT",
    "IEF":     "IEF",
    "GDX":     "GDX",
    "GDXJ":    "GDXJ",
    "USO":     "USO",
    "DBC":     "DBC",
    "SOL-USD": "SOL-USD",
    "XRP-USD": "XRP-USD",
    "NEM":     "NEM",
    "WPM":     "WPM",
    "SILJ":    "SILJ",
    "FCX":     "FCX",
    "SOXX":    "SOXX",
    "IAU":     "IAU",
    "BNB-USD": "BNB-USD",
    "IWM":     "IWM",
    "EEM":     "EEM",
    "COPX":    "COPX",
    "RING":    "RING",
    "PAAS":    "PAAS",
    "AEM":     "AEM",
    "KGC":     "KGC",
    "AGI":     "AGI",
    "URA":     "URA",
    "URNM":    "URNM",
    "SCCO":    "SCCO",
    "MP":      "MP",
    "GOLD":    "GOLD",
    "CDE":     "CDE",
    "HL":      "HL",
    "MAG":     "MAG",
    "RGLD":    "RGLD",
}


class YfinanceFetcher:
    """Download OHLCV from yfinance and upsert into the local PostgreSQL ohlcv table.

    Usage::

        fetcher = YfinanceFetcher()
        rows = fetcher.fetch_and_store("AAPL", days=400)
        print(f"Upserted {rows} rows")
    """

    def __init__(self, dsn: str = _DEFAULT_DSN) -> None:
        self._dsn = dsn

    def fetch_and_store(
        self,
        symbol: str,
        days: int = 400,
        end_date: Optional[date] = None,
    ) -> int:
        """Download `days` of daily OHLCV for `symbol` and upsert into PostgreSQL.

        Args:
            symbol:   DB symbol key, e.g. "AAPL", "BTC-USD", "EUR-USD".
            days:     Calendar days to look back from end_date.
            end_date: Last date (inclusive). Defaults to yesterday (yfinance
                      sometimes excludes today's partial bar).

        Returns:
            Number of rows upserted.

        Raises:
            ValueError: If the symbol is not in the supported symbol map.
            RuntimeError: If yfinance returns empty data.
        """
        import yfinance as yf

        if symbol not in _YFINANCE_TICKER:
            raise ValueError(
                f"Unknown symbol '{symbol}'. "
                f"Supported: {list(_YFINANCE_TICKER.keys())}"
            )

        ticker = _YFINANCE_TICKER[symbol]
        end = end_date or (date.today() - timedelta(days=1))
        start = end - timedelta(days=days)

        logger.info(
            "Downloading %s (yf: %s)  %s → %s",
            symbol, ticker, start, end,
        )

        raw = yf.download(
            ticker,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),  # yfinance end is exclusive
            interval="1d",
            auto_adjust=True,
            progress=False,
        )

        if raw.empty:
            raise RuntimeError(
                f"yfinance returned no data for {ticker} ({start} → {end})"
            )

        df = self._normalize(raw, symbol)
        validate_ohlcv(df)

        rows = self._upsert(df, symbol)
        logger.info("Upserted %d rows for %s", rows, symbol)
        return rows

    # ── Internal ──────────────────────────────────────────────────────────────

    def _normalize(self, raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Normalize a yfinance DataFrame to match the ohlcv table schema."""
        # yfinance returns MultiIndex columns when auto_adjust=True;
        # flatten if necessary
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)

        df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.columns = ["open", "high", "low", "close", "volume"]

        # Ensure UTC timezone
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")

        df.index.name = "timestamp"

        # vwap approximation: (open + high + low + close) / 4
        # Repair OHLC integrity — yfinance forex data occasionally has
        # high < close or low > open due to rounding / feed quirks.
        df["high"] = df[["open", "high", "low", "close"]].max(axis=1)
        df["low"]  = df[["open", "high", "low", "close"]].min(axis=1)

        df["vwap"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4

        # Drop rows with NaN (some forex pairs have weekend gaps)
        df = df.dropna(subset=["open", "high", "low", "close"])

        # Ensure volume is non-negative (forex pairs sometimes report 0)
        df["volume"] = df["volume"].clip(lower=0)

        # Remove weekend bars for FX instruments.
        # Spot FX has near-zero liquidity on Saturdays/Sundays and yfinance
        # frequently returns corrupted prices for those bars (different session
        # source or stale mid-price).  Only equities and crypto trade 7 days/week.
        is_forex = (df["volume"] == 0).sum() / max(len(df), 1) > 0.5
        if is_forex:
            df = df[df.index.dayofweek < 5]  # Monday=0 … Friday=4

        return df.sort_index()

    def _upsert(self, df: pd.DataFrame, symbol: str) -> int:
        """UPSERT rows into the ohlcv table. Existing rows are overwritten."""
        sql = """
            INSERT INTO ohlcv (symbol, timestamp, open, high, low, close, volume, vwap)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol, timestamp) DO UPDATE SET
                open   = EXCLUDED.open,
                high   = EXCLUDED.high,
                low    = EXCLUDED.low,
                close  = EXCLUDED.close,
                volume = EXCLUDED.volume,
                vwap   = EXCLUDED.vwap
        """
        rows = 0
        conn = None
        try:
            conn = psycopg2.connect(self._dsn)
            with conn:
                with conn.cursor() as cur:
                    for ts, row in df.iterrows():
                        cur.execute(sql, (
                            symbol,
                            ts.to_pydatetime(),
                            float(row["open"]),
                            float(row["high"]),
                            float(row["low"]),
                            float(row["close"]),
                            float(row["volume"]),
                            float(row["vwap"]),
                        ))
                        rows += 1
        finally:
            if conn is not None:
                conn.close()
        return rows
