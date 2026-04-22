# trading-system/strategy/src/data/alpaca_fetcher.py
#
# Alpaca Markets OHLCV fetcher for Cloud Run environments.
# Drop-in replacement for yfinance_fetcher.py — Yahoo Finance blocks GCP IPs.
#
# Data API: https://data.alpaca.markets
#   Stocks:  GET /v2/stocks/{symbol}/bars   (feed=iex — free/paper accounts)
#   Crypto:  GET /v1beta3/crypto/us/bars    (no feed param needed)
#
# Usage:
#   fetcher = AlpacaFetcher()
#   results = fetcher.fetch_and_store_all(["AAPL", "BTC-USD"], days=400)
#   # returns {symbol: row_count}
#
# Credentials: ALPACA_API_KEY / ALPACA_SECRET_KEY env vars or Secret Manager.
# DB: DATABASE_URL env var or default postgres://quantai:...@localhost:5432/quantai

from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import date, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_DATA_BASE = "https://data.alpaca.markets"
_GCP_PROJECT = os.environ.get("GCP_PROJECT_ID", "quantai-trading-paper")
_API_SLEEP_S = 0.25  # rate-limit: 200 req/min on paper

# Symbols not available on Alpaca — existing DB data kept when encountered.
# BNB-USD:  Binance Coin not listed on Alpaca
# GBP-USD:  FX — Alpaca paper does not support FX spot trading/data
# EUR-USD:  FX — same
_ALPACA_UNSUPPORTED = frozenset({"BNB-USD", "GBP-USD", "EUR-USD"})

# yfinance → Alpaca symbol mapping
# Stocks / ETFs pass through unchanged.
# Crypto: yfinance uses BTC-USD, Alpaca data API uses BTCUSD.
_YF_TO_ALPACA_STOCK = {}   # no mapping needed for stocks
_YF_TO_ALPACA_CRYPTO = {
    "BTC-USD":  "BTCUSD",
    "ETH-USD":  "ETHUSD",
    "SOL-USD":  "SOLUSD",
    "XRP-USD":  "XRPUSD",
}


def _yf_to_data_symbol(yf_symbol: str) -> Optional[str]:
    """Translate yfinance symbol to Alpaca data API symbol. None = unsupported."""
    if yf_symbol in _ALPACA_UNSUPPORTED:
        return None
    return _YF_TO_ALPACA_CRYPTO.get(yf_symbol, yf_symbol)


def _is_crypto(yf_symbol: str) -> bool:
    return yf_symbol in _YF_TO_ALPACA_CRYPTO


# ── Credential helpers ─────────────────────────────────────────────────────────

def _gcloud_secret(secret_id: str) -> Optional[str]:
    try:
        r = subprocess.run(
            ["gcloud", "secrets", "versions", "access", "latest",
             f"--secret={secret_id}", f"--project={_GCP_PROJECT}"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _load_credentials() -> tuple[str, str]:
    """Return (api_key, secret_key) from env vars or Secret Manager."""
    api_key = os.environ.get("ALPACA_API_KEY") or _gcloud_secret("alpaca-api-key")
    secret_key = os.environ.get("ALPACA_SECRET_KEY") or _gcloud_secret("alpaca-secret-key")

    if not api_key or not secret_key:
        try:
            import sys
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
            from strategy.src.gcp import get_secret
            if not api_key:
                api_key = get_secret("alpaca-api-key", _GCP_PROJECT)
            if not secret_key:
                secret_key = get_secret("alpaca-secret-key", _GCP_PROJECT)
        except Exception:
            pass

    if not api_key:
        raise RuntimeError("ALPACA_API_KEY not set and not found in Secret Manager")
    if not secret_key:
        raise RuntimeError("ALPACA_SECRET_KEY not set and not found in Secret Manager")
    return api_key, secret_key


# ── AlpacaFetcher ──────────────────────────────────────────────────────────────

class AlpacaFetcher:
    """Download daily OHLCV bars from Alpaca and upsert into PostgreSQL.

    Supports the 31 production symbols from CLAUDE.md.
    Skips symbols in _ALPACA_UNSUPPORTED (keeps existing DB rows).
    """

    def __init__(self) -> None:
        api_key, secret_key = _load_credentials()
        self._session = requests.Session()
        self._session.headers.update({
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
        })
        self._db_url = os.environ.get(
            "DATABASE_URL",
            "postgres://quantai:quantai_dev_2026@localhost:5432/quantai",
        )

    # ── Alpaca data API ───────────────────────────────────────────────────────

    def _fetch_stock_bars(
        self, alpaca_symbol: str, start: str, end: str
    ) -> list[dict]:
        """GET /v2/stocks/{symbol}/bars (feed=iex, timeframe=1Day)."""
        bars: list[dict] = []
        url = f"{_DATA_BASE}/v2/stocks/{alpaca_symbol}/bars"
        params: dict = {
            "timeframe": "1Day",
            "start": start,
            "end": end,
            "feed": "iex",
            "limit": 10000,
            "adjustment": "raw",
        }
        while True:
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            page_bars = data.get("bars") or []
            bars.extend(page_bars)
            next_token = data.get("next_page_token")
            if not next_token:
                break
            params["page_token"] = next_token
            time.sleep(_API_SLEEP_S)
        return bars

    def _fetch_crypto_bars(
        self, alpaca_symbol: str, start: str, end: str
    ) -> list[dict]:
        """GET /v1beta3/crypto/us/bars (timeframe=1D).

        Note: v1beta3 uses '1D' (short format), not '1Day' like the v2 stocks API.
        """
        bars: list[dict] = []
        url = f"{_DATA_BASE}/v1beta3/crypto/us/bars"
        params: dict = {
            "symbols": alpaca_symbol,
            "timeframe": "1D",
            "start": f"{start}T00:00:00Z",
            "end": f"{end}T00:00:00Z",
            "limit": 1000,
        }
        while True:
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            page_bars = (data.get("bars") or {}).get(alpaca_symbol, [])
            bars.extend(page_bars)
            next_token = data.get("next_page_token")
            if not next_token:
                break
            params["page_token"] = next_token
            time.sleep(_API_SLEEP_S)
        return bars

    def _bars_to_rows(
        self, yf_symbol: str, bars: list[dict]
    ) -> list[tuple]:
        """Convert Alpaca bar dicts to PostgreSQL UPSERT rows.

        Returns list of (symbol, timestamp, open, high, low, close, volume, vwap).
        Strips Saturday/Sunday bars for FX/non-crypto instruments.
        """
        rows = []
        is_fx = "-" in yf_symbol and not _is_crypto(yf_symbol)
        for bar in bars:
            ts_str = bar.get("t", "")
            if not ts_str:
                continue
            # Normalize timezone: Alpaca returns RFC3339 e.g. "2024-01-02T00:00:00Z"
            if ts_str.endswith("Z"):
                ts_str = ts_str[:-1] + "+00:00"
            from datetime import datetime, timezone
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            # Strip weekends for FX (sparse volume issue from Phase 3)
            if is_fx and ts.weekday() >= 5:
                continue
            rows.append((
                yf_symbol,
                ts,
                str(bar.get("o", 0)),
                str(bar.get("h", 0)),
                str(bar.get("l", 0)),
                str(bar.get("c", 0)),
                str(bar.get("v", 0)),
                str(bar.get("vw", bar.get("c", 0))),  # vwap (not available for crypto)
            ))
        return rows

    # ── PostgreSQL upsert ─────────────────────────────────────────────────────

    def _upsert(self, rows: list[tuple]) -> int:
        """UPSERT rows into ohlcv table. Returns number of rows inserted/updated."""
        if not rows:
            return 0
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(self._db_url)
        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO ohlcv
                        (symbol, timestamp, open, high, low, close, volume, vwap)
                    VALUES %s
                    ON CONFLICT (symbol, timestamp) DO UPDATE SET
                        open   = EXCLUDED.open,
                        high   = EXCLUDED.high,
                        low    = EXCLUDED.low,
                        close  = EXCLUDED.close,
                        volume = EXCLUDED.volume,
                        vwap   = EXCLUDED.vwap
                    """,
                    rows,
                    template="(%s, %s, %s, %s, %s, %s, %s, %s)",
                )
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch_and_store(self, yf_symbol: str, days: int = 400) -> int:
        """Fetch and upsert OHLCV for one symbol. Returns row count."""
        if yf_symbol in _ALPACA_UNSUPPORTED:
            logger.info("%s: unsupported on Alpaca — skipped", yf_symbol)
            return 0

        alpaca_symbol = _yf_to_data_symbol(yf_symbol)
        if alpaca_symbol is None:
            logger.warning("%s: no Alpaca symbol mapping — skipped", yf_symbol)
            return 0

        end_date = date.today().isoformat()
        start_date = (date.today() - timedelta(days=days)).isoformat()

        try:
            if _is_crypto(yf_symbol):
                raw_bars = self._fetch_crypto_bars(alpaca_symbol, start_date, end_date)
            else:
                raw_bars = self._fetch_stock_bars(alpaca_symbol, start_date, end_date)
        except requests.HTTPError as e:
            logger.warning(
                "%s: Alpaca data API error %s: %s",
                yf_symbol,
                e.response.status_code if e.response else "?",
                e.response.text[:200] if e.response else str(e),
            )
            return 0
        except Exception as e:
            logger.warning("%s: fetch error: %s", yf_symbol, e)
            return 0

        if not raw_bars:
            logger.warning("%s: no bars returned from Alpaca", yf_symbol)
            return 0

        rows = self._bars_to_rows(yf_symbol, raw_bars)
        count = self._upsert(rows)
        logger.info("%s: %d bars upserted", yf_symbol, count)
        time.sleep(_API_SLEEP_S)
        return count

    def fetch_and_store_all(
        self, symbols: list[str], days: int = 400
    ) -> dict[str, int]:
        """Fetch and upsert OHLCV for all symbols.

        Returns {symbol: row_count}.
        Unsupported symbols return 0 (existing DB rows preserved).
        """
        results: dict[str, int] = {}
        for symbol in symbols:
            if symbol in _ALPACA_UNSUPPORTED:
                results[symbol] = 0
                continue
            count = self.fetch_and_store(symbol, days=days)
            results[symbol] = count
        return results
