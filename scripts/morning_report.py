#!/usr/bin/env python3
# trading-system/scripts/morning_report.py
#
# Sends a comprehensive morning report via Telegram after the daily cron job.
# Called as the final step of run_daily.sh (replaces --daily-summary).
#
# Report sections:
#   🌍 Market Regime  — latest SPY/MA200 from system_metrics
#   📈 Signals         — yesterday's BUY/SELL/HOLD counts from signals JSON
#   💰 P&L Summary    — today, week, cumulative, open positions
#   🎯 90-Day Gate    — Sharpe, MaxDD, trade count, days elapsed
#   ⏭ Next run       — next scheduled Cloud Run execution
#
# Usage:
#   python3 scripts/morning_report.py
#
# Always non-fatal: DB failures return empty data; Telegram failures log a warning.

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Optional

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS_DIR)
from telegram_alert import send_alert

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("morning_report")

_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://quantai:quantai_dev_2026@localhost:5432/quantai",
)
_SIGNALS_FILE = "/tmp/quantai_signals_today.json"
_PAPER_START = date(2026, 4, 7)

_REGIME_EMOJI = {"BULL": "\U0001f7e2", "NEUTRAL": "\U0001f7e1", "BEAR": "\U0001f534"}


# ── DB helpers ────────────────────────────────────────────────────────────────

def _connect():
    import psycopg2
    return psycopg2.connect(_DB_URL)


def _query_regime() -> dict:
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT labels, recorded_at
                    FROM system_metrics
                    WHERE metric_name = 'market_regime'
                    ORDER BY recorded_at DESC
                    LIMIT 1
                """)
                row = cur.fetchone()
                if row:
                    labels, recorded_at = row
                    if not isinstance(labels, dict):
                        labels = json.loads(labels or "{}")
                    return {
                        "regime": labels.get("regime", ""),
                        "spy_price": float(labels.get("spy_price", 0.0)),
                        "spy_ma200": float(labels.get("spy_ma200", 0.0)),
                        "delta_pct": float(labels.get("delta_pct", 0.0)),
                    }
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Regime query failed: %s", e)
    return {}


def _query_pnl() -> dict:
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                today = date.today()

                cur.execute(
                    "SELECT COALESCE(total_pnl, 0), ending_value FROM daily_pnl WHERE trading_date = %s",
                    (today,),
                )
                row = cur.fetchone()
                today_pnl = float(row[0]) if row else 0.0
                ending_value = float(row[1]) if (row and row[1] is not None) else None

                if ending_value is not None:
                    cumulative_pnl = ending_value - 100_000.0
                else:
                    cur.execute("SELECT COALESCE(SUM(realized_pnl), 0) FROM daily_pnl")
                    r = cur.fetchone()
                    cumulative_pnl = float(r[0]) if r else 0.0

                # Week P&L: last 7 calendar days covers Mon–Fri window
                cur.execute(
                    "SELECT COALESCE(SUM(total_pnl), 0) FROM daily_pnl "
                    "WHERE trading_date > %s AND trading_date <= %s",
                    (today - timedelta(days=7), today),
                )
                r = cur.fetchone()
                week_pnl = float(r[0]) if r else 0.0

                cur.execute("SELECT COUNT(*) FROM positions WHERE quantity != 0")
                r = cur.fetchone()
                open_positions = int(r[0]) if r else 0

                return {
                    "today_pnl": today_pnl,
                    "week_pnl": week_pnl,
                    "cumulative_pnl": cumulative_pnl,
                    "open_positions": open_positions,
                }
        finally:
            conn.close()
    except Exception as e:
        logger.warning("P&L query failed: %s", e)
    return {"today_pnl": 0.0, "week_pnl": 0.0, "cumulative_pnl": 0.0, "open_positions": 0}


def _query_gate() -> dict:
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                today = date.today()
                days_elapsed = max(1, (today - _PAPER_START).days + 1)

                cur.execute(
                    "SELECT trading_date, starting_value, ending_value, num_trades "
                    "FROM daily_pnl WHERE trading_date >= %s ORDER BY trading_date",
                    (_PAPER_START,),
                )
                rows = cur.fetchall()

                total_trades = sum(int(r[3] or 0) for r in rows)

                # Max drawdown (peak-to-trough on ending_value)
                max_dd_pct = 0.0
                peak: Optional[float] = None
                daily_returns: list[float] = []
                prev_val: Optional[float] = None

                for r in rows:
                    sv = float(r[1])
                    ev = float(r[2]) if r[2] is not None else None

                    if prev_val is None:
                        prev_val = sv

                    if ev is not None:
                        if peak is None or ev > peak:
                            peak = ev
                        if peak and peak > 0:
                            dd = (peak - ev) / peak * 100.0
                            max_dd_pct = max(max_dd_pct, dd)
                        if prev_val > 0:
                            daily_returns.append((ev - prev_val) / prev_val)
                        prev_val = ev

                # Annualised Sharpe from daily returns
                sharpe: Optional[float] = None
                if len(daily_returns) >= 2:
                    import statistics
                    mean_r = statistics.mean(daily_returns)
                    std_r = statistics.stdev(daily_returns)
                    if std_r > 0:
                        sharpe = (mean_r / std_r) * (252 ** 0.5)

                # Consecutive days without a new trade (from most recent)
                no_trade_days = 0
                for r in reversed(rows):
                    if int(r[3] or 0) == 0:
                        no_trade_days += 1
                    else:
                        break

                return {
                    "days_elapsed": days_elapsed,
                    "total_trades": total_trades,
                    "max_dd_pct": max_dd_pct,
                    "sharpe": sharpe,
                    "no_trade_days": no_trade_days,
                }
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Gate query failed: %s", e)
    return {"days_elapsed": 1, "total_trades": 0, "max_dd_pct": 0.0, "sharpe": None}


def _load_signals() -> dict:
    try:
        with open(_SIGNALS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_pnl(v: float) -> str:
    return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"


def _next_run_label() -> str:
    """Next Mon–Fri 22:00 UTC run, with Thailand offset."""
    now = datetime.now(timezone.utc)
    candidate = now.replace(hour=22, minute=0, second=0, microsecond=0)
    if now >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    day = candidate.strftime("%a %Y-%m-%d")
    th_hour = (22 + 7) % 24
    return f"{day} 22:00 UTC ({th_hour:02d}:00 TH)"


# ── Report builder ────────────────────────────────────────────────────────────

def build_report() -> tuple[str, str]:
    """Return (message_body, alert_level)."""
    today = date.today()

    # "yesterday" label — skip weekends
    yesterday = today - timedelta(days=1)
    while yesterday.weekday() >= 5:
        yesterday -= timedelta(days=1)
    yesterday_label = yesterday.strftime("%b %d")

    regime_data = _query_regime()
    pnl_data = _query_pnl()
    gate_data = _query_gate()
    signals_data = _load_signals()

    # ── Regime ────────────────────────────────────────────────────────────────
    regime = regime_data.get("regime", "")
    spy_price = regime_data.get("spy_price", 0.0)
    spy_ma200 = regime_data.get("spy_ma200", 0.0)
    delta_pct = regime_data.get("delta_pct", 0.0)

    r_emoji = _REGIME_EMOJI.get(regime, "\u26aa")
    if regime and spy_price and spy_ma200:
        regime_section = (
            f"\U0001f30d Market Regime: {r_emoji} {regime}\n"
            f"SPY: ${spy_price:.2f} | MA200: ${spy_ma200:.2f} | {delta_pct:+.1f}%"
        )
    elif regime:
        regime_section = f"\U0001f30d Market Regime: {r_emoji} {regime}"
    else:
        regime_section = "\U0001f30d Market Regime: \u26aa UNKNOWN (no data yet)"

    # ── Signals ───────────────────────────────────────────────────────────────
    buy_count = int(signals_data.get("buy", 0))
    sell_count = int(signals_data.get("sell", 0))
    hold_count = int(signals_data.get("hold", 0))
    total_signals = buy_count + sell_count + hold_count

    if total_signals == 0:
        signals_section = (
            f"\U0001f4c8 Signals ({yesterday_label})\n"
            "No signal data (strategy may not have run live)"
        )
    else:
        sig_line = f"BUY:  {buy_count} | SELL: {sell_count} | HOLD: {hold_count}"
        orders_submitted = int(signals_data.get("orders_submitted", 0))
        if orders_submitted > 0:
            sig_line += f"\nOrders submitted: {orders_submitted}"
        if buy_count == 0 and (sell_count > 0 or hold_count > 0):
            if regime == "BEAR":
                blocked = "BUY blocked by: Regime (BEAR — all BUY suppressed)"
            elif regime == "NEUTRAL":
                blocked = "BUY blocked by: Regime (NEUTRAL — reduced scores)"
            else:
                blocked = "Waiting: MA crossover or RSI pullback to entry zone"
            sig_line += f"\n{blocked}"
        signals_section = f"\U0001f4c8 Signals ({yesterday_label})\n{sig_line}"

    # ── P&L ──────────────────────────────────────────────────────────────────
    today_pnl = pnl_data.get("today_pnl", 0.0)
    week_pnl = pnl_data.get("week_pnl", 0.0)
    cumulative_pnl = pnl_data.get("cumulative_pnl", 0.0)
    open_positions = pnl_data.get("open_positions", 0)

    pnl_section = (
        f"\U0001f4b0 P&L Summary\n"
        f"Today:      {_fmt_pnl(today_pnl)}\n"
        f"Week:       {_fmt_pnl(week_pnl)}\n"
        f"Cumulative: {_fmt_pnl(cumulative_pnl)}\n"
        f"Open positions: {open_positions}"
    )

    # ── Gate ─────────────────────────────────────────────────────────────────
    days_elapsed = gate_data.get("days_elapsed", 1)
    total_trades = gate_data.get("total_trades", 0)
    max_dd_pct = gate_data.get("max_dd_pct", 0.0)
    sharpe = gate_data.get("sharpe")

    if sharpe is None:
        sharpe_str = "N/A (no trades yet)" if total_trades == 0 else "N/A (insufficient data)"
        sharpe_badge = ""
    elif days_elapsed < 30:
        sharpe_str = f"{sharpe:.2f} (early — <30 days)"
        sharpe_badge = " \u2705" if sharpe >= 1.0 else ""
    else:
        sharpe_str = f"{sharpe:.2f}"
        sharpe_badge = " \u2705" if sharpe >= 1.0 else (" \U0001f6a8" if sharpe < 0 else "")

    dd_badge = (
        " \u2705" if max_dd_pct < 8.0
        else (" \u26a0\ufe0f" if max_dd_pct < 15.0 else " \U0001f6a8")
    )

    no_trade_days = gate_data.get("no_trade_days", 0)
    no_trade_str = f" ({no_trade_days}d no new trades)" if no_trade_days >= 3 else ""
    gate_section = (
        f"\U0001f3af 90-Day Gate Progress (Day {days_elapsed}/90)\n"
        f"Sharpe:  {sharpe_str}{sharpe_badge}\n"
        f"MaxDD:   {max_dd_pct:.2f}%{dd_badge} (gate: <15%)\n"
        f"Trades:  {total_trades}{no_trade_str}"
    )

    # ── Next run ──────────────────────────────────────────────────────────────
    next_section = f"\u23ed Next run: {_next_run_label()}"

    # ── Alert level ───────────────────────────────────────────────────────────
    if max_dd_pct > 15.0:
        level = "CRITICAL"
    elif max_dd_pct > 8.0:
        level = "WARNING"
    else:
        level = "SUMMARY"

    message = "\n\n".join([
        f"QuantAI Morning Report \u2014 {today.isoformat()}",
        regime_section,
        signals_section,
        pnl_section,
        gate_section,
        next_section,
    ])

    return message, level


# ── Entry point ───────────────────────────────────────────────────────────────

def send_morning_report() -> bool:
    """Build and send the morning report. Always non-fatal."""
    try:
        message, level = build_report()
        logger.info("Sending morning report (level=%s)…", level)
        return send_alert(message, level=level)
    except Exception as e:
        logger.warning("Morning report failed (non-fatal): %s", e)
        return False


def main() -> None:
    ok = send_morning_report()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
