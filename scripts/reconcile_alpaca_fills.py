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

# Non-fatal Telegram import — telegram_alert.py is in the same scripts/ directory
sys.path.insert(0, os.path.dirname(__file__))
try:
    from telegram_alert import send_alert as _telegram_alert
except ImportError:
    def _telegram_alert(message: str, level: str = "INFO") -> bool:  # type: ignore[misc]
        return False

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


def _alpaca_to_db_symbol(alpaca_symbol: str) -> str:
    """Reverse of _to_alpaca_symbol in alpaca_direct.py.

    Alpaca returns crypto as a concatenated pair ("BTCUSD", "ETHUSD"); our
    DB stores them in yfinance style ("BTC-USD", "ETH-USD").  Stocks are
    unchanged.  FX pairs (EUR-USD, GBP-USD) never appear in Alpaca positions.
    """
    if (
        len(alpaca_symbol) == 6
        and alpaca_symbol.endswith("USD")
        and alpaca_symbol not in ("GBPUSD", "EURUSD")
    ):
        return alpaca_symbol[:-3] + "-USD"
    return alpaca_symbol


def _sync_positions_from_alpaca(
    conn,
    session: requests.Session,
    endpoint: str,
) -> None:
    """Mirror Alpaca /v2/positions into our positions table.

    Alpaca is source of truth: every open Alpaca position is UPSERTed, and
    any DB row for a symbol NOT in the Alpaca response is zeroed out
    (quantity=0, unrealized_pnl=0).  Idempotent — safe to run every cron.

    Non-fatal: if the Alpaca API call fails we log a warning and return.
    Callers should continue — the fills they just wrote are still valid,
    positions will re-sync on the next cron run.
    """
    try:
        resp = session.get(f"{endpoint}/positions", timeout=10)
        resp.raise_for_status()
        alpaca_positions = resp.json() or []
    except Exception as e:
        logger.warning(
            "Position sync failed (non-fatal — fills already committed): %s", e,
        )
        return

    db_symbols: list[str] = []
    with conn.cursor() as cur:
        for p in alpaca_positions:
            db_sym = _alpaca_to_db_symbol(p["symbol"])
            db_symbols.append(db_sym)
            cur.execute(
                """
                INSERT INTO positions
                    (symbol, quantity, average_cost, unrealized_pnl, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (symbol) DO UPDATE SET
                    quantity       = EXCLUDED.quantity,
                    average_cost   = EXCLUDED.average_cost,
                    unrealized_pnl = EXCLUDED.unrealized_pnl,
                    updated_at     = NOW()
                """,
                (
                    db_sym,
                    str(p.get("qty", "0")),
                    str(p.get("avg_entry_price", "0")),
                    str(p.get("unrealized_pl", "0")),
                ),
            )

        # Zero out any DB rows not returned by Alpaca.  Using = ANY(%s) with
        # an empty array is safe on PG (no rows match), unlike NOT IN ().
        cur.execute(
            """
            UPDATE positions
            SET quantity = 0, unrealized_pnl = 0, updated_at = NOW()
            WHERE quantity != 0
              AND NOT (symbol = ANY(%s))
            """,
            (db_symbols,),
        )

    conn.commit()
    logger.info(
        "Position sync: %d open positions mirrored from Alpaca",
        len(alpaca_positions),
    )


def _safe_update_order_status(
    conn, client_order_id, status: str, broker_id_short: str,
) -> bool:
    """Update an order's status, swallowing DB errors.

    Returns True on success, False if the UPDATE was rejected (e.g. CHECK
    constraint violation on an unfamiliar status). On failure the aborted
    transaction is rolled back so the connection stays usable for the
    rest of the reconciliation loop and the downstream position sync.
    """
    try:
        _update_order_status(conn, client_order_id, status)
        return True
    except psycopg2.Error as e:
        # CheckViolation, OperationalError, etc. — never let one bad row
        # take down the whole reconcile loop.
        logger.warning(
            "Failed to update order %s → status=%s: %s",
            broker_id_short, status, e,
        )
        try:
            conn.rollback()
        except psycopg2.Error:
            pass
        return False


def reconcile(db_url: str, endpoint: str, session: requests.Session) -> dict:
    """Reconcile all SUBMITTED orders, then sync positions from Alpaca.

    Returns a dict with operator-visible counters:
        fills_written:   int  — fills inserted into PostgreSQL
        update_failures: int  — orders whose status UPDATE was rejected
                                (e.g. CHECK-constraint mismatch). Each
                                failure is logged but does not halt the
                                loop or skip the position sync.
        positions_synced: bool — whether _sync_positions_from_alpaca ran
                                 to completion (best-effort; non-fatal).

    Position sync always runs after the orders loop, even if individual
    UPDATE statements failed — the positions table is the source of
    truth for the morning report and must not depend on every single
    order reconciling cleanly.
    """
    conn = psycopg2.connect(db_url)
    fills_written = 0
    update_failures = 0
    positions_synced = False

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
            broker_id_short = broker_id[:8] if broker_id else "(none)"

            try:
                resp = session.get(f"{endpoint}/orders/{broker_id}", timeout=10)
                resp.raise_for_status()
                alpaca_order = resp.json()
                time.sleep(_API_SLEEP_S)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    # Order not found — may have been cancelled externally.
                    # Use "CANCELED" (Alpaca's spelling) — both spellings are
                    # valid post-migration_006.
                    logger.warning(
                        "Order %s not found on Alpaca — marking CANCELED",
                        broker_id_short,
                    )
                    if not _safe_update_order_status(
                        conn, client_id, "CANCELED", broker_id_short,
                    ):
                        update_failures += 1
                else:
                    logger.warning("Alpaca API error for %s: %s", broker_id_short, e)
                continue
            except Exception as e:
                logger.warning("Error checking order %s: %s", broker_id_short, e)
                continue

            alpaca_status = alpaca_order.get("status", "unknown")

            if alpaca_status == "filled":
                fill_price_str = alpaca_order.get("filled_avg_price") or "0"
                filled_qty_str = alpaca_order.get("filled_qty") or str(order["quantity"])

                try:
                    fill_price = float(fill_price_str)
                    filled_qty = float(filled_qty_str)
                except (ValueError, TypeError):
                    logger.warning("Cannot parse fill data for %s — skipping", broker_id_short)
                    continue

                if fill_price <= 0:
                    logger.warning("Zero fill price for %s — skipping", broker_id_short)
                    continue

                fill_id = str(uuid.uuid4())
                filled_at_str = alpaca_order.get("filled_at") or alpaca_order.get("updated_at")
                try:
                    filled_at = datetime.fromisoformat(
                        filled_at_str.replace("Z", "+00:00")
                    ) if filled_at_str else datetime.now(timezone.utc)
                except Exception:
                    filled_at = datetime.now(timezone.utc)

                try:
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
                except psycopg2.Error as e:
                    logger.warning(
                        "Failed to write fill for %s: %s — skipping", broker_id_short, e,
                    )
                    try:
                        conn.rollback()
                    except psycopg2.Error:
                        pass
                    update_failures += 1
                    continue

                if not _safe_update_order_status(
                    conn, client_id, "FILLED", broker_id_short,
                ):
                    update_failures += 1
                    # Fill is in DB even if status flip failed; do not double-count.
                fills_written += 1
                logger.info(
                    "FILLED: %s %s qty=%.4f @ $%.4f  fill_id=%s",
                    order["side"], symbol, filled_qty, fill_price, fill_id[:8],
                )
                # Telegram: fill confirmed
                _telegram_alert(
                    f"Fill Confirmed\n"
                    f"Symbol: {symbol} | Side: {order['side']} | "
                    f"Qty: {filled_qty:.4f} @ ${fill_price:.4f}",
                    level=order["side"],  # "BUY" or "SELL"
                )

            elif alpaca_status in ("canceled", "expired", "rejected"):
                if not _safe_update_order_status(
                    conn, client_id, alpaca_status.upper(), broker_id_short,
                ):
                    update_failures += 1
                else:
                    logger.info(
                        "Order %s %s for %s",
                        broker_id_short, alpaca_status.upper(), symbol,
                    )

            else:
                # Still pending/accepted — leave as SUBMITTED
                logger.debug(
                    "Order %s still %s for %s — will check next run",
                    broker_id_short, alpaca_status, symbol,
                )

        # After fills are committed, mirror Alpaca positions into our DB.
        # This is the only place `positions` is populated on Cloud Run —
        # the Rust OMS (the other writer) does not run there (ALPACA_DIRECT=1).
        # Run unconditionally — positions sync must not depend on every
        # individual order UPDATE succeeding (pghfd 2026-04-25 incident).
        try:
            _sync_positions_from_alpaca(conn, session, endpoint)
            positions_synced = True
        except Exception as e:
            logger.warning("Position sync raised — non-fatal: %s", e)

    finally:
        conn.close()

    return {
        "fills_written": fills_written,
        "update_failures": update_failures,
        "positions_synced": positions_synced,
    }


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

    result = reconcile(db_url, endpoint, session)
    session.close()
    logger.info(
        "Done — %d fill(s) written, %d update failure(s), positions_synced=%s",
        result["fills_written"],
        result["update_failures"],
        result["positions_synced"],
    )


if __name__ == "__main__":
    main()
