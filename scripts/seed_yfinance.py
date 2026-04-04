#!/usr/bin/env python3
# trading-system/scripts/seed_yfinance.py
#
# Download 400 days of daily OHLCV from yfinance and upsert into PostgreSQL.
# Replaces (or augments) the 30-row synthetic seed from scripts/seed_ohlcv.sql.
#
# Usage:
#   python3 scripts/seed_yfinance.py               # all symbols, 400 days
#   python3 scripts/seed_yfinance.py --symbols AAPL BTC-USD
#   python3 scripts/seed_yfinance.py --days 252
#
# After running, verify:
#   psql $DATABASE_URL -c "SELECT symbol, COUNT(*), MIN(timestamp)::date, MAX(timestamp)::date FROM ohlcv GROUP BY symbol ORDER BY symbol;"

from __future__ import annotations

import argparse
import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.src.data.yfinance_fetcher import YfinanceFetcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("seed_yfinance")

DEFAULT_SYMBOLS = [
    "BTC-USD", "BNB-USD",
    "GLD", "IAU", "SLV",
    "GDX", "GDXJ", "RING", "PAAS", "SILJ", "WPM", "HL", "CDE",
    "NEM", "AEM", "AGI", "GOLD", "KGC",
    "URA", "URNM", "DBC", "SCCO", "MP",
    "SPY", "QQQ", "IWM", "XLK", "AAPL", "TLT", "EEM", "GBP-USD",
]
DEFAULT_DAYS = 400


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download OHLCV from yfinance and seed PostgreSQL"
    )
    parser.add_argument(
        "--symbols", nargs="+", default=DEFAULT_SYMBOLS,
        help="Symbols to fetch (default: AAPL BTC-USD EUR-USD)",
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS,
        help=f"Calendar days to look back (default: {DEFAULT_DAYS})",
    )
    args = parser.parse_args()

    fetcher = YfinanceFetcher()
    total = 0
    errors = []

    print(f"\nDownloading {args.days} days of OHLCV for: {', '.join(args.symbols)}\n")

    for symbol in args.symbols:
        try:
            rows = fetcher.fetch_and_store(symbol, days=args.days)
            print(f"  {symbol:<10} ✓  {rows} rows upserted")
            total += rows
        except Exception as e:
            print(f"  {symbol:<10} ✗  {e}")
            errors.append(symbol)

    print(f"\nTotal rows upserted: {total}")

    if errors:
        print(f"Failed symbols: {errors}")
        sys.exit(1)
    else:
        print("All symbols seeded successfully.\n")
        print("Verify with:")
        print("  psql $DATABASE_URL -c \"SELECT symbol, COUNT(*), MIN(timestamp)::date, MAX(timestamp)::date FROM ohlcv GROUP BY symbol ORDER BY symbol;\"")


if __name__ == "__main__":
    main()
