# trading-system/scripts/tests/conftest.py
#
# Shared fixtures for cron-flow integration tests.
#
# Spins up a clean local Postgres state per test against the real
# Docker container (postgres:16 on localhost:5432) so CHECK constraints,
# generated columns, FKs, and triggers all fire — these are the exact
# boundaries the 2026-04-24 + 2026-04-25 incidents lived at.
#
# Layout:
#   schema_applied     — session-scoped, applies init.sql + migrations 002,003,006
#   db_conn            — function-scoped, truncates mutable tables and yields a
#                        live psycopg2 connection
#   alpaca_mock        — function-scoped, returns a router-style MagicMock
#                        session that dispatches /v2/orders/{id}, /v2/positions,
#                        /v2/account based on the requested URL
#   silence_telegram   — autouse, no-op send_alert so tests never hit network
#   fast_reconcile     — autouse, sets reconcile._API_SLEEP_S=0 for fast loops

from __future__ import annotations

import os
import pathlib
import sys
from typing import Iterator
from unittest.mock import MagicMock

import psycopg2
import pytest

# Make the scripts/ dir importable so tests can `import reconcile_alpaca_fills`.
_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

_ROOT = _SCRIPTS_DIR.parent
_INFRA = _ROOT / "infra" / "postgres"

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://quantai:quantai_dev_2026@localhost:5432/quantai",
)

# init.sql is idempotent (CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT
# EXISTS).  The triggers are not — re-running raises.  We strip them on
# re-apply because they're already created by the docker-compose volume mount.
_SCHEMA_FILES = [
    _INFRA / "init.sql",
    _INFRA / "migration_002_system_metrics.sql",
    _INFRA / "migration_003_signal_type.sql",
    _INFRA / "migration_006_orders_status_lifecycle.sql",
]

# Tables we wipe before every test.  Order matters only if not using CASCADE;
# fills→orders is the lone FK and CASCADE handles it.
_MUTABLE_TABLES = (
    "fills", "orders", "positions",
    "daily_pnl", "signals", "system_metrics", "risk_events", "ohlcv",
)


@pytest.fixture(scope="session")
def schema_applied() -> Iterator[None]:
    """Apply schema + idempotent migrations once per test session.

    Skips the entire test module if the local Postgres is unreachable —
    keeps CI/dev environments without Docker green.
    """
    try:
        conn = psycopg2.connect(DB_URL, connect_timeout=2)
    except psycopg2.Error as e:
        pytest.skip(f"Local Postgres unreachable ({e}); skipping integration tests.")
        return

    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for sql_path in _SCHEMA_FILES:
                sql = sql_path.read_text()
                # init.sql declares triggers without IF NOT EXISTS — when we
                # re-run on an already-initialised DB they raise.  Strip the
                # CREATE TRIGGER blocks; the original docker-compose run made
                # them once and that's enough.
                if sql_path.name == "init.sql":
                    sql = _strip_create_triggers(sql)
                cur.execute(sql)
    finally:
        conn.close()
    yield


def _strip_create_triggers(sql: str) -> str:
    """Remove `CREATE TRIGGER … EXECUTE FUNCTION …;` statements.

    `CREATE TRIGGER` has no `IF NOT EXISTS` form in PG 16 → re-applying
    init.sql crashes.  Triggers are created once by the docker-compose
    init script and we don't need them re-applied.
    """
    out: list[str] = []
    skip = False
    for line in sql.splitlines():
        stripped = line.lstrip()
        if stripped.upper().startswith("CREATE TRIGGER"):
            skip = True
        if skip:
            if line.rstrip().endswith(";"):
                skip = False
            continue
        out.append(line)
    return "\n".join(out)


@pytest.fixture
def db_conn(schema_applied) -> Iterator[psycopg2.extensions.connection]:
    """Yield a psycopg2 connection with all mutable tables truncated.

    TRUNCATE … RESTART IDENTITY CASCADE handles fills→orders FK and resets
    BIGSERIAL counters.  Connection is closed at teardown.
    """
    conn = psycopg2.connect(DB_URL)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"TRUNCATE {', '.join(_MUTABLE_TABLES)} "
                    "RESTART IDENTITY CASCADE"
                )
        yield conn
    finally:
        conn.close()


# ── Alpaca mock ───────────────────────────────────────────────────────────────

class _AlpacaMockState:
    """Holds whatever the mock should return for each Alpaca endpoint.

    Tests mutate the dicts/lists directly:
        alpaca_mock.orders["broker-id-1"] = {"status": "filled", ...}
        alpaca_mock.positions = [{...}]
        alpaca_mock.account = {"equity": "...", "last_equity": "..."}
    """

    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.positions: list[dict] = []
        self.account: dict = {
            "equity": "100000.00", "last_equity": "100000.00",
            "status": "ACTIVE",
        }
        self.fail_positions = False  # raise on GET /v2/positions
        self.fail_account = False    # raise on GET /v2/account


@pytest.fixture
def alpaca_mock() -> _AlpacaMockState:
    """Return a router state object that backs the mock Alpaca session."""
    return _AlpacaMockState()


@pytest.fixture
def alpaca_session(alpaca_mock: _AlpacaMockState) -> MagicMock:
    """A requests.Session-like MagicMock that routes by URL substring."""
    import requests

    def _get(url: str, *_, **__):  # type: ignore[no-untyped-def]
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock(return_value=None)

        if "/positions" in url:
            if alpaca_mock.fail_positions:
                raise requests.ConnectionError("mock: positions endpoint down")
            resp.json = MagicMock(return_value=alpaca_mock.positions)
            return resp
        if url.endswith("/account"):
            if alpaca_mock.fail_account:
                raise requests.ConnectionError("mock: account endpoint down")
            resp.json = MagicMock(return_value=alpaca_mock.account)
            return resp
        if "/orders/" in url:
            broker_id = url.rsplit("/", 1)[-1]
            order = alpaca_mock.orders.get(broker_id)
            if order is None:
                # 404 → reconcile marks order CANCELED
                err = requests.HTTPError("404 not found")
                err.response = MagicMock(status_code=404)
                resp.raise_for_status = MagicMock(side_effect=err)
                resp.status_code = 404
                return resp
            resp.json = MagicMock(return_value=order)
            return resp

        raise AssertionError(f"unexpected mock URL: {url}")

    session = MagicMock()
    session.get.side_effect = _get
    return session


# ── Speed + isolation ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fast_reconcile(monkeypatch):
    """Strip the 0.3s per-order Alpaca rate-limit sleep so the suite is fast."""
    import reconcile_alpaca_fills as rec
    monkeypatch.setattr(rec, "_API_SLEEP_S", 0)


@pytest.fixture(autouse=True)
def database_url_env(monkeypatch):
    """Pin DATABASE_URL for scripts that read it at module load."""
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    # morning_report loads _DB_URL at import time — refresh it after we set env
    import importlib
    import morning_report
    importlib.reload(morning_report)


@pytest.fixture(autouse=True)
def silence_telegram(monkeypatch, database_url_env):
    """No-op the Telegram send_alert so tests never depend on bot creds.

    Depends on database_url_env so the morning_report reload happens FIRST;
    otherwise the reload would discard our send_alert patch.
    """
    import telegram_alert
    monkeypatch.setattr(telegram_alert, "send_alert", lambda *a, **kw: True)
    import morning_report
    monkeypatch.setattr(morning_report, "send_alert", lambda *a, **kw: True)
    import reconcile_alpaca_fills as rec
    monkeypatch.setattr(rec, "_telegram_alert", lambda *a, **kw: True)
