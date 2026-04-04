# trading-system/strategy/src/data/fetcher.py
#
# PostgreSQL OHLCV fetcher — primary data source for paper trading phase.
#
# Design:
#   - Uses psycopg2 (synchronous) — strategy layer is not latency-critical.
#   - Returns DataFrames with Decimal columns preserved as float64 for
#     numpy/pandas compatibility (conversion happens here, not in strategy).
#   - Validates OHLCV integrity before returning (ADR-001).
#
# Phase 2 extension points:
#   - IbkrDataFetcher: same interface, pulls from TWS API.
#   - BigQueryFetcher: for historical ML training data.

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

# Default connection: mirrors .env / Docker Compose setup
_DEFAULT_DSN = os.environ.get(
    "DATABASE_URL",
    "postgres://quantai:quantai_dev_2026@localhost:5432/quantai",
)


class PostgresOhlcvFetcher:
    """Fetch OHLCV bars from the local PostgreSQL hot store.

    The DB holds 30 days of daily bars per symbol (seeded by scripts/seed_ohlcv.sql).
    Phase 2 extension: swap this with IbkrDataFetcher for live data.

    Usage::

        fetcher = PostgresOhlcvFetcher()
        df = fetcher.fetch("AAPL", days=30)
        # df: DatetimeIndex, columns=[open,high,low,close,volume,vwap]
    """

    def __init__(self, dsn: str = _DEFAULT_DSN) -> None:
        self._dsn = dsn
        self._conn: Optional[psycopg2.extensions.connection] = None

    # ── Connection management ─────────────────────────────────────────────────

    def connect(self) -> None:
        """Open the database connection."""
        self._conn = psycopg2.connect(self._dsn)
        logger.info("PostgresOhlcvFetcher: connected to PostgreSQL")

    def disconnect(self) -> None:
        """Close the database connection."""
        if self._conn and not self._conn.closed:
            self._conn.close()
            logger.info("PostgresOhlcvFetcher: disconnected")

    def __enter__(self) -> "PostgresOhlcvFetcher":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()

    def _ensure_connected(self) -> psycopg2.extensions.connection:
        if self._conn is None or self._conn.closed:
            self.connect()
        return self._conn  # type: ignore[return-value]

    # ── Fetch ─────────────────────────────────────────────────────────────────

    def fetch(
        self,
        symbol: str,
        days: int = 30,
        end_date: Optional[date] = None,
    ) -> pd.DataFrame:
        """Fetch daily OHLCV bars for `symbol`.

        Args:
            symbol:   Ticker, e.g. "AAPL", "BTC-USD", "EUR-USD".
            days:     Number of calendar days to look back.
            end_date: Last date (inclusive). Defaults to today.

        Returns:
            DataFrame sorted by timestamp ascending, index=timestamp (UTC).
            Columns: open, high, low, close, volume, vwap (float64).
            Empty DataFrame if no data found.

        Raises:
            ValueError: If OHLCV integrity check fails.
        """
        conn = self._ensure_connected()
        end = end_date or date.today()
        start = end - timedelta(days=days)

        sql = """
            SELECT
                timestamp,
                open::float8   AS open,
                high::float8   AS high,
                low::float8    AS low,
                close::float8  AS close,
                volume::float8 AS volume,
                vwap::float8   AS vwap
            FROM ohlcv
            WHERE symbol = %s
              AND timestamp >= %s
              AND timestamp <= %s
            ORDER BY timestamp ASC
        """

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (symbol, start, end))
            rows = cur.fetchall()

        if not rows:
            logger.warning("No OHLCV data found for %s (%s → %s)", symbol, start, end)
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume", "vwap"]
            )

        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp").sort_index()

        validate_ohlcv(df)
        logger.info(
            "Fetched %d bars for %s (%s → %s)",
            len(df),
            symbol,
            df.index[0].date(),
            df.index[-1].date(),
        )
        return df

    def fetch_latest_close(self, symbol: str) -> Optional[float]:
        """Return the most recent closing price for a symbol.

        Used by the strategy layer to get the current reference price
        for the risk check (current_price field in SignalRequest).
        """
        conn = self._ensure_connected()
        sql = """
            SELECT close::float8
            FROM ohlcv
            WHERE symbol = %s
            ORDER BY timestamp DESC
            LIMIT 1
        """
        with conn.cursor() as cur:
            cur.execute(sql, (symbol,))
            row = cur.fetchone()
        return row[0] if row else None

    def available_symbols(self) -> list[str]:
        """List all symbols that have data in the OHLCV table."""
        conn = self._ensure_connected()
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT symbol FROM ohlcv ORDER BY symbol")
            return [row[0] for row in cur.fetchall()]
