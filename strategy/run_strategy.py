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
from src.db import database_url as _database_url
from src.signals import Direction

# When ALPACA_DIRECT=1, live signals go straight to Alpaca REST (no Rust gRPC needed).
# This is the Cloud Run path — Rust OMS cannot run there (no Redis, PaperBroker only).
_ALPACA_DIRECT = os.environ.get("ALPACA_DIRECT", "0") == "1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_strategy")

# Tech-focused 16-symbol universe (replaces the 30-symbol precious-metals-heavy
# universe that caused 2026-04-28 100% concentration → -$4,825 cumulative loss).
# Sector caps still apply (max 3 positions / 30% notional per sector).
SYMBOLS = [
    # Big Tech (sector: big_tech)
    "AAPL", "MSFT", "NVDA", "GOOGL", "META",
    # Tech ETFs (sector: tech_etf)
    "QQQ", "XLK", "SMH",
    # Growth (sector: growth)
    "TSLA", "AMD", "AVGO",
    # Broad market (sector: broad_market)
    "SPY", "IWM",
    # Crypto (sector: crypto)
    "BTC-USD",
    # Defensive (sector: defensive — bonds only, no precious metals)
    "TLT", "BND",
]

# All 16 symbols are Alpaca-paper-tradeable.
LIVE_SYMBOLS = list(SYMBOLS)

# Max calendar days of staleness before skipping a symbol in live mode.
# 5 trading days = 7 calendar days covers weekends + 1 holiday.
_LIVE_STALE_DAYS = 7

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

        # ── Fetch VIXY for bar-by-bar VIX filter ──────────────────────────────
        vix_df: pd.DataFrame = pd.DataFrame()
        if cfg.vix_filter:
            vix_df = fetcher.fetch(cfg.vix_symbol, days=700)
            if vix_df.empty:
                logger.warning(
                    "%s data not available — VIX filter disabled for backtest",
                    cfg.vix_symbol,
                )
            else:
                _tmp_v = MomentumStrategy(cfg)
                vstate = _tmp_v.update_vix(vix_df)
                if _tmp_v._vix_level:
                    print(f"  VIX state: {vstate}  {cfg.vix_symbol}=${_tmp_v._vix_price:.2f}  "
                          f"MA{cfg.vix_ma_period}=${_tmp_v._vix_level:.2f}  "
                          f"thresholds={cfg.vix_caution_threshold:.0f}/{cfg.vix_panic_threshold:.0f}")
        vix_df_arg = vix_df if not vix_df.empty else None

        for symbol in symbols:
            if symbol == cfg.vix_symbol:
                continue  # VIX proxy is a data source, not a tradable target
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
                        MomentumConfig(**{**cfg.__dict__, "regime_filter": False, "vix_filter": False})
                    ).generate_signals_series(symbol, df)
                    sigs_filtered = strategy.generate_signals_series(
                        symbol, df, regime_df=regime_df_arg, vix_df=vix_df_arg,
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
                    print(f" Regime+VIX filter: {blocked_buys} BUY signal(s) blocked")
                print("=" * 70)
                wf = engine.walk_forward(
                    symbol, df, strategy,
                    regime_df=regime_df_arg, vix_df=vix_df_arg,
                )
                _print_walkforward(wf)
            else:
                print("\n" + "=" * 70)
                print(f" SINGLE-PASS BACKTEST (DEV) — {symbol}  ({bars} bars, need ≥{PROD_MIN})")
                if blocked_buys > 0:
                    print(f" Regime+VIX filter: {blocked_buys} BUY signal(s) blocked")
                print("=" * 70)
                result = engine.run(
                    symbol, df, strategy,
                    regime_df=regime_df_arg, vix_df=vix_df_arg,
                )
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

    # ── Hard stop loss (BEFORE strategy signal loop) ──────────────────────────
    # Frees equity for the same day's signals if a position has breached
    # MomentumConfig.stop_loss_pct. AlpacaDirect path only — TradingBridgeClient
    # has no Alpaca position view.
    if _ALPACA_DIRECT and strategy.config.stop_loss_enabled:
        try:
            stops = client.check_and_trigger_stops(
                stop_loss_pct=strategy.config.stop_loss_pct,
                warn_pct=strategy.config.stop_loss_warn_pct,
                telegram_alert=_telegram_alert,
            )
            triggered = [s for s in stops if s.triggered]
            warned = [s for s in stops if s.warned]
            if triggered:
                print(f"\n  STOP LOSS: {len(triggered)} position(s) closed:")
                for s in triggered:
                    print(f"    🛑 {s.symbol:<8} {s.unrealized_plpc*100:+6.2f}%  "
                          f"order={s.order_id}")
            if warned:
                print(f"  STOP LOSS WARN: {len(warned)} position(s) at risk:")
                for s in warned:
                    print(f"    ⚠  {s.symbol:<8} {s.unrealized_plpc*100:+6.2f}%  "
                          f"(stop at -{strategy.config.stop_loss_pct*100:.1f}%)")
            counts["stops_triggered"] = len(triggered)
            counts["stops_warned"] = len(warned)
        except Exception as e:
            logger.warning("Stop-loss check failed (non-fatal): %s", e)

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

        # ── Economic calendar — surface blackout / D-1 alert ─────────────────
        if strategy.config.calendar_filter:
            try:
                from src.filters.economic_calendar import EconomicCalendar
                _cal = EconomicCalendar(blackout_days_before=strategy.config.calendar_blackout_days)
                import datetime as _dt
                _today = _dt.date.today()
                _block_reason = _cal.blackout_reason(_today)
                if _block_reason:
                    print(f"  [CALENDAR] BLACKOUT — {_block_reason} — all BUYs blocked")
                _next = _cal.get_next_event(_today)
                if _next:
                    _ev, _days = _next
                    print(f"  [CALENDAR] Next event: {_ev.kind.value} "
                          f"({_ev.event_date.isoformat()}) — {_days} day(s) away")
                # D-1 alert: fire once per day when an event is exactly tomorrow
                _tomorrow_ev = _cal.event_within(_today, 1)
                if _tomorrow_ev is not None and (_tomorrow_ev.event_date - _today).days == 1:
                    _telegram_alert(
                        f"Tomorrow: {_tomorrow_ev.kind.value} — {_tomorrow_ev.description}\n"
                        f"BUY orders will be blocked on "
                        f"{_today.isoformat()} and {_tomorrow_ev.event_date.isoformat()}.",
                        level="WARNING",
                    )
                counts["calendar_blackout"] = bool(_block_reason)         # type: ignore[assignment]
                counts["calendar_blackout_reason"] = _block_reason or ""  # type: ignore[assignment]
                if _next:
                    counts["calendar_next_event"] = _next[0].kind.value   # type: ignore[assignment]
                    counts["calendar_next_event_date"] = _next[0].event_date.isoformat()  # type: ignore[assignment]
                    counts["calendar_next_event_days_away"] = int(_next[1])  # type: ignore[assignment]
            except Exception as _e:
                logger.debug("Calendar telemetry/alert failed (non-fatal): %s", _e)

        # ── VIX (volatility) detection — drives BUY blocking and size halving ─
        if strategy.config.vix_filter:
            vixy_days = max(strategy.config.vix_ma_period * 4, 90)
            vixy_df = fetcher.fetch(strategy.config.vix_symbol, days=vixy_days)
            vix_state = strategy.update_vix(vixy_df)
            vix_level = strategy._vix_level or 0.0
            vix_price = strategy._vix_price or 0.0
            if vix_level > 0:
                print(f"  VIX state: {vix_state}  "
                      f"{strategy.config.vix_symbol}=${vix_price:.2f}  "
                      f"MA{strategy.config.vix_ma_period}=${vix_level:.2f}  "
                      f"thresholds={strategy.config.vix_caution_threshold:.0f}/"
                      f"{strategy.config.vix_panic_threshold:.0f}")
            else:
                print(f"  VIX state: {vix_state} (no VIXY data — defaulting to permissive)")
            if vix_state == "PANIC":
                print(f"  [VIX] PANIC — all BUY signals suppressed")
            elif vix_state == "CAUTION":
                print(f"  [VIX] CAUTION — BUY/SELL position size halved")
            _check_and_record_vix_change(vix_state, vix_level, vix_price)
            counts["vix_state"] = vix_state           # type: ignore[assignment]
            counts["vix_level"] = round(vix_level, 2) # type: ignore[assignment]
            counts["vix_price"] = round(vix_price, 2) # type: ignore[assignment]

        for symbol in symbols:
            # 90 calendar days ≈ 63 trading days — enough for stable RSI(7)+ATR(14)
            # context and for trend_ride_min_bars consecutive-uptrend detection.
            df = fetcher.fetch(symbol, days=90)
            if df.empty:
                print(f"  {symbol}: no data")
                continue

            # Staleness gate: skip symbols whose latest bar is >7 calendar days old.
            import datetime as _dt
            latest_date = df.index[-1].date() if hasattr(df.index[-1], "date") else df.index[-1]
            data_age_days = (_dt.date.today() - latest_date).days
            if data_age_days > _LIVE_STALE_DAYS:
                logger.warning(
                    "%s: OHLCV data is %d days stale (latest=%s) — skipping live signal",
                    symbol, data_age_days, latest_date,
                )
                print(f"  {symbol:<10}   SKIPPED — data {data_age_days}d stale (latest={latest_date})")
                continue

            current_price = fetcher.fetch_latest_close(symbol)
            if current_price is None:
                print(f"  {symbol}: cannot determine current price")
                continue

            signal = strategy.generate_signal(symbol, df, portfolio_value=100_000.0)

            trend_ride_flag = " [trend_ride]" if signal.features.get("trend_ride") else ""
            print(f"  {symbol:<10} → direction={signal.direction.value:<5} "
                  f"score={signal.score:.4f}  price=${current_price:.4f}  "
                  f"rsi={signal.features.get('rsi', '?'):.1f}{trend_ride_flag}")

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
        default=None,
        help="Override symbols (default: SYMBOLS for backtest, LIVE_SYMBOLS for live)",
    )
    args = parser.parse_args()

    backtest_syms = args.symbols or SYMBOLS
    live_syms = args.symbols or LIVE_SYMBOLS

    if args.mode in ("backtest", "all"):
        run_backtest(backtest_syms)

    if args.mode in ("live", "all"):
        run_live(live_syms)

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
    try:
        db_url = _database_url()
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


def _check_and_record_vix_change(
    current_state: str,
    vix_level: float,
    vix_price: float,
) -> None:
    """Persist VIX state to system_metrics and Telegram-alert on threshold cross.

    Reads the last stored vix_state; if it differs from current_state, fires
    an alert sized to the new state (PANIC=CRITICAL, CAUTION=WARNING,
    CALM=INFO). Always non-fatal.
    """
    try:
        import psycopg2
        import json as _json

        db_url = _database_url()
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT labels->>'vix_state'
                    FROM system_metrics
                    WHERE metric_name = 'vix_state'
                    ORDER BY recorded_at DESC
                    LIMIT 1
                    """
                )
                row = cur.fetchone()
                last_state = row[0] if row else None

                # Encode VIX state as numeric for time-series Grafana panels
                state_value = {"CALM": 0.0, "CAUTION": 1.0, "PANIC": 2.0}.get(current_state, 0.0)

                cur.execute(
                    """
                    INSERT INTO system_metrics (metric_name, metric_value, labels)
                    VALUES ('vix_state', %s, %s)
                    """,
                    (
                        state_value,
                        _json.dumps({
                            "vix_state": current_state,
                            "vix_level": round(vix_level, 4),
                            "vix_price": round(vix_price, 4),
                        }),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        if last_state and last_state != current_state:
            transition = f"{last_state} → {current_state}"
            if current_state == "PANIC":
                msg = (
                    f"VIX State Change: {transition}\n"
                    f"VIXY MA20: {vix_level:.2f} | Last close: ${vix_price:.2f}\n"
                    f"PANIC: all new BUY orders blocked until volatility subsides."
                )
                _telegram_alert(msg, level="CRITICAL")
            elif current_state == "CAUTION":
                msg = (
                    f"VIX State Change: {transition}\n"
                    f"VIXY MA20: {vix_level:.2f} | Last close: ${vix_price:.2f}\n"
                    f"CAUTION: position sizes halved on BUY/SELL."
                )
                _telegram_alert(msg, level="WARNING")
            else:  # CALM
                msg = (
                    f"VIX State Change: {transition}\n"
                    f"VIXY MA20: {vix_level:.2f} | Last close: ${vix_price:.2f}\n"
                    f"CALM: full position sizing restored."
                )
                _telegram_alert(msg, level="INFO")
            logger.info("VIX state change alert sent: %s", transition)

    except Exception as e:
        logger.debug("VIX state record/alert failed (non-fatal): %s", e)


def _check_max_drawdown_alert() -> None:
    """Query live MaxDD from daily_pnl. Send CRITICAL alert if > 8%."""
    try:
        import psycopg2

        db_url = _database_url()
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
