# trading-system/strategy/src/bridge/alpaca_direct.py
#
# Direct Python → Alpaca REST order submission.
#
# Drop-in replacement for TradingBridgeClient when ALPACA_DIRECT=1.
# Bypasses the Rust gRPC engine entirely — signals go straight to
# Alpaca paper-api.alpaca.markets via REST.
#
# Why this exists:
#   Cloud Run Jobs have no Redis, so the Rust OMS cannot start.
#   Rust main.rs also uses PaperBroker (not AlpacaBroker), so
#   even if it could run, it would not submit real Alpaca orders.
#
# Interface contract:
#   Mirrors TradingBridgeClient exactly:
#     health_check()  → HealthStatus
#     submit_signal() → Optional[BridgeResponse]
#     context manager protocol
#
# Risk checks applied (matching Rust risk engine limits from CLAUDE.md):
#   - score >= 0.55 (min signal score)
#   - stop_loss required
#   - qty > 0
#   - max 5% of portfolio equity per new position
#   - max 10 open positions (Alpaca positions count)
#   - skip if already long on BUY, skip if no position on SELL
#
# Order lifecycle:
#   Submit → INSERT orders (SUBMITTED) → Alpaca queue → fills overnight
#   Fills reconciled by scripts/reconcile_alpaca_fills.py (next-day run)
#
# Unsupported symbols (skipped silently):
#   GBP-USD, EUR-USD (FX — Alpaca paper does not support FX trading)
#   BNB-USD          (not listed on Alpaca)

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

import requests

from ..signals import Direction, SignalResult
from .client import BridgeResponse, HealthStatus

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_DEFAULT_ENDPOINT = "https://paper-api.alpaca.markets/v2"
_GCP_PROJECT = os.environ.get("GCP_PROJECT_ID", "quantai-trading-paper")

# Symbols not tradeable on Alpaca paper — skip order submission silently.
_ALPACA_UNSUPPORTED = frozenset({"GBP-USD", "EUR-USD", "BNB-USD"})

# Risk limits — must match core/src/risk/mod.rs
_MAX_POSITION_PCT = Decimal("0.05")   # 5% of portfolio per position
_MAX_OPEN_POSITIONS = 10
_MIN_SIGNAL_SCORE = 0.55

# Alpaca API rate limit: 200 req/min on paper. A brief sleep keeps us safe.
_API_SLEEP_S = 0.3


# ── Symbol translation ─────────────────────────────────────────────────────────

def _to_alpaca_symbol(yf_symbol: str) -> Optional[str]:
    """Translate a yfinance-style symbol to Alpaca format.

    Returns None for symbols not supported on Alpaca paper.

    Examples:
        AAPL    → AAPL      (stock — unchanged)
        BTC-USD → BTCUSD    (crypto — remove dash)
        GBP-USD → None      (FX — unsupported)
    """
    if yf_symbol in _ALPACA_UNSUPPORTED:
        return None
    # Crypto: BTC-USD → BTCUSD
    if yf_symbol.endswith("-USD") and yf_symbol not in ("GBP-USD", "EUR-USD"):
        return yf_symbol.replace("-", "")
    return yf_symbol


# ── Credential loading ─────────────────────────────────────────────────────────

def _gcloud_secret(secret_id: str) -> Optional[str]:
    """Read a secret via gcloud CLI subprocess. Works locally (ADC) and on Cloud Run."""
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


def _load_credentials() -> tuple[str, str, str]:
    """Return (endpoint, api_key, secret_key).

    Priority:
      1. Env vars (ALPACA_API_KEY, ALPACA_SECRET_KEY)
      2. gcloud CLI subprocess  (works locally + Cloud Run)
      3. GCP Secret Manager Python SDK (ADC, Cloud Run service accounts)

    Raises RuntimeError if credentials cannot be loaded.
    """
    endpoint = os.environ.get("ALPACA_ENDPOINT", _DEFAULT_ENDPOINT)

    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")

    if not api_key:
        api_key = _gcloud_secret("alpaca-api-key")
    if not secret_key:
        secret_key = _gcloud_secret("alpaca-secret-key")

    if not api_key or not secret_key:
        try:
            from ..gcp import get_secret
            if not api_key:
                api_key = get_secret("alpaca-api-key", _GCP_PROJECT)
            if not secret_key:
                secret_key = get_secret("alpaca-secret-key", _GCP_PROJECT)
        except Exception as e:
            raise RuntimeError(
                f"Cannot load Alpaca credentials from env, gcloud, or Secret Manager: {e}"
            ) from e

    if not api_key:
        raise RuntimeError("ALPACA_API_KEY not set and not in Secret Manager")
    if not secret_key:
        raise RuntimeError("ALPACA_SECRET_KEY not set and not in Secret Manager")

    # Safety: reject live endpoint — paper only
    if "api.alpaca.markets" in endpoint and "paper-api" not in endpoint:
        raise RuntimeError(
            "SAFETY: ALPACA_ENDPOINT appears to be the live endpoint. "
            "Live trading requires explicit Phase 4 authorization."
        )

    return endpoint, api_key, secret_key


# ── AlpacaDirectClient ────────────────────────────────────────────────────────

class AlpacaDirectClient:
    """Alpaca REST order submission — drop-in for TradingBridgeClient.

    Usage (identical to TradingBridgeClient)::

        with AlpacaDirectClient() as client:
            health = client.health_check()
            response = client.submit_signal(signal, current_price=180.0)
    """

    def __init__(self) -> None:
        self._endpoint: str = ""
        self._session: Optional[requests.Session] = None
        self._equity: Optional[Decimal] = None
        # Task 15: track symbols submitted this session — defense-in-depth against
        # duplicate submissions within a single run_live() invocation.
        self._submitted_symbols: set[str] = set()

    # ── Connection management ─────────────────────────────────────────────────

    def connect(self) -> None:
        endpoint, api_key, secret_key = _load_credentials()
        self._endpoint = endpoint
        self._session = requests.Session()
        self._session.headers.update({
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
            "Content-Type": "application/json",
        })
        self._submitted_symbols = set()  # reset on reconnect
        logger.info("AlpacaDirectClient: connected to %s", endpoint)

    def disconnect(self) -> None:
        if self._session:
            self._session.close()
            self._session = None
        logger.info("AlpacaDirectClient: disconnected")

    def __enter__(self) -> "AlpacaDirectClient":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()

    def _sess(self) -> requests.Session:
        if self._session is None:
            self.connect()
        return self._session  # type: ignore[return-value]

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_account(self) -> dict:
        resp = self._sess().get(f"{self._endpoint}/account", timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _get_positions(self) -> dict[str, dict]:
        """Return {alpaca_symbol: position_data} for all open Alpaca positions."""
        resp = self._sess().get(f"{self._endpoint}/positions", timeout=10)
        resp.raise_for_status()
        positions = resp.json()
        return {p["symbol"]: p for p in positions}

    def _get_open_orders(self, alpaca_symbol: str) -> list[dict]:
        """Return pending/queued orders for alpaca_symbol via GET /orders.

        Used as a cross-invocation dedup guard: a pending order that was
        submitted in a previous Cloud Run invocation will not appear in
        _get_positions() (which returns only filled positions), but will
        appear here.  See Task 15 (KGC double-submission bug).
        """
        resp = self._sess().get(
            f"{self._endpoint}/orders",
            params={"status": "open", "symbols": alpaca_symbol},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def _get_clock(self) -> dict:
        resp = self._sess().get(f"{self._endpoint}/clock", timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _submit_market_order(
        self,
        alpaca_symbol: str,
        side: str,
        qty: str,
        client_order_id: str,
    ) -> dict:
        """POST /orders — submit a market order."""
        payload = {
            "symbol": alpaca_symbol,
            "qty": qty,
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "client_order_id": client_order_id,
        }
        resp = self._sess().post(
            f"{self._endpoint}/orders", json=payload, timeout=10
        )
        resp.raise_for_status()
        return resp.json()

    def _close_position(self, alpaca_symbol: str) -> dict:
        """DELETE /positions/{symbol} — liquidate entire position."""
        resp = self._sess().delete(
            f"{self._endpoint}/positions/{alpaca_symbol}", timeout=10
        )
        resp.raise_for_status()
        return resp.json()

    def _record_order_pg(
        self,
        client_order_id: str,
        broker_order_id: str,
        symbol: str,
        side: str,
        qty: Decimal,
        stop_loss: Decimal,
        signal_score: float,
        strategy_id: str,
        signal_type: str = "momentum",
    ) -> None:
        """INSERT into orders table with status=SUBMITTED.

        Fills are recorded by reconcile_alpaca_fills.py after market open.
        Non-fatal: DB failures log a warning but do not abort trading.
        """
        db_url = os.environ.get(
            "DATABASE_URL",
            "postgres://quantai:quantai_dev_2026@localhost:5432/quantai",
        )
        try:
            import psycopg2

            conn = psycopg2.connect(db_url)
            conn.autocommit = False
            now = datetime.now(timezone.utc)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO orders (
                            client_order_id, broker_order_id, symbol, side,
                            order_type, quantity, stop_loss, signal_score,
                            strategy_id, signal_type, status, created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, 'MARKET', %s, %s, %s, %s,
                                  %s, 'SUBMITTED', %s, %s)
                        ON CONFLICT (client_order_id) DO NOTHING
                        """,
                        (
                            client_order_id,
                            broker_order_id,
                            symbol,
                            side,
                            str(qty),
                            str(stop_loss),
                            signal_score,
                            strategy_id,
                            signal_type,
                            now,
                            now,
                        ),
                    )
                conn.commit()
                logger.info(
                    "Order recorded: %s %s %s qty=%s signal_type=%s (status=SUBMITTED)",
                    side, symbol, client_order_id[:12], qty, signal_type,
                )
            except Exception as e:
                conn.rollback()
                logger.warning("Order DB insert failed (non-fatal): %s", e)
            finally:
                conn.close()
        except ImportError:
            logger.warning("psycopg2 not available — skipping DB record")
        except Exception as e:
            logger.warning("DB connection failed (non-fatal): %s", e)

    # ── Public API ────────────────────────────────────────────────────────────

    def health_check(self) -> HealthStatus:
        """Check Alpaca account health.

        Returns HealthStatus with portfolio_value from Alpaca equity.
        Raises requests.HTTPError on API failure.
        """
        account = self._get_account()
        equity = account.get("equity", "0")
        self._equity = Decimal(equity)
        status = account.get("status", "UNKNOWN")
        healthy = status == "ACTIVE"

        clock = self._get_clock()
        is_open = clock.get("is_open", False)
        next_open = clock.get("next_open", "unknown")
        if not is_open:
            logger.info("Market is CLOSED — orders will queue until %s", next_open)

        positions = self._get_positions()

        return HealthStatus(
            healthy=healthy,
            paper_mode=True,  # enforced by _load_credentials
            portfolio_value=equity,
            open_orders=len(positions),
            pubsub_active=False,  # Pub/Sub handled separately
        )

    def submit_signal(
        self,
        signal: SignalResult,
        current_price: float,
        quantity_override: Optional[Decimal] = None,
    ) -> Optional[BridgeResponse]:
        """Submit a trading signal as an Alpaca paper order.

        Args:
            signal:            SignalResult from the strategy.
            current_price:     Latest market price (for position sizing).
            quantity_override: Override signal's suggested_quantity if set.

        Returns:
            BridgeResponse if order was submitted.
            None if signal was HOLD or symbol is unsupported.

        Risk checks applied (matching Rust risk engine):
            - HOLD signals filtered
            - Unsupported symbols skipped
            - Score < 0.55 rejected
            - Missing stop_loss rejected
            - Max 5% position size
            - Max 10 open positions
            - No double-buy / no-position SELL
        """
        # ── Pre-checks ────────────────────────────────────────────────────────

        if signal.direction == Direction.HOLD:
            logger.debug("%s: HOLD — skipped", signal.symbol)
            return None

        alpaca_symbol = _to_alpaca_symbol(signal.symbol)
        if alpaca_symbol is None:
            logger.info("%s: not tradeable on Alpaca — skipped", signal.symbol)
            return None

        if signal.score < _MIN_SIGNAL_SCORE:
            logger.warning(
                "%s: score %.4f < %.2f minimum — rejected",
                signal.symbol, signal.score, _MIN_SIGNAL_SCORE,
            )
            return BridgeResponse(
                accepted=False,
                order_id="",
                status="REJECTED",
                message=f"score {signal.score:.4f} below minimum {_MIN_SIGNAL_SCORE}",
            )

        if signal.suggested_stop_loss is None:
            logger.warning("%s: missing stop_loss — rejected", signal.symbol)
            return BridgeResponse(
                accepted=False,
                order_id="",
                status="REJECTED",
                message="stop_loss required",
            )

        qty = quantity_override or signal.suggested_quantity
        if qty is None or qty <= Decimal("0"):
            logger.warning("%s: invalid qty %s — rejected", signal.symbol, qty)
            return BridgeResponse(
                accepted=False,
                order_id="",
                status="REJECTED",
                message=f"invalid quantity: {qty}",
            )

        # ── Portfolio context ─────────────────────────────────────────────────

        try:
            account = self._get_account()
            equity = Decimal(account.get("equity", "100000"))
            self._equity = equity
            time.sleep(_API_SLEEP_S)

            positions = self._get_positions()
            time.sleep(_API_SLEEP_S)
        except Exception as e:
            logger.error("%s: Alpaca API call failed: %s", signal.symbol, e)
            return BridgeResponse(
                accepted=False,
                order_id="",
                status="ERROR",
                message=str(e),
            )

        # ── Position limit ────────────────────────────────────────────────────

        if len(positions) >= _MAX_OPEN_POSITIONS:
            logger.warning(
                "%s: %d open positions >= max %d — rejected",
                signal.symbol, len(positions), _MAX_OPEN_POSITIONS,
            )
            return BridgeResponse(
                accepted=False,
                order_id="",
                status="REJECTED",
                message=f"max open positions ({_MAX_OPEN_POSITIONS}) reached",
            )

        # ── Position sizing: cap at 5% of portfolio ───────────────────────────

        max_qty_by_pct = (equity * _MAX_POSITION_PCT) / Decimal(str(current_price))
        qty = min(qty, max_qty_by_pct)
        # Round to reasonable precision (whole shares for stocks, 4dp for crypto)
        if "USD" in signal.symbol:
            qty = qty.quantize(Decimal("0.0001"))
        else:
            qty = qty.quantize(Decimal("1"))
        if qty <= Decimal("0"):
            logger.warning(
                "%s: position size rounded to 0 (price=%.4f equity=%.2f) — rejected",
                signal.symbol, current_price, float(equity),
            )
            return BridgeResponse(
                accepted=False,
                order_id="",
                status="REJECTED",
                message="position size rounds to 0",
            )

        # ── Direction-specific logic ──────────────────────────────────────────

        features = signal.features or {}
        if not features and signal.direction != Direction.HOLD:
            logger.warning(
                "%s: signal has no features dict — defaulting signal_type to 'momentum'",
                signal.symbol,
            )
        is_trend_ride = bool(features.get("trend_ride", False))
        signal_type = "trend_ride" if is_trend_ride else "momentum"
        signal_type_code = "tr" if is_trend_ride else "mom"
        client_order_id = f"quantai-{signal_type_code}-{uuid.uuid4()}"
        side = signal.direction.value.lower()  # "buy" or "sell"

        if signal.direction == Direction.BUY:
            # Guard 1: already have a filled position.
            if alpaca_symbol in positions:
                logger.info(
                    "%s: already long %s shares — skipping BUY",
                    signal.symbol,
                    positions[alpaca_symbol].get("qty", "?"),
                )
                return BridgeResponse(
                    accepted=False,
                    order_id="",
                    status="SKIPPED",
                    message="already long position",
                )

            # Guard 2 (Task 15): session-level dedup — prevents a second BUY for
            # the same symbol within the same run_live() invocation.  Catches the
            # case where the symbol loop somehow reaches the same ticker twice.
            if alpaca_symbol in self._submitted_symbols:
                logger.warning(
                    "%s: duplicate BUY within same session — skipping (seen_symbols guard)",
                    signal.symbol,
                )
                return BridgeResponse(
                    accepted=False,
                    order_id="",
                    status="SKIPPED",
                    message="duplicate BUY within session",
                )

            # Guard 3 (Task 15): cross-invocation dedup — check for a pending order
            # submitted in a previous Cloud Run invocation that has not yet been
            # filled.  Such orders are invisible to _get_positions() but visible
            # via GET /orders?status=open.  Non-fatal: if the API call fails we
            # log a warning and proceed rather than blocking legitimate trades.
            try:
                open_orders = self._get_open_orders(alpaca_symbol)
                time.sleep(_API_SLEEP_S)
                pending_buys = [o for o in open_orders if o.get("side") == "buy"]
                if pending_buys:
                    logger.warning(
                        "%s: %d pending BUY order(s) already exist — skipping "
                        "(cross-invocation dedup guard)",
                        signal.symbol, len(pending_buys),
                    )
                    return BridgeResponse(
                        accepted=False,
                        order_id="",
                        status="SKIPPED",
                        message=(
                            f"pending BUY order already exists "
                            f"({len(pending_buys)} open order(s))"
                        ),
                    )
            except Exception as e:
                logger.warning(
                    "%s: could not check open orders (non-fatal, proceeding): %s",
                    signal.symbol, e,
                )

            logger.info(
                "AlpacaDirect BUY %s qty=%s @ $%.4f  score=%.4f  stop=%s",
                signal.symbol, qty, current_price,
                signal.score, signal.suggested_stop_loss,
            )

            try:
                order = self._submit_market_order(
                    alpaca_symbol=alpaca_symbol,
                    side="buy",
                    qty=str(qty),
                    client_order_id=client_order_id,
                )
            except requests.HTTPError as e:
                body = e.response.text if e.response is not None else str(e)
                logger.error("%s: POST /orders failed: %s", signal.symbol, body)
                return BridgeResponse(
                    accepted=False,
                    order_id="",
                    status="ERROR",
                    message=body[:200],
                )

        elif signal.direction == Direction.SELL:
            if alpaca_symbol not in positions:
                logger.info(
                    "%s: no long position — nothing to SELL, skipping",
                    signal.symbol,
                )
                return BridgeResponse(
                    accepted=False,
                    order_id="",
                    status="SKIPPED",
                    message="no position to sell",
                )

            logger.info(
                "AlpacaDirect SELL (close) %s  score=%.4f",
                signal.symbol, signal.score,
            )

            try:
                order = self._close_position(alpaca_symbol)
            except requests.HTTPError as e:
                body = e.response.text if e.response is not None else str(e)
                logger.error(
                    "%s: DELETE /positions/%s failed: %s",
                    signal.symbol, alpaca_symbol, body,
                )
                return BridgeResponse(
                    accepted=False,
                    order_id="",
                    status="ERROR",
                    message=body[:200],
                )

        else:
            return None  # unreachable

        broker_order_id = order.get("id", "")
        alpaca_status = order.get("status", "unknown")

        logger.info(
            "Alpaca order submitted: %s %s  broker_id=%s  status=%s",
            side.upper(), signal.symbol, broker_order_id, alpaca_status,
        )

        # Task 15: mark as submitted so session-level guard catches any retry
        self._submitted_symbols.add(alpaca_symbol)

        # ── Record in PostgreSQL ──────────────────────────────────────────────

        self._record_order_pg(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            symbol=signal.symbol,
            side=side.upper(),
            qty=qty,
            stop_loss=signal.suggested_stop_loss,
            signal_score=signal.score,
            strategy_id=signal.strategy_id,
            signal_type=signal_type,
        )

        return BridgeResponse(
            accepted=True,
            order_id=broker_order_id,
            status=alpaca_status,
            message=f"submitted via Alpaca direct  client_id={client_order_id[:8]}",
        )
