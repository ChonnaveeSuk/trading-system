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
import json
import logging
import sys
import os

# Add the strategy directory to path when running as script
sys.path.insert(0, os.path.dirname(__file__))

# Non-fatal Telegram import — works once scripts/telegram_alert.py exists.
# Absent credentials → send_alert() logs a warning and returns False.
_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
sys.path.insert(0, _SCRIPTS_DIR)
try:
    from telegram_alert import send_alert as _telegram_alert
    _TELEGRAM = True
except ImportError:
    def _telegram_alert(message: str, level: str = "INFO") -> bool:  # type: ignore[misc]
        return False
    _TELEGRAM = False

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
      ≥315 bars (252 IS + 63 OOS): walk-forward with production MA params (5/15/10)
      <315 bars:                   single-pass with dev MA params (5/10/8), flagged DEV MODE

    Regime filter:
      When regime_filter=True (default), SPY data is fetched and passed to the
      engine as regime_df.  Each symbol's results include a 'Blocked by regime'
      count showing how many BUY signals were suppressed by the bear/neutral filter.
    """
    import pandas as pd
    from src.backtester import BacktestConfig, WalkForwardSummary

    PROD_MIN = 315  # 252 IS + 63 OOS minimum for walk-forward

    cfg = MomentumConfig(fast_period=5, slow_period=15, vol_period=10, bb_period=0)
    engine = BacktestEngine(config=BacktestConfig(
        commission_per_share=0.005,
        slippage_bps=0.5,
    ))

    with PostgresOhlcvFetcher() as fetcher:
        # ── Fetch SPY for bar-by-bar regime detection ─────────────────────────
        spy_df: pd.DataFrame = pd.DataFrame()
        if cfg.regime_filter:
            spy_df = fetcher.fetch("SPY", days=700)
            if spy_df.empty:
                logger.warning("SPY data not available — regime filter disabled for backtest")
            else:
                # Report current regime from SPY
                _tmp_strategy = MomentumStrategy(cfg)
                regime_now = _tmp_strategy.update_regime(spy_df)
                print(f"\n  Market regime (SPY MA{cfg.regime_ma_period}): {regime_now}")
                if _tmp_strategy._spy_price and _tmp_strategy._spy_ma200:
                    spy_delta = (_tmp_strategy._spy_price - _tmp_strategy._spy_ma200) / _tmp_strategy._spy_ma200 * 100
                    print(f"  SPY=${_tmp_strategy._spy_price:.2f}  MA{cfg.regime_ma_period}=${_tmp_strategy._spy_ma200:.2f}  delta={spy_delta:+.2f}%")

        regime_df_arg = spy_df if not spy_df.empty else None

        for symbol in symbols:
            df = fetcher.fetch(symbol, days=700)
            if df.empty:
                print(f"  {symbol}: no data — skipping")
                continue

            bars = len(df)
            strategy = MomentumStrategy(cfg)

            # ── Count regime-blocked BUY signals (for reporting) ──────────────
            blocked_buys = 0
            if regime_df_arg is not None:
                try:
                    sigs_raw = MomentumStrategy(
                        MomentumConfig(**{**cfg.__dict__, "regime_filter": False})
                    ).generate_signals_series(symbol, df)
                    sigs_filtered = strategy.generate_signals_series(
                        symbol, df, regime_df=regime_df_arg
                    )
                    buys_raw = int((sigs_raw["direction"] == "BUY").sum())
                    buys_filtered = int((sigs_filtered["direction"] == "BUY").sum())
                    blocked_buys = max(0, buys_raw - buys_filtered)
                except Exception:
                    pass

            if bars >= PROD_MIN:
                print("\n" + "=" * 70)
                print(f" WALK-FORWARD BACKTEST — {symbol}  ({bars} bars)")
                if blocked_buys > 0:
                    print(f" Regime filter: {blocked_buys} BUY signal(s) blocked")
                print("=" * 70)
                wf = engine.walk_forward(symbol, df, strategy, regime_df=regime_df_arg)
                _print_walkforward(wf)
            else:
                print("\n" + "=" * 70)
                print(f" SINGLE-PASS BACKTEST (DEV) — {symbol}  ({bars} bars, need ≥{PROD_MIN})")
                if blocked_buys > 0:
                    print(f" Regime filter: {blocked_buys} BUY signal(s) blocked")
                print("=" * 70)
                result = engine.run(symbol, df, strategy, regime_df=regime_df_arg)
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

    # Signal counters for daily summary and Telegram alerts
    counts: dict[str, int] = {"buy": 0, "sell": 0, "hold": 0, "orders_submitted": 0}

    print()
    with PostgresOhlcvFetcher() as fetcher:
        # ── Market regime detection (must happen before signal generation) ────
        if strategy.config.regime_filter:
            # Need ≥regime_ma_period trading days (~300 calendar days for MA200)
            spy_days = max(strategy.config.regime_ma_period * 2, 300)
            spy_df = fetcher.fetch("SPY", days=spy_days)
            # Log SPY data freshness before computing regime
            if not spy_df.empty:
                import datetime as _dt
                latest = spy_df.index[-1]
                if hasattr(latest, "date"):
                    latest = latest.date()
                data_age = (_dt.date.today() - latest).days
                logger.info("SPY data: %d bars, latest=%s (%d days old)", len(spy_df), latest, data_age)
            regime = strategy.update_regime(spy_df)
            spy_price = strategy._spy_price or 0.0
            spy_ma200 = strategy._spy_ma200 or 0.0
            spy_delta_pct = ((spy_price - spy_ma200) / spy_ma200 * 100) if spy_ma200 > 0 else 0.0
            print(f"\n  Market regime: {regime}  "
                  f"SPY=${spy_price:.2f}  MA{strategy.config.regime_ma_period}=${spy_ma200:.2f}  "
                  f"delta={spy_delta_pct:+.2f}%")
            if regime == "BEAR":
                print(f"  [REGIME] BEAR market — all BUY signals suppressed")
            elif regime == "NEUTRAL":
                print(f"  [REGIME] NEUTRAL — BUY scores reduced by 30%")
            _check_and_record_regime_change(regime, spy_price, spy_ma200)
            counts["regime"] = regime  # type: ignore[assignment]  # str in int dict — OK
            counts["regime_spy_price"] = round(spy_price, 2)   # type: ignore[assignment]
            counts["regime_spy_ma200"] = round(spy_ma200, 2)   # type: ignore[assignment]
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

            direction_key = signal.direction.value.lower()
            if direction_key in counts:
                counts[direction_key] += 1

            if signal.direction == Direction.HOLD:
                print(f"  {symbol:<10}   HOLD — not sent to OMS")
                continue

            try:
                response = client.submit_signal(signal, current_price=current_price)
                if response and response.accepted:
                    print(f"  {symbol:<10}   ✓ ORDER ACCEPTED: {response.order_id}")
                    counts["orders_submitted"] += 1
                    # Telegram: BUY/SELL order confirmed
                    qty_str = str(signal.suggested_quantity) if signal.suggested_quantity else "?"
                    _telegram_alert(
                        f"{signal.direction.value} Order Submitted\n"
                        f"Symbol: {symbol} | Price: ${current_price:.2f} | "
                        f"Qty: {qty_str} | Score: {signal.score:.2f}",
                        level=signal.direction.value,
                    )
                elif response:
                    print(f"  {symbol:<10}   ✗ REJECTED: {response.message}")
            except Exception as e:
                print(f"  {symbol:<10}   ERROR: {e}")

    client.disconnect()

    # Persist signal counts for run_daily.sh → telegram_alert.py --daily-summary
    try:
        signal_data = {**counts, "date": __import__("datetime").date.today().isoformat()}
        with open("/tmp/quantai_signals_today.json", "w") as f:
            json.dump(signal_data, f)
        logger.debug("Signal counts written to /tmp/quantai_signals_today.json")
    except Exception as e:
        logger.debug("Could not write signals file (non-fatal): %s", e)

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

    # Check live MaxDD after any live run — alert if it exceeds 8% warning threshold
    if args.mode in ("live", "all"):
        _check_max_drawdown_alert()


def _check_and_record_regime_change(
    current_regime: str,
    spy_price: float,
    spy_ma200: float,
) -> None:
    """Persist regime to system_metrics and send a Telegram alert on change.

    Reads the last stored regime from system_metrics; if it differs from
    current_regime, fires a ⚠️ or 🚨 alert.  Always non-fatal.
    """
    db_url = os.environ.get(
        "DATABASE_URL",
        "postgres://quantai:quantai_dev_2026@localhost:5432/quantai",
    )
    try:
        import psycopg2
        import json as _json

        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                # Read last stored regime
                cur.execute(
                    """
                    SELECT labels->>'regime'
                    FROM system_metrics
                    WHERE metric_name = 'market_regime'
                    ORDER BY recorded_at DESC
                    LIMIT 1
                    """
                )
                row = cur.fetchone()
                last_regime = row[0] if row else None

                # Encode regime as numeric (useful for Grafana time-series)
                regime_value = {"BULL": 1.0, "NEUTRAL": 0.0, "BEAR": -1.0}.get(current_regime, 0.0)
                delta_pct = ((spy_price - spy_ma200) / spy_ma200 * 100) if spy_ma200 > 0 else 0.0

                # Write current regime
                cur.execute(
                    """
                    INSERT INTO system_metrics (metric_name, metric_value, labels)
                    VALUES ('market_regime', %s, %s)
                    """,
                    (
                        regime_value,
                        _json.dumps({
                            "regime": current_regime,
                            "spy_price": round(spy_price, 4),
                            "spy_ma200": round(spy_ma200, 4),
                            "delta_pct": round(delta_pct, 4),
                        }),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        # Alert on regime change
        if last_regime and last_regime != current_regime:
            spy_delta = ((spy_price - spy_ma200) / spy_ma200 * 100) if spy_ma200 > 0 else 0.0
            transition = f"{last_regime} \u2192 {current_regime}"
            if current_regime == "BEAR":
                msg = (
                    f"Regime Change: {transition}\n"
                    f"SPY: ${spy_price:.2f} | MA200: ${spy_ma200:.2f} | Delta: {spy_delta:+.2f}%\n"
                    f"BUY signals suppressed until regime recovers."
                )
                _telegram_alert(msg, level="CRITICAL")
            elif last_regime == "BEAR" and current_regime in ("NEUTRAL", "BULL"):
                msg = (
                    f"Regime Change: {transition}\n"
                    f"SPY: ${spy_price:.2f} | MA200: ${spy_ma200:.2f} | Delta: {spy_delta:+.2f}%\n"
                    f"BUY signals re-enabled."
                )
                _telegram_alert(msg, level="INFO")
            else:
                msg = (
                    f"Regime Change: {transition}\n"
                    f"SPY: ${spy_price:.2f} | MA200: ${spy_ma200:.2f} | Delta: {spy_delta:+.2f}%"
                )
                _telegram_alert(msg, level="WARNING")
            logger.info("Regime change alert sent: %s", transition)

    except Exception as e:
        logger.debug("Regime record/alert failed (non-fatal): %s", e)


def _check_max_drawdown_alert() -> None:
    """Query live MaxDD from daily_pnl. Send CRITICAL alert if > 8%."""
    try:
        import psycopg2

        db_url = os.environ.get(
            "DATABASE_URL",
            "postgres://quantai:quantai_dev_2026@localhost:5432/quantai",
        )
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH peaks AS (
                        SELECT
                            ending_value,
                            MAX(ending_value) OVER (
                                ORDER BY trading_date
                                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                            ) AS peak_value
                        FROM daily_pnl
                        WHERE ending_value IS NOT NULL
                    )
                    SELECT COALESCE(
                        MAX((peak_value - ending_value) / NULLIF(peak_value, 0)),
                        0.0
                    )
                    FROM peaks
                    """
                )
                row = cur.fetchone()
                max_dd_pct = float(row[0]) * 100.0 if row else 0.0
        finally:
            conn.close()

        if max_dd_pct > 8.0:
            gate_status = "EXCEEDS gate limit (15%)" if max_dd_pct > 15.0 else "exceeds 8% warning"
            _telegram_alert(
                f"MaxDD Alert: {max_dd_pct:.2f}% {gate_status}\n"
                f"Current MaxDD: {max_dd_pct:.2f}% | Gate limit: 15%\n"
                f"Review portfolio — consider reducing exposure.",
                level="CRITICAL",
            )
            logger.warning("MaxDD alert sent: %.2f%%", max_dd_pct)
        else:
            logger.info("MaxDD check OK: %.2f%% (threshold 8%%)", max_dd_pct)

    except Exception as e:
        logger.debug("MaxDD check failed (non-fatal): %s", e)


if __name__ == "__main__":
    main()
