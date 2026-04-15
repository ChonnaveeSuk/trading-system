#!/usr/bin/env python3
# trading-system/scripts/seed_alpaca.py
#
# Download daily OHLCV from Alpaca Markets and upsert into PostgreSQL.
# Drop-in replacement for seed_yfinance.py for Cloud Run environments
# where Yahoo Finance blocks GCP IP ranges.
#
# Credentials:
#   - ALPACA_API_KEY / ALPACA_SECRET_KEY env vars
#   - OR: GCP Secret Manager alpaca-api-key / alpaca-secret-key
#
# Unsupported symbols (GBP-USD, BNB-USD) are skipped with a warning;
# existing data for those symbols is preserved in the DB.
#
# Usage:
#   python3 scripts/seed_alpaca.py                   # all symbols, 400 days
#   python3 scripts/seed_alpaca.py --symbols AAPL BTC-USD
#   python3 scripts/seed_alpaca.py --days 7           # daily refresh
#
# After running, verify:
#   psql $DATABASE_URL -c "SELECT symbol, COUNT(*), MAX(timestamp)::date FROM ohlcv GROUP BY symbol ORDER BY symbol;"

from __future__ import annotations

import argparse
import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.src.data.alpaca_fetcher import AlpacaFetcher, _ALPACA_UNSUPPORTED

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("seed_alpaca")

# All 31 production symbols (matches CLAUDE.md capital simulation)
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
        description="Download OHLCV from Alpaca Markets and seed PostgreSQL"
    )
    parser.add_argument(
        "--symbols", nargs="+", default=DEFAULT_SYMBOLS,
        help="Symbols to fetch (default: all 31 production symbols)",
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS,
        help=f"Calendar days to look back (default: {DEFAULT_DAYS})",
    )
    args = parser.parse_args()

    fetcher = AlpacaFetcher()
    total   = 0
    ok      = []
    skipped = []
    failed  = []

    unsupported = [s for s in args.symbols if s in _ALPACA_UNSUPPORTED]
    supported   = [s for s in args.symbols if s not in _ALPACA_UNSUPPORTED]

    print(f"\nFetching {args.days} days of OHLCV for {len(args.symbols)} symbols via Alpaca…")
    if unsupported:
        print(f"  Note: {unsupported} not available on Alpaca — existing DB data kept.\n")

    try:
        results = fetcher.fetch_and_store_all(args.symbols, days=args.days)
    except RuntimeError as exc:
        print(f"\n[FATAL] Cannot connect to Alpaca: {exc}")
        sys.exit(1)

    for symbol in args.symbols:
        rows = results.get(symbol, 0)
        if symbol in _ALPACA_UNSUPPORTED:
            print(f"  {symbol:<12} ⚠  not on Alpaca (kept existing data)")
            skipped.append(symbol)
        elif rows == 0:
            print(f"  {symbol:<12} ✗  0 rows (no data returned)")
            failed.append(symbol)
        else:
            print(f"  {symbol:<12} ✓  {rows} rows upserted")
            ok.append(symbol)
            total += rows

    print(f"\n{'─'*50}")
    print(f"Fetched:  {len(ok)}/{len(supported)} symbols  |  {total} total rows upserted")
    if skipped:
        print(f"Skipped:  {skipped} (no Alpaca data — yfinance data retained)")
    if failed:
        print(f"Failed:   {failed} (0 rows — investigate manually)")

    print("\nVerify:")
    print('  psql $DATABASE_URL -c "SELECT symbol, COUNT(*), MAX(timestamp)::date FROM ohlcv GROUP BY symbol ORDER BY symbol;"')

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
