#!/usr/bin/env python3
# trading-system/scripts/reconcile_alpaca_fills.py
#
# Reconcile Alpaca paper fills → PostgreSQL fills table.
#
# Run at the START of each daily loop (before update_daily_pnl.py) to capture
# fills from orders submitted in the previous session.
#
# Flow:
#   1. Query orders with status=SUBMITTED from PostgreSQL
#   2. For each: GET /orders/{broker_order_id} from Alpaca
#   3. If filled → INSERT fills row, UPDATE order status=FILLED
#   4. If canceled/rejected/expired → UPDATE order status
#   5. Skip if still pending (will reconcile tomorrow)
#
# Usage:
#   python3 scripts/reconcile_alpaca_fills.py
#   DATABASE_URL=... ALPACA_API_KEY=... python3 scripts/reconcile_alpaca_fills.py
#
# Designed to be idempotent — safe to run multiple times.

from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import requests
    import psycopg2
    import psycopg2.extras
except ImportError as e:
    print(f"Missing dependency: {e}")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("reconcile_alpaca_fills")

_GCP_PROJECT = os.environ.get("GCP_PROJECT_ID", "quantai-trading-paper")
_DEFAULT_ENDPOINT = "https://paper-api.alpaca.markets/v2"
_API_SLEEP_S = 0.3
_COMMISSION = 0.0  # Alpaca paper: no commission


def _load_credentials() -> tuple[str, str, str]:
    """Return (endpoint, api_key, secret_key)."""
    endpoint = os.environ.get("ALPACA_ENDPOINT", _DEFAULT_ENDPOINT)
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        try:
            import subprocess
            def _gcloud_secret(name: str) -> str | None:
                r = subprocess.run(
                    ["gcloud", "secrets", "versions", "access", "latest",
                     f"--secret={name}", f"--project={_GCP_PROJECT}"],
                    capture_output=True, text=True, timeout=10,
                )
                return r.stdout.strip() if r.returncode == 0 else None

            if not api_key:
                api_key = _gcloud_secret("alpaca-api-key")
            if not secret_key:
                secret_key = _gcloud_secret("alpaca-secret-key")
        except Exception:
            pass

    if not api_key or not secret_key:
        # Try Secret Manager Python SDK
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
            from strategy.src.gcp import get_secret
            if not api_key:
                api_key = get_secret("alpaca-api-key", _GCP_PROJECT)
            if not secret_key:
                secret_key = get_secret("alpaca-secret-key", _GCP_PROJECT)
        except Exception as e:
            logger.error("Cannot load credentials: %s", e)
            sys.exit(1)

    return endpoint, api_key, secret_key  # type: ignore[return-value]


def _alpaca_session(api_key: str, secret_key: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
        "Content-Type": "application/json",
    })
    return s


def reconcile(db_url: str, endpoint: str, session: requests.Session) -> int:
    """Reconcile all SUBMITTED orders. Returns number of fills written."""
    conn = psycopg2.connect(db_url)
    fills_written = 0

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT client_order_id, broker_order_id, symbol, side, quantity,
                       signal_score, strategy_id
                FROM orders
                WHERE status = 'SUBMITTED'
                  AND broker_order_id IS NOT NULL
                ORDER BY created_at
            """)
            submitted = cur.fetchall()

        logger.info("Found %d SUBMITTED orders to reconcile", len(submitted))

        for order in submitted:
            broker_id = order["broker_order_id"]
            client_id = order["client_order_id"]
            symbol = order["symbol"]

            try:
                resp = session.get(f"{endpoint}/orders/{broker_id}", timeout=10)
                resp.raise_for_status()
                alpaca_order = resp.json()
                time.sleep(_API_SLEEP_S)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    # Order not found — may have been cancelled externally
                    logger.warning("Order %s not found on Alpaca — marking CANCELLED", broker_id[:8])
                    _update_order_status(conn, client_id, "CANCELLED")
                else:
                    logger.warning("Alpaca API error for %s: %s", broker_id[:8], e)
                continue
            except Exception as e:
                logger.warning("Error checking order %s: %s", broker_id[:8], e)
                continue

            alpaca_status = alpaca_order.get("status", "unknown")

            if alpaca_status == "filled":
                fill_price_str = alpaca_order.get("filled_avg_price") or "0"
                filled_qty_str = alpaca_order.get("filled_qty") or str(order["quantity"])

                try:
                    fill_price = float(fill_price_str)
                    filled_qty = float(filled_qty_str)
                except (ValueError, TypeError):
                    logger.warning("Cannot parse fill data for %s — skipping", broker_id[:8])
                    continue

                if fill_price <= 0:
                    logger.warning("Zero fill price for %s — skipping", broker_id[:8])
                    continue

                fill_id = str(uuid.uuid4())
                filled_at_str = alpaca_order.get("filled_at") or alpaca_order.get("updated_at")
                try:
                    filled_at = datetime.fromisoformat(
                        filled_at_str.replace("Z", "+00:00")
                    ) if filled_at_str else datetime.now(timezone.utc)
                except Exception:
                    filled_at = datetime.now(timezone.utc)

                _write_fill(
                    conn=conn,
                    fill_id=fill_id,
                    client_order_id=str(client_id),
                    broker_order_id=broker_id,
                    symbol=symbol,
                    side=order["side"],
                    filled_qty=filled_qty,
                    fill_price=fill_price,
                    commission=_COMMISSION,
                    filled_at=filled_at,
                    strategy_id=order["strategy_id"],
                )
                _update_order_status(conn, client_id, "FILLED")
                fills_written += 1
                logger.info(
                    "FILLED: %s %s qty=%.4f @ $%.4f  fill_id=%s",
                    order["side"], symbol, filled_qty, fill_price, fill_id[:8],
                )

            elif alpaca_status in ("canceled", "expired", "rejected"):
                _update_order_status(conn, client_id, alpaca_status.upper())
                logger.info(
                    "Order %s %s for %s",
                    broker_id[:8], alpaca_status.upper(), symbol,
                )

            else:
                # Still pending/accepted — leave as SUBMITTED
                logger.debug(
                    "Order %s still %s for %s — will check next run",
                    broker_id[:8], alpaca_status, symbol,
                )

    finally:
        conn.close()

    return fills_written


def _write_fill(
    conn,
    fill_id: str,
    client_order_id: str,
    broker_order_id: str,
    symbol: str,
    side: str,
    filled_qty: float,
    fill_price: float,
    commission: float,
    filled_at: datetime,
    strategy_id: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fills (
                fill_id, client_order_id, broker_order_id, symbol, side,
                filled_quantity, fill_price, commission, timestamp, strategy_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fill_id) DO NOTHING
            """,
            (
                fill_id, client_order_id, broker_order_id, symbol, side,
                str(filled_qty), str(fill_price), str(commission),
                filled_at, strategy_id,
            ),
        )
    conn.commit()


def _update_order_status(conn, client_order_id, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE orders SET status = %s, updated_at = NOW() WHERE client_order_id = %s",
            (status, str(client_order_id)),
        )
    conn.commit()


def main() -> None:
    db_url = os.environ.get(
        "DATABASE_URL",
        "postgres://quantai:quantai_dev_2026@localhost:5432/quantai",
    )

    logger.info("Reconciling Alpaca fills → PostgreSQL…")
    endpoint, api_key, secret_key = _load_credentials()
    session = _alpaca_session(api_key, secret_key)

    fills = reconcile(db_url, endpoint, session)
    session.close()
    logger.info("Done — %d fill(s) written to PostgreSQL", fills)


if __name__ == "__main__":
    main()
