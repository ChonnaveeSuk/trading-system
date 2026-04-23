#!/usr/bin/env python3
# trading-system/scripts/update_daily_pnl.py
#
# Upserts today's row in the daily_pnl table.
#
# Called by run_daily.sh at the end of each trading day.
#
# Computes:
#   realized_pnl   — sum of today's fills (SELL proceeds − BUY costs)
#   unrealized_pnl — sum of positions.unrealized_pnl (all open positions)
#   starting_value — yesterday's ending_value (or $100,000 on day 1)
#   ending_value   — starting_value + total_pnl
#   num_trades     — count of fills with timestamp = today
#
# Usage:
#   python3 scripts/update_daily_pnl.py
#   DATABASE_URL=postgres://... python3 scripts/update_daily_pnl.py

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import psycopg2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("update_daily_pnl")

_DSN = os.environ.get(
    "DATABASE_URL",
    "postgres://quantai:quantai_dev_2026@localhost:5432/quantai",
)
_STARTING_CAPITAL = Decimal("100000.00")
_GCP_PROJECT = os.environ.get("GCP_PROJECT_ID", "quantai-trading-paper")
_ALPACA_ENDPOINT_DEFAULT = "https://paper-api.alpaca.markets/v2"


# ── Alpaca equity helpers ─────────────────────────────────────────────────────

def _fetch_today_pnl_from_alpaca(
    session,
    endpoint: str,
) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """Return (today_pnl, equity) from Alpaca /v2/account.

    today_pnl = equity - last_equity  (matches Alpaca UI exactly).

    On any failure (network, missing field, bad JSON), returns (None, None)
    and logs a warning.  Callers must treat None as "skip this update"
    rather than overwriting daily_pnl with corrupt zeros.
    """
    try:
        resp = session.get(f"{endpoint}/account", timeout=10)
        resp.raise_for_status()
        body = resp.json() or {}
    except Exception as e:
        logger.warning(
            "Alpaca equity fetch failed (non-fatal — daily_pnl skipped): %s", e,
        )
        return None, None

    if "equity" not in body or "last_equity" not in body:
        logger.warning(
            "Alpaca /account missing equity or last_equity field — skipping",
        )
        return None, None

    try:
        equity = Decimal(str(body["equity"]))
        last_equity = Decimal(str(body["last_equity"]))
    except Exception as e:
        logger.warning("Alpaca equity parse failed (non-fatal): %s", e)
        return None, None

    return equity - last_equity, equity


def _load_alpaca_session():
    """Build a requests.Session authenticated to Alpaca paper-api.

    Credentials: env vars first, then gcloud CLI subprocess (works on both
    local dev and Cloud Run SA).  Returns (session, endpoint) or (None, None)
    if credentials can't be loaded — caller must handle that.
    """
    try:
        import requests
    except ImportError:
        logger.warning("requests not available — skipping Alpaca fetch")
        return None, None

    endpoint = os.environ.get("ALPACA_ENDPOINT", _ALPACA_ENDPOINT_DEFAULT)
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")

    def _gcloud_secret(name: str) -> Optional[str]:
        try:
            r = subprocess.run(
                ["gcloud", "secrets", "versions", "access", "latest",
                 f"--secret={name}", f"--project={_GCP_PROJECT}"],
                capture_output=True, text=True, timeout=10,
            )
            return r.stdout.strip() if r.returncode == 0 else None
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None

    if not api_key:
        api_key = _gcloud_secret("alpaca-api-key")
    if not secret_key:
        secret_key = _gcloud_secret("alpaca-secret-key")

    if not api_key or not secret_key:
        logger.warning(
            "Alpaca credentials unavailable — today_pnl from DB fallback only",
        )
        return None, None

    session = requests.Session()
    session.headers.update({
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
        "Content-Type": "application/json",
    })
    return session, endpoint


def update(dsn: str = _DSN) -> None:
    today = date.today()
    conn = psycopg2.connect(dsn)

    try:
        with conn:
            with conn.cursor() as cur:
                # ── Starting value ────────────────────────────────────────────
                # Use yesterday's ending_value if available, else $100k.
                cur.execute("""
                    SELECT COALESCE(ending_value, starting_value)
                    FROM daily_pnl
                    WHERE trading_date = %s
                """, (today,))
                row = cur.fetchone()
                if row:
                    # Row already exists — update in place
                    starting_value = None  # will keep existing starting_value
                else:
                    cur.execute("""
                        SELECT COALESCE(ending_value, starting_value)
                        FROM daily_pnl
                        WHERE trading_date < %s
                        ORDER BY trading_date DESC
                        LIMIT 1
                    """, (today,))
                    prev = cur.fetchone()
                    starting_value = Decimal(str(prev[0])) if prev else _STARTING_CAPITAL

                # ── Today's P&L from Alpaca equity delta ─────────────────────
                # equity - last_equity matches the Alpaca UI exactly and is
                # always correct even when positions are still open.  The old
                # cash-flow CASE formula (SUM CASE WHEN side='SELL' …) treated
                # every BUY as a realized loss and produced the 2026-04-23
                # false -$49,234 / 50.55% drawdown.  Bug archived in
                # infra/postgres/migration_005_backfill_daily_pnl_20260423.sql.
                day_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
                day_next = day_start + timedelta(days=1)

                alpaca_session, alpaca_endpoint = _load_alpaca_session()
                today_pnl_from_alpaca: Optional[Decimal] = None
                live_equity: Optional[Decimal] = None
                if alpaca_session is not None:
                    today_pnl_from_alpaca, live_equity = _fetch_today_pnl_from_alpaca(
                        alpaca_session, alpaca_endpoint,
                    )
                    alpaca_session.close()

                # ── Unrealized P&L from open positions (populated by reconcile) ─
                cur.execute("SELECT COALESCE(SUM(unrealized_pnl), 0) FROM positions")
                unrealized_pnl = Decimal(str(cur.fetchone()[0]))

                # Allocate the total P&L into realized vs unrealized so the
                # generated column total_pnl = realized + unrealized still matches.
                # realized = today_total - unrealized.  If Alpaca is unreachable,
                # fall back to zero total rather than corrupting with the old
                # cash-flow formula.
                if today_pnl_from_alpaca is not None:
                    realized_pnl = today_pnl_from_alpaca - unrealized_pnl
                else:
                    logger.warning(
                        "Alpaca equity unavailable — realized_pnl set to 0 "
                        "(unrealized_pnl still reflects DB positions)",
                    )
                    realized_pnl = Decimal("0")

                # ── Trade count (fills today) ─────────────────────────────────
                cur.execute(
                    "SELECT COUNT(*) FROM fills WHERE timestamp >= %s AND timestamp < %s",
                    (day_start, day_next),
                )
                num_trades = cur.fetchone()[0]

                # ── Upsert ────────────────────────────────────────────────────
                if starting_value is None:
                    # Row exists — update P&L fields only, keep starting_value
                    cur.execute("""
                        UPDATE daily_pnl
                        SET realized_pnl   = %s,
                            unrealized_pnl = %s,
                            num_trades     = %s,
                            ending_value   = (SELECT starting_value FROM daily_pnl WHERE trading_date = %s) + %s + %s,
                            updated_at     = NOW()
                        WHERE trading_date = %s
                    """, (
                        realized_pnl,
                        unrealized_pnl,
                        num_trades,
                        today,
                        realized_pnl,
                        unrealized_pnl,
                        today,
                    ))
                else:
                    ending_value = starting_value + realized_pnl + unrealized_pnl
                    cur.execute("""
                        INSERT INTO daily_pnl
                            (trading_date, starting_value, ending_value,
                             realized_pnl, unrealized_pnl, num_trades)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (trading_date) DO UPDATE SET
                            realized_pnl   = EXCLUDED.realized_pnl,
                            unrealized_pnl = EXCLUDED.unrealized_pnl,
                            num_trades     = EXCLUDED.num_trades,
                            ending_value   = EXCLUDED.ending_value,
                            updated_at     = NOW()
                    """, (
                        today,
                        starting_value,
                        ending_value,
                        realized_pnl,
                        unrealized_pnl,
                        num_trades,
                    ))
                    ending_value_log = ending_value

                logger.info(
                    "daily_pnl updated for %s: realized=$%.2f  unrealized=$%.2f  trades=%d",
                    today, realized_pnl, unrealized_pnl, num_trades,
                )

    finally:
        conn.close()


if __name__ == "__main__":
    try:
        update()
    except Exception as e:
        logger.error("daily_pnl update failed: %s", e)
        sys.exit(1)
