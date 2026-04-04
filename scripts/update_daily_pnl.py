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
import sys
from datetime import date, timezone
from decimal import Decimal

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

                # ── Today's realized P&L from fills ──────────────────────────
                cur.execute("""
                    SELECT COALESCE(SUM(
                        CASE WHEN side = 'SELL'
                             THEN (filled_quantity * fill_price - commission)
                             ELSE -(filled_quantity * fill_price + commission)
                        END
                    ), 0)
                    FROM fills
                    WHERE timestamp::date = %s
                """, (today,))
                realized_pnl = Decimal(str(cur.fetchone()[0]))

                # ── Unrealized P&L from open positions ────────────────────────
                cur.execute("SELECT COALESCE(SUM(unrealized_pnl), 0) FROM positions")
                unrealized_pnl = Decimal(str(cur.fetchone()[0]))

                # ── Trade count (fills today) ─────────────────────────────────
                cur.execute(
                    "SELECT COUNT(*) FROM fills WHERE timestamp::date = %s", (today,)
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
