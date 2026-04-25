# trading-system/scripts/tests/test_cron_flow_e2e.py
#
# End-to-end integration coverage for the daily cron pipeline:
#
#   reconcile_alpaca_fills.reconcile()
#       ↓ (writes fills + UPSERTs positions)
#   update_daily_pnl.update()
#       ↓ (writes daily_pnl from Alpaca equity delta + DB unrealized)
#   morning_report.build_report()
#       ↓ (renders Telegram message)
#
# Each test exercises the full chain against the local Docker Postgres so
# CHECK constraints, generated columns (total_pnl, gross_value), FKs and
# triggers all fire — these are the seams the past two P0/P1 incidents
# lived at and that mocked-cursor unit tests cannot cover.
#
# Past incidents this suite locks in regression coverage for:
#   - 2026-04-23 P0 — false -$49,234 drawdown (positions empty + cash-flow
#                     CASE).  Scenarios 1, 3 prove total_pnl is now derived
#                     from Alpaca equity delta and never approaches -$49k.
#   - 2026-04-25 P1 — orders.status CheckViolation crashed reconcile and
#                     left positions empty.  Scenario 2 proves the
#                     CANCELED branch round-trips through the live CHECK
#                     constraint without crashing.
#
# All Alpaca traffic is mocked.  No external network is touched.

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

import psycopg2
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import reconcile_alpaca_fills as rec  # noqa: E402
import update_daily_pnl as upd  # noqa: E402
import morning_report  # noqa: E402


# ── Seeding helpers ───────────────────────────────────────────────────────────

_ALPACA_ENDPOINT = "https://paper-api.alpaca.markets/v2"


def _seed_submitted_orders(
    conn, symbols: Iterable[str], qty: int = 10, price: float = 100.0,
) -> list[tuple[str, str, str]]:
    """Insert one SUBMITTED order per symbol; return list of (client_id,
    broker_id, symbol) tuples for later mock setup."""
    rows: list[tuple[str, str, str]] = []
    with conn:
        with conn.cursor() as cur:
            for sym in symbols:
                broker_id = f"alp-{uuid.uuid4().hex[:12]}"
                client_id = f"quantai-tr-{broker_id}"
                cur.execute(
                    """
                    INSERT INTO orders (
                        client_order_id, broker_order_id, symbol, side,
                        order_type, quantity, stop_loss, signal_score,
                        strategy_id, signal_type, status
                    )
                    VALUES (%s, %s, %s, 'BUY', 'MARKET', %s, %s, %s,
                            %s, %s, 'SUBMITTED')
                    """,
                    (
                        client_id, broker_id, sym, qty, price * 0.95,
                        0.72, "trend_ride", "trend_ride",
                    ),
                )
                rows.append((client_id, broker_id, sym))
    return rows


def _alpaca_filled_payload(
    broker_id: str, symbol: str, qty: int, price: float,
) -> dict:
    return {
        "id": broker_id,
        "symbol": symbol,
        "status": "filled",
        "side": "buy",
        "qty": str(qty),
        "filled_qty": str(qty),
        "filled_avg_price": f"{price:.4f}",
        "filled_at": datetime.now(timezone.utc).isoformat(),
    }


def _alpaca_position_payload(
    symbol: str, qty: int, price: float, unrealized: float = 0.0,
) -> dict:
    return {
        "symbol": symbol,
        "qty": str(qty),
        "avg_entry_price": f"{price:.4f}",
        "unrealized_pl": f"{unrealized:.4f}",
        "side": "long",
    }


def _seed_daily_pnl_history(conn, days_back: int = 5) -> None:
    """Seed a few prior daily_pnl rows so the gate has data for Sharpe.

    Rows go from (today - days_back) up to (today - 1), each ~+$50/day so
    Sharpe is positive and the gate computes successfully.
    """
    today = date.today()
    starting = Decimal("100000.00")
    with conn:
        with conn.cursor() as cur:
            for i in range(days_back, 0, -1):
                d = today - timedelta(days=i)
                ending = starting + Decimal("50.00")
                cur.execute(
                    """
                    INSERT INTO daily_pnl
                        (trading_date, starting_value, ending_value,
                         realized_pnl, unrealized_pnl, num_trades)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (trading_date) DO UPDATE SET
                        ending_value   = EXCLUDED.ending_value,
                        realized_pnl   = EXCLUDED.realized_pnl,
                        unrealized_pnl = EXCLUDED.unrealized_pnl,
                        num_trades     = EXCLUDED.num_trades
                    """,
                    (d, starting, ending, "50.00", "0.00", 0),
                )
                starting = ending


def _patch_alpaca_session(monkeypatch, session) -> None:
    """Inject our mock session into update_daily_pnl._load_alpaca_session."""
    monkeypatch.setattr(
        upd, "_load_alpaca_session",
        lambda: (session, _ALPACA_ENDPOINT),
    )


# ── 1. Happy path — 10 mixed-sector fills, sane morning report ────────────────

def test_happy_path_full_cron(db_conn, alpaca_mock, alpaca_session, monkeypatch):
    """10 SUBMITTED orders → all fill → daily_pnl + morning report sane.

    Asserts the entire seam: fills written, positions populated, daily_pnl
    invariant intact, all 7 morning-report sections render, no false $49k,
    HIGH CONCENTRATION does NOT fire (mixed sectors).
    """
    # 10 symbols across 5 different sectors (precious_metals, equity_broad,
    # equity_tech, bonds, uranium, crypto).  Confirms HIGH CONCENTRATION
    # only fires on real concentration, not just any non-empty book.
    symbols_prices = [
        ("GLD",     11,  434.35),  # precious_metals
        ("AAPL",    25,  175.20),  # equity_single
        ("SPY",     12,  515.40),  # equity_broad
        ("QQQ",      8,  445.10),  # equity_broad
        ("TLT",     30,   95.30),  # bonds
        ("URA",     45,   28.90),  # uranium
        ("GDX",     20,   31.40),  # precious_metals
        ("XLK",     10,  205.55),  # equity_tech
        ("IWM",     15,  205.30),  # equity_broad
        ("AEM",     14,   68.10),  # precious_metals
    ]

    seeded = _seed_submitted_orders(
        db_conn, [s for s, *_ in symbols_prices],
    )
    # Seed history so 90-day gate has Sharpe data
    _seed_daily_pnl_history(db_conn, days_back=5)

    # Mock Alpaca: every order fills, every symbol has an open position
    for (client_id, broker_id, sym), (_, qty, price) in zip(seeded, symbols_prices):
        alpaca_mock.orders[broker_id] = _alpaca_filled_payload(
            broker_id, sym, qty, price,
        )
    alpaca_mock.positions = [
        _alpaca_position_payload(s, q, p, unrealized=15.50)
        for s, q, p in symbols_prices
    ]
    # Equity moved from $100k → $100,155 (10 positions × $15.50 unrealized)
    alpaca_mock.account = {
        "equity": "100155.00", "last_equity": "100050.00",
        "status": "ACTIVE",
    }

    # ── Step 1: reconcile ─────────────────────────────────────────────────────
    result = rec.reconcile(
        os.environ["DATABASE_URL"], _ALPACA_ENDPOINT, alpaca_session,
    )
    assert result["fills_written"] == 10, result
    assert result["update_failures"] == 0, result
    assert result["positions_synced"] is True, result

    # All 10 orders flipped to FILLED, 10 fills inserted
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM fills")
        assert cur.fetchone()[0] == 10
        cur.execute("SELECT COUNT(*) FROM orders WHERE status = 'FILLED'")
        assert cur.fetchone()[0] == 10
        cur.execute("SELECT COUNT(*) FROM positions WHERE quantity != 0")
        assert cur.fetchone()[0] == 10

    # ── Step 2: update_daily_pnl ──────────────────────────────────────────────
    _patch_alpaca_session(monkeypatch, alpaca_session)
    upd.update(os.environ["DATABASE_URL"])

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT starting_value, ending_value, realized_pnl, "
            "unrealized_pnl, total_pnl, num_trades "
            "FROM daily_pnl WHERE trading_date = CURRENT_DATE"
        )
        sv, ev, real, unreal, total, n = cur.fetchone()

    # Alpaca delta = 100155 - 100050 = $105
    assert total == Decimal("105.0000000000")
    # Generated column invariant: total = realized + unrealized (PG enforces)
    assert total == real + unreal
    # Invariant carried into ending_value: ev = sv + total
    assert ev == sv + total
    # Crucial regression: total is NOT anywhere near -$49k (the 2026-04-23 bug)
    assert total > Decimal("-1000"), (
        f"total_pnl={total} smells like the 2026-04-23 BUY-notional bug"
    )
    assert n == 10  # 10 fills today

    # ── Step 3: morning_report ────────────────────────────────────────────────
    message, level = morning_report.build_report()

    assert "QuantAI Morning Report" in message
    assert "Market Regime" in message
    assert "Signals" in message
    assert "P&L Summary" in message
    assert "90-Day Gate Progress" in message
    assert "Sector Exposure" in message
    assert "Next run" in message
    # Mixed sectors — HIGH CONCENTRATION must NOT fire
    assert "HIGH CONCENTRATION" not in message, (
        "HIGH CONCENTRATION fired on a 5-sector book — false positive"
    )
    assert "-$49" not in message  # bug-signature sniff
    # Today P&L should round-trip into the report (line: "Today: +$105.00")
    assert "+$105.00" in message
    # MaxDD low → no CRITICAL alert
    assert level in {"SUMMARY", "WARNING"}


# ── 2. Cancelled order — CANCELED branch round-trips through CHECK constraint ─

def test_cancelled_order_lingering(db_conn, alpaca_mock, alpaca_session,
                                   monkeypatch):
    """A SUBMITTED order Alpaca reports as `canceled` must not crash reconcile.

    Locks in 2026-04-25 P1 fix: migration_006 + _safe_update_order_status.
    Without the fix, the orders.status CHECK constraint would raise
    CheckViolation, exception would escape the loop, _sync_positions_from_alpaca
    would be skipped, and morning_report would degrade.
    """
    seeded = _seed_submitted_orders(db_conn, ["KGC"], qty=153, price=32.35)
    client_id, broker_id, _ = seeded[0]

    alpaca_mock.orders[broker_id] = {
        "id": broker_id, "symbol": "KGC",
        "status": "canceled",  # American spelling — exact 2026-04-25 trigger
        "side": "buy", "qty": "153",
    }
    alpaca_mock.positions = []  # cancelled order → no open position
    alpaca_mock.account = {
        "equity": "100000.00", "last_equity": "100000.00",
        "status": "ACTIVE",
    }

    # ── reconcile must not raise; sync must run ──────────────────────────────
    result = rec.reconcile(
        os.environ["DATABASE_URL"], _ALPACA_ENDPOINT, alpaca_session,
    )
    assert result["fills_written"] == 0
    assert result["update_failures"] == 0, (
        f"CANCELED status should round-trip post-migration_006, got {result}"
    )
    assert result["positions_synced"] is True

    # Order in DB is now CANCELED (not still SUBMITTED, not crashed)
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM orders WHERE client_order_id = %s",
            (client_id,),
        )
        assert cur.fetchone()[0] == "CANCELED"

    # ── update_daily_pnl + morning_report still complete cleanly ─────────────
    _patch_alpaca_session(monkeypatch, alpaca_session)
    upd.update(os.environ["DATABASE_URL"])

    message, _ = morning_report.build_report()
    assert "QuantAI Morning Report" in message  # didn't crash


# ── 3. Day 1-16 simulation — empty positions, P&L from equity delta ───────────

def test_empty_positions_pnl_from_equity_delta(
    db_conn, alpaca_mock, alpaca_session, monkeypatch,
):
    """Replays the 2026-04-23 pre-incident state: positions table empty,
    no fills today, but Alpaca shows a small day-loss.

    The 2026-04-23 P0 bug would here have computed total_pnl = -(BUY notional)
    ≈ -$49,234.  The Alpaca-equity-delta fix must produce ~-$543 and never
    approach the bug signature.
    """
    # Seed history so the gate query has prior rows to anchor starting_value
    _seed_daily_pnl_history(db_conn, days_back=3)

    alpaca_mock.positions = []  # day 1-16: nothing reconciled yet
    alpaca_mock.account = {
        # The exact pre-fix-incident numbers from the post-mortem
        "equity": "96858.99", "last_equity": "97401.99",
        "status": "ACTIVE",
    }

    _patch_alpaca_session(monkeypatch, alpaca_session)
    upd.update(os.environ["DATABASE_URL"])

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT realized_pnl, unrealized_pnl, total_pnl, num_trades "
            "FROM daily_pnl WHERE trading_date = CURRENT_DATE"
        )
        real, unreal, total, n = cur.fetchone()

    # equity delta = -$543.00 → total_pnl = -$543
    assert total == Decimal("-543.00")
    # Generated column invariant
    assert total == real + unreal
    # No fills today
    assert n == 0
    # The bug signature: pre-fix this would have been ≈ -$49,234
    assert total > Decimal("-5000"), (
        f"total_pnl={total} smells like the 2026-04-23 BUY-notional bug"
    )

    # Morning report renders without crashing on the day-loss
    message, level = morning_report.build_report()
    assert "QuantAI Morning Report" in message
    assert "-$543" in message
    # MaxDD < 8% on a $543 loss → SUMMARY level
    assert level == "SUMMARY"


# ── 4. Sector concentration — all 10 in precious_metals → HIGH CONCENTRATION ──

def test_high_concentration_warning_fires(
    db_conn, alpaca_mock, alpaca_session, monkeypatch,
):
    """All 10 positions in precious_metals → morning report fires HIGH
    CONCENTRATION (>50% of book in a single sector)."""
    pm_book = [
        ("GLD",  11,  434.35),
        ("IAU",  50,   65.20),
        ("AEM",  14,   68.10),
        ("KGC", 153,   32.35),
        ("AGI",  80,   18.40),
        ("WPM",  35,   55.20),
        ("GOLD", 75,   18.10),
        ("NEM",  60,   42.30),
        ("CDE", 200,    5.90),
        ("SLV",  50,   29.40),
    ]
    seeded = _seed_submitted_orders(db_conn, [s for s, *_ in pm_book])
    _seed_daily_pnl_history(db_conn, days_back=3)

    for (client_id, broker_id, sym), (_, qty, price) in zip(seeded, pm_book):
        alpaca_mock.orders[broker_id] = _alpaca_filled_payload(
            broker_id, sym, qty, price,
        )
    alpaca_mock.positions = [
        _alpaca_position_payload(s, q, p, unrealized=8.0)
        for s, q, p in pm_book
    ]
    alpaca_mock.account = {
        "equity": "100080.00", "last_equity": "100000.00",
        "status": "ACTIVE",
    }

    rec.reconcile(os.environ["DATABASE_URL"], _ALPACA_ENDPOINT, alpaca_session)
    _patch_alpaca_session(monkeypatch, alpaca_session)
    upd.update(os.environ["DATABASE_URL"])

    message, _ = morning_report.build_report()
    assert "Sector Exposure" in message
    assert "precious_metals" in message
    assert "HIGH CONCENTRATION" in message, (
        "HIGH CONCENTRATION must fire when 100% of book is one sector"
    )
    assert "precious_metals 100%" in message or "precious_metals  100%" in message \
        or "precious_metals 100" in message  # tolerate format spacing


# ── 5. Partial cron failure — morning_report DB issue, cron must not block ────

def test_morning_report_degrades_gracefully(
    db_conn, alpaca_mock, alpaca_session, monkeypatch,
):
    """Reconcile succeeds, but morning_report's DB is unreachable.

    Each _query_* helper catches exceptions and returns defaults → build_report
    must still produce a report (degraded), and send_morning_report must
    return without raising — cron does not abort.
    """
    seeded = _seed_submitted_orders(db_conn, ["GLD"], qty=11, price=434.35)
    client_id, broker_id, _ = seeded[0]

    alpaca_mock.orders[broker_id] = _alpaca_filled_payload(
        broker_id, "GLD", 11, 434.35,
    )
    alpaca_mock.positions = [_alpaca_position_payload("GLD", 11, 434.35, 5.0)]
    alpaca_mock.account = {
        "equity": "100005.00", "last_equity": "100000.00",
        "status": "ACTIVE",
    }

    # reconcile completes cleanly
    result = rec.reconcile(
        os.environ["DATABASE_URL"], _ALPACA_ENDPOINT, alpaca_session,
    )
    assert result["fills_written"] == 1
    assert result["positions_synced"] is True

    # Now break morning_report's DB access — bogus DSN to unreachable port
    monkeypatch.setattr(
        morning_report,
        "_DB_URL",
        "postgres://nope:nope@127.0.0.1:1/nope?connect_timeout=1",
    )

    # build_report must NOT raise — every _query_* catches internally
    message, level = morning_report.build_report()
    assert "QuantAI Morning Report" in message
    # P&L falls back to zeros on DB failure
    assert "Today:      +$0.00" in message
    # send_morning_report must return (True/False), not raise
    ok = morning_report.send_morning_report()
    assert isinstance(ok, bool), (
        f"send_morning_report must not raise on DB failure, got {ok!r}"
    )
