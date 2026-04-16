#!/usr/bin/env python3
# trading-system/strategy/run_strategy.py
#
# Phase 2 strategy runner:
#   1. Fetch OHLCV from PostgreSQL
#   2. Run momentum backtest (with explicit "dev mode" warning for 30-day data)
#   3. Generate current signal
#   4. Send to Rust execution engine via gRPC
#   5. Print portfolio status
#
# Usage:
#   # Backtest only (no live signal):
#   python run_strategy.py --mode backtest
#
#   # Send live signal to Rust OMS (Rust engine must be running):
#   python run_strategy.py --mode live
#
#   # Both:
#   python run_strategy.py --mode all

from __future__ import annotations

import argparse
import logging
import sys
import os

# Add the strategy directory to path when running as script
sys.path.insert(0, os.path.dirname(__file__))

from src.data.fetcher import PostgresOhlcvFetcher
from src.signals.momentum import MomentumStrategy, MomentumConfig
from src.backtester.engine import BacktestEngine
from src.backtester import BacktestConfig
from src.bridge.client import TradingBridgeClient
from src.signals import Direction

# When ALPACA_DIRECT=1, live signals go straight to Alpaca REST (no Rust gRPC needed).
# This is the Cloud Run path — Rust OMS cannot run there (no Redis, PaperBroker only).
_ALPACA_DIRECT = os.environ.get("ALPACA_DIRECT", "0") == "1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_strategy")

SYMBOLS = [
    "BTC-USD", "BNB-USD",
    "GLD", "IAU", "SLV",
    "GDX", "GDXJ", "RING", "PAAS", "SILJ", "WPM", "HL", "CDE",
    "NEM", "AEM", "AGI", "GOLD", "KGC",
    "URA", "URNM", "DBC", "SCCO", "MP",
    "SPY", "QQQ", "IWM", "XLK", "AAPL", "TLT", "EEM", "GBP-USD",
]
GRPC_HOST = os.environ.get("GRPC_HOST", "localhost")
GRPC_PORT = int(os.environ.get("GRPC_PORT", "50051"))


def run_backtest(symbols: list[str]) -> None:
    """Backtest the momentum strategy on all symbols.

    Auto-detects data volume:
      ≥315 bars (252 IS + 63 OOS): walk-forward with production MA params (10/30/20)
      <315 bars:                   single-pass with dev MA params (5/10/8), flagged DEV MODE
    """
    from src.backtester import BacktestConfig, WalkForwardSummary

    PROD_MIN = 315  # 252 IS + 63 OOS minimum for walk-forward

    engine = BacktestEngine(config=BacktestConfig(
        commission_per_share=0.005,
        slippage_bps=0.5,
    ))

    with PostgresOhlcvFetcher() as fetcher:
        for symbol in symbols:
            df = fetcher.fetch(symbol, days=700)  # fetch full seeded history
            if df.empty:
                print(f"  {symbol}: no data — skipping")
                continue

            bars = len(df)
            if bars >= PROD_MIN:
                # Production walk-forward — 5/15/10 generates more crossovers per
                # 63-day OOS window than the original 10/30/20 config
                strategy = MomentumStrategy(MomentumConfig(fast_period=5, slow_period=15, vol_period=10, bb_period=0))
                print("\n" + "=" * 70)
                print(f" WALK-FORWARD BACKTEST — {symbol}  ({bars} bars)")
                print("=" * 70)
                wf = engine.walk_forward(symbol, df, strategy)
                _print_walkforward(wf)
            else:
                # Dev single-pass
                strategy = MomentumStrategy(MomentumConfig(fast_period=5, slow_period=10, vol_period=8))
                print("\n" + "=" * 70)
                print(f" SINGLE-PASS BACKTEST (DEV) — {symbol}  ({bars} bars, need ≥{PROD_MIN})")
                print("=" * 70)
                result = engine.run(symbol, df, strategy)
                gate = "✓ PASS" if result.passes_gate() else "✗ FAIL"
                print(f"\n  {gate}  {result.summary()}")
                for note in result.notes:
                    print(f"         ↳ {note}")
    print()


def _print_walkforward(wf) -> None:
    from src.backtester import WalkForwardSummary
    print(f"\n  {wf.summary()}\n")
    print(f"  {'Win':>4} {'#':>5}  {'OOS Period':<25}  {'Sharpe':>7}  {'MaxDD':>7}  {'Return':>8}  {'Trades':>6}")
    print("  " + "-" * 72)
    for w in wf.windows:
        gate = "✓" if w.passes_gate() else "✗"
        print(
            f"  {gate:>4} {w.window_index:>5}  "
            f"{w.oos_start} → {w.oos_end}  "
            f"{w.oos_sharpe:>7.2f}  "
            f"{w.oos_max_drawdown:>6.1%}  "
            f"{w.oos_total_return:>7.1%}  "
            f"{w.oos_num_trades:>6}"
        )
    if not wf.windows:
        print("  No windows generated.")
    for note in wf.notes:
        print(f"\n  ↳ {note}")


def run_live(symbols: list[str]) -> None:
    """Generate live signals and submit orders.

    Two paths:
      ALPACA_DIRECT=1 → AlpacaDirectClient (Cloud Run / no Rust needed)
      default         → TradingBridgeClient (gRPC to local Rust OMS)
    """
    strategy = MomentumStrategy(MomentumConfig(fast_period=5, slow_period=15, vol_period=10, bb_period=0))

    if _ALPACA_DIRECT:
        print("\n" + "=" * 70)
        print(" LIVE SIGNAL → ALPACA DIRECT (REST)")
        print("=" * 70)
        try:
            from src.bridge.alpaca_direct import AlpacaDirectClient
            client = AlpacaDirectClient()
            client.connect()
        except Exception as e:
            print(f"\n  AlpacaDirectClient init failed: {e}")
            return
    else:
        print("\n" + "=" * 70)
        print(" PHASE 2 — LIVE SIGNAL → RUST OMS (gRPC)")
        print("=" * 70)
        try:
            client = TradingBridgeClient(host=GRPC_HOST, port=GRPC_PORT, timeout=5.0)
            client.connect()
        except Exception as e:
            print(f"\n  Cannot connect to Rust OMS at {GRPC_HOST}:{GRPC_PORT}")
            print(f"  Error: {e}")
            print("  Start the Rust engine first: cd core && cargo run")
            print("  (or run with --mode backtest to skip gRPC)")
            return

    # Health check — works for both clients
    try:
        health = client.health_check()
        print(f"\n  Engine health: {health}")
        if not health.healthy:
            print("  Engine unhealthy — aborting.")
            client.disconnect()
            return
        if not health.paper_mode:
            print("  SAFETY: engine not in paper mode — aborting.")
            client.disconnect()
            return
    except Exception as e:
        print(f"\n  Health check failed: {e}")
        client.disconnect()
        return

    print()
    with PostgresOhlcvFetcher() as fetcher:
        for symbol in symbols:
            df = fetcher.fetch(symbol, days=35)
            if df.empty:
                print(f"  {symbol}: no data")
                continue

            current_price = fetcher.fetch_latest_close(symbol)
            if current_price is None:
                print(f"  {symbol}: cannot determine current price")
                continue

            signal = strategy.generate_signal(symbol, df, portfolio_value=100_000.0)

            print(f"  {symbol:<10} → direction={signal.direction.value:<5} "
                  f"score={signal.score:.4f}  price=${current_price:.4f}")

            if signal.direction == Direction.HOLD:
                print(f"  {symbol:<10}   HOLD — not sent to OMS")
                continue

            try:
                response = client.submit_signal(signal, current_price=current_price)
                if response and response.accepted:
                    print(f"  {symbol:<10}   ✓ ORDER ACCEPTED: {response.order_id}")
                elif response:
                    print(f"  {symbol:<10}   ✗ REJECTED: {response.message}")
            except Exception as e:
                print(f"  {symbol:<10}   ERROR: {e}")

    client.disconnect()
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="QuantAI Phase 2 strategy runner")
    parser.add_argument(
        "--mode",
        choices=["backtest", "live", "all"],
        default="backtest",
        help="backtest: run backtest only | live: send signal to Rust OMS | all: both",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=SYMBOLS,
        help="Symbols to trade (default: 31 curated production symbols)",
    )
    args = parser.parse_args()

    if args.mode in ("backtest", "all"):
        run_backtest(args.symbols)

    if args.mode in ("live", "all"):
        run_live(args.symbols)


if __name__ == "__main__":
    main()
