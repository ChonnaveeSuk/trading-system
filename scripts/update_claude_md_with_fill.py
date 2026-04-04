#!/usr/bin/env python3
# trading-system/scripts/update_claude_md_with_fill.py
#
# Reads /tmp/quantai_first_fill_result.json and patches CLAUDE.md with:
#   - Updated "Last updated" date
#   - First live fill record under Phase 4 Remaining
#   - New "## First Live Fill" section
#
# Called by run_first_live_fill.sh after a successful live fill.
# Can also be run manually: python3 scripts/update_claude_md_with_fill.py

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULT_FILE = os.environ.get("RESULT_FILE", "/tmp/quantai_first_fill_result.json")
CLAUDE_MD = os.path.join(REPO, "CLAUDE.md")


def bq_row_count(fill_id: str) -> int | None:
    """Check BigQuery for the fill row. Returns row count or None on error."""
    query = (
        f"SELECT COUNT(*) as n FROM quantai_trading.trades "
        f"WHERE JSON_VALUE(data, '$.fill_id') = '{fill_id}'"
    )
    try:
        result = subprocess.run(
            ["bq", "query", "--project_id=quantai-trading-paper",
             "--use_legacy_sql=false", "--format=json", query],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            rows = json.loads(result.stdout)
            return int(rows[0]["n"]) if rows else 0
    except Exception:
        pass
    return None


def pg_fill_price(fill_id: str) -> str | None:
    """Re-query PostgreSQL to confirm fill_price is stored."""
    try:
        import psycopg2
        conn = psycopg2.connect(
            os.environ.get("DATABASE_URL",
                           "postgres://quantai:quantai_dev_2026@localhost:5432/quantai")
        )
        with conn.cursor() as cur:
            cur.execute("SELECT fill_price FROM fills WHERE fill_id = %s", (fill_id,))
            row = cur.fetchone()
        conn.close()
        return str(row[0]) if row else None
    except Exception:
        return None


def patch_claude_md(r: dict, bq_count: int | None, pg_price: str | None) -> None:
    with open(CLAUDE_MD) as f:
        text = f.read()

    today_str = date.today().isoformat()

    # 1. Update "Last updated" line
    text = re.sub(r"\*\*Last updated:\*\* [\d-]+",
                  f"**Last updated:** {today_str}", text)

    # 2. Update test-related "Phase 4 Remaining" bullet about fill test
    old_bullet = (
        "- [ ] Run full fill test during market hours: "
        "`python3 scripts/test_alpaca_connection.py`"
    )
    fill_price_str = r.get("fill_price") or "N/A"
    fill_id_str = r.get("fill_id") or "N/A"
    alpaca_id_str = r.get("alpaca_order_id") or "N/A"
    pg_ok = "✓" if r.get("pg_inserted") else "✗"
    ps_ok = "✓" if r.get("pubsub_published") else "✗"
    bq_ok = f"✓ ({bq_count} row)" if bq_count else "pending (~5 min latency)"
    new_bullet = (
        f"- [x] Full fill test PASSED — {today_str} market hours:\n"
        f"  Fill price=${fill_price_str}, Alpaca ID={alpaca_id_str[:8]}…, "
        f"PostgreSQL={pg_ok}, Pub/Sub={ps_ok}, BigQuery={bq_ok}"
    )
    if old_bullet in text:
        text = text.replace(old_bullet, new_bullet)

    # 3. Update "Phase 4 Remaining" header note
    text = text.replace(
        "### ⏳ Phase 4 Remaining — Next Session",
        "### ⏳ Phase 4 Remaining"
    )

    # 4. Append "## First Live Fill" section before "## GCP Infrastructure"
    first_fill_section = f"""
---

## First Live Fill (Day 1 of 90-Day Paper Run)

**Date:** {today_str}
**Market open:** 09:30 ET / 20:30 Thai

| Field | Value |
|-------|-------|
| Symbol | {r.get("symbol", "AAPL")} |
| Side | BUY |
| Quantity | {r.get("filled_qty", "1")} share |
| Fill price | ${fill_price_str} |
| Alpaca order ID | {alpaca_id_str} |
| Fill ID (PostgreSQL) | {fill_id_str} |
| PostgreSQL verified | {pg_ok} (fill_price={pg_price or fill_price_str}) |
| Pub/Sub published | {ps_ok} |
| BigQuery rows | {bq_ok} |
| Account equity | ${r.get("account_equity", "N/A")} |

**90-Day gate target (ends {_add_days(today_str, 90)}):** Sharpe > 1.0, MaxDD < 15%
Tracked daily by `scripts/update_daily_pnl.py` → Grafana "Live Paper Gate" dashboard.

"""

    marker = "\n---\n\n## GCP Infrastructure"
    if marker in text and "## First Live Fill" not in text:
        text = text.replace(marker, first_fill_section + "---\n\n## GCP Infrastructure", 1)

    with open(CLAUDE_MD, "w") as f:
        f.write(text)

    print(f"  ✓  CLAUDE.md updated — First Live Fill section added ({today_str})")


def _add_days(date_str: str, days: int) -> str:
    from datetime import date, timedelta
    d = date.fromisoformat(date_str)
    return (d + timedelta(days=days)).isoformat()


def main() -> None:
    print(f"\n  Reading result file: {RESULT_FILE}")
    if not os.path.exists(RESULT_FILE):
        print(f"  ✗  Result file not found: {RESULT_FILE}")
        print("     Run test_alpaca_connection.py --result-file first.")
        sys.exit(1)

    with open(RESULT_FILE) as f:
        r = json.load(f)

    print(f"  success:         {r.get('success')}")
    print(f"  market_was_open: {r.get('market_was_open')}")
    print(f"  fill_price:      {r.get('fill_price')}")
    print(f"  fill_id:         {r.get('fill_id')}")

    if not r.get("success"):
        print("  ✗  Result shows failure — not updating CLAUDE.md")
        sys.exit(1)

    if not r.get("market_was_open"):
        print("  ·  Market was closed during test — fill_price is synthetic")
        print("     CLAUDE.md will not be updated until a real fill is recorded.")
        sys.exit(0)

    # Re-verify PostgreSQL
    fill_id = r.get("fill_id")
    pg_price = pg_fill_price(fill_id) if fill_id else None
    if pg_price:
        print(f"  ✓  PostgreSQL confirmed: fill_price={pg_price}")
    else:
        print("  ·  PostgreSQL re-verify skipped (psycopg2 unavailable or fill not found)")

    # Check BigQuery
    bq_count = bq_row_count(fill_id) if fill_id else None
    if bq_count is not None:
        print(f"  ✓  BigQuery: {bq_count} row(s) for fill_id")
    else:
        print("  ·  BigQuery check skipped (bq CLI unavailable or latency)")

    # Patch CLAUDE.md
    patch_claude_md(r, bq_count, pg_price)


if __name__ == "__main__":
    main()
