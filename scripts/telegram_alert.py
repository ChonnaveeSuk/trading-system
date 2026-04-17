#!/usr/bin/env python3
# trading-system/scripts/telegram_alert.py
#
# Telegram alerts for QuantAI trading events.
#
# Usage as module (import from another script):
#   sys.path.insert(0, "/path/to/scripts")
#   from telegram_alert import send_alert
#   send_alert("BUY Order Submitted\nSymbol: AAPL | ...", level="BUY")
#
# Standalone test (confirms credentials + sends test message):
#   python3 scripts/telegram_alert.py --test
#
# Daily summary (queries DB + reads signals JSON, sends summary):
#   python3 scripts/telegram_alert.py --daily-summary
#
# Always non-fatal: failures log a warning but never abort trading.

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("telegram_alert")

_GCP_PROJECT = os.environ.get("GCP_PROJECT_ID", "quantai-trading-paper")

# Path where run_strategy.py writes today's signal counts.
# read by --daily-summary to build the "Signals: SELL×N HOLD×N BUY×N" line.
_SIGNALS_FILE = "/tmp/quantai_signals_today.json"

# ── Alert level → emoji ────────────────────────────────────────────────────────

LEVEL_EMOJI: dict[str, str] = {
    "INFO":     "🔵",
    "BUY":      "🟢",
    "SELL":     "🔴",
    "WARNING":  "⚠️",
    "CRITICAL": "🚨",
    "SUMMARY":  "📊",
}


# ── Credential loading ─────────────────────────────────────────────────────────

def _gcloud_secret(secret_id: str) -> Optional[str]:
    """Read a GCP secret via gcloud CLI subprocess (works locally + Cloud Run)."""
    import subprocess
    try:
        r = subprocess.run(
            ["gcloud", "secrets", "versions", "access", "latest",
             f"--secret={secret_id}", f"--project={_GCP_PROJECT}"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _load_credentials() -> tuple[Optional[str], Optional[str]]:
    """Return (bot_token, chat_id) or (None, None) if unavailable.

    Priority:
      1. Env vars  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
      2. gcloud CLI subprocess (ADC — works locally and on Cloud Run SA)
      3. GCP Secret Manager Python SDK (google-cloud-secret-manager)
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token:
        bot_token = _gcloud_secret("telegram-bot-token")
    if not chat_id:
        chat_id = _gcloud_secret("telegram-chat-id")

    if not bot_token or not chat_id:
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
            from strategy.src.gcp import get_secret
            if not bot_token:
                bot_token = get_secret("telegram-bot-token", _GCP_PROJECT)
            if not chat_id:
                chat_id = get_secret("telegram-chat-id", _GCP_PROJECT)
        except Exception as e:
            logger.debug("Secret Manager SDK unavailable: %s", e)

    if not bot_token or not chat_id:
        logger.warning(
            "Telegram credentials not found — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID "
            "env vars or store as GCP secrets (telegram-bot-token, telegram-chat-id)"
        )
        return None, None

    return bot_token, chat_id


# ── Core send function ─────────────────────────────────────────────────────────

def send_alert(message: str, level: str = "INFO") -> bool:
    """Send a Telegram message. Returns True if successfully delivered.

    Always non-fatal: logs a warning on failure, never raises.

    Args:
        message: Alert body (plain text).
        level:   INFO | BUY | SELL | WARNING | CRITICAL | SUMMARY
    """
    try:
        import requests
    except ImportError:
        logger.warning("requests not available — Telegram alert skipped")
        return False

    bot_token, chat_id = _load_credentials()
    if not bot_token or not chat_id:
        return False

    emoji = LEVEL_EMOJI.get(level.upper(), "🔵")
    full_message = f"{emoji} {message}"

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": full_message},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Telegram [%s] sent: %.80s", level, message)
        return True
    except Exception as e:
        logger.warning("Telegram alert failed (non-fatal): %s", e)
        return False


# ── Daily summary ──────────────────────────────────────────────────────────────

_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://quantai:quantai_dev_2026@localhost:5432/quantai",
)


def _query_daily_pnl() -> dict:
    """Query today's P&L and max drawdown from the daily_pnl table."""
    try:
        import psycopg2
    except ImportError:
        logger.warning("psycopg2 not available — P&L skipped from summary")
        return {}

    result: dict = {}
    try:
        conn = psycopg2.connect(_DB_URL)
        today = date.today()
        try:
            with conn.cursor() as cur:
                # Today's P&L
                cur.execute(
                    """
                    SELECT
                        COALESCE(realized_pnl + unrealized_pnl, 0) AS total_pnl,
                        ending_value,
                        starting_value
                    FROM daily_pnl
                    WHERE trading_date = %s
                    """,
                    (today,),
                )
                row = cur.fetchone()
                if row:
                    result["daily_pnl"] = float(row[0])
                    result["ending_value"] = float(row[1]) if row[1] is not None else None
                    result["starting_value"] = float(row[2])

                # Cumulative P&L: latest ending_value vs. $100k starting capital
                if result.get("ending_value") is not None:
                    result["cumulative_pnl"] = result["ending_value"] - 100_000.0
                else:
                    # Fall back to sum of all realized P&L
                    cur.execute("SELECT COALESCE(SUM(realized_pnl), 0) FROM daily_pnl")
                    row2 = cur.fetchone()
                    result["cumulative_pnl"] = float(row2[0]) if row2 else 0.0

                # Max drawdown from peak over entire run
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
                row3 = cur.fetchone()
                result["max_dd_pct"] = float(row3[0]) * 100.0 if row3 else 0.0
        finally:
            conn.close()
    except Exception as e:
        logger.warning("daily_pnl query failed: %s", e)

    return result


def _next_run_label() -> str:
    """Return next scheduled run label — Mon–Fri 22:00 UTC."""
    now = datetime.now(timezone.utc)
    candidate = now.replace(hour=22, minute=0, second=0, microsecond=0)
    if now >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:  # skip Sat/Sun
        candidate += timedelta(days=1)
    return candidate.strftime("%a") + " 22:00 UTC"


def send_daily_summary() -> bool:
    """Compile and send the daily trading summary to Telegram."""
    today = date.today().isoformat()

    # Signal counts written by run_strategy.py after run_live()
    signals: dict = {}
    try:
        with open(_SIGNALS_FILE) as f:
            signals = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass  # No signal file — strategy may not have run live today

    buy_count = signals.get("buy", 0)
    sell_count = signals.get("sell", 0)
    hold_count = signals.get("hold", 0)
    orders_submitted = signals.get("orders_submitted", 0)
    regime = signals.get("regime", "")
    spy_price = signals.get("regime_spy_price", 0.0)
    spy_ma200 = signals.get("regime_spy_ma200", 0.0)

    pnl = _query_daily_pnl()
    daily_pnl = pnl.get("daily_pnl", 0.0)
    cumulative_pnl = pnl.get("cumulative_pnl", 0.0)
    max_dd_pct = pnl.get("max_dd_pct", 0.0)
    next_run = _next_run_label()

    daily_sign = "+" if daily_pnl >= 0 else ""
    cum_sign = "+" if cumulative_pnl >= 0 else ""

    # Regime line (only included when regime data is available)
    regime_emoji = {"BULL": "\U0001f7e2", "NEUTRAL": "\U0001f7e1", "BEAR": "\U0001f534"}.get(regime, "")
    if regime and spy_price and spy_ma200:
        spy_delta = (spy_price - spy_ma200) / spy_ma200 * 100
        regime_line = f"Regime: {regime_emoji} {regime}  SPY=${spy_price:.2f}  MA200=${spy_ma200:.2f}  ({spy_delta:+.2f}%)\n"
    elif regime:
        regime_line = f"Regime: {regime_emoji} {regime}\n"
    else:
        regime_line = ""

    message = (
        f"Daily Summary \u2014 {today}\n"
        f"{regime_line}"
        f"Signals: SELL\u00d7{sell_count} HOLD\u00d7{hold_count} BUY\u00d7{buy_count}\n"
        f"Orders: {orders_submitted} submitted\n"
        f"P&L today: {daily_sign}${daily_pnl:.2f} | Cumulative: {cum_sign}${cumulative_pnl:.2f}\n"
        f"MaxDD: {max_dd_pct:.2f}% | Next run: {next_run}"
    )

    # Escalate level if drawdown is elevated
    if max_dd_pct > 15.0:
        level = "CRITICAL"
    elif max_dd_pct > 8.0:
        level = "WARNING"
    else:
        level = "SUMMARY"

    return send_alert(message, level=level)


# ── Standalone entry point ─────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="QuantAI Telegram alert helper")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Send a test message to verify credentials",
    )
    parser.add_argument(
        "--daily-summary",
        action="store_true",
        help="Send today's trading summary (queries DB + reads signals file)",
    )
    args = parser.parse_args()

    if args.test:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        ok = send_alert(
            f"QuantAI alert system online\nTime: {now}\nProject: {_GCP_PROJECT}",
            level="INFO",
        )
        sys.exit(0 if ok else 1)

    if args.daily_summary:
        ok = send_daily_summary()
        sys.exit(0 if ok else 1)

    # Default: show usage
    parser.print_help()


if __name__ == "__main__":
    main()

