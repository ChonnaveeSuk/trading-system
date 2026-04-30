# trading-system/scripts/tests/test_obsidian_sync.py
#
# Unit coverage for the morning-report → Obsidian vault daily-note sync.
#
# These tests don't touch Postgres — they construct a ReportData snapshot
# directly and assert against the written file.  They do depend on the
# autouse fixtures in conftest.py (DATABASE_URL pin + send_alert no-op)
# because conftest reloads `morning_report` to refresh module-level
# globals; that reload is harmless here.

from __future__ import annotations

import importlib
from datetime import date

import pytest

import morning_report


def _make_data(today: date = date(2026, 5, 4)) -> "morning_report.ReportData":
    return morning_report.ReportData(
        message=(
            "QuantAI Morning Report — 2026-05-04\n\n"
            "🌍 Market Regime: 🟢 BULL\n"
            "📈 Signals (May 03)\n"
            "BUY:  3 | SELL: 0 | HOLD: 13\n\n"
            "💰 P&L Summary\n"
            "Today:      +$457.00"
        ),
        level="SUMMARY",
        today=today,
        regime="BULL",
        sharpe=1.39,
        trades=16,
        pnl_today=457.0,
    )


def test_save_to_obsidian_writes_frontmatter_and_body(tmp_path, monkeypatch):
    """Happy path: vault dir exists, no prior note → write the daily note.

    Asserts every frontmatter field the QuantAI MOC's Dataview queries
    aggregate over (date, regime, sharpe, trades, pnl_today, tags) plus
    the prev-day backlink and the embedded report body.
    """
    monkeypatch.setenv("OBSIDIAN_DAILY_DIR", str(tmp_path))

    data = _make_data()
    ok = morning_report.save_to_obsidian(data)

    assert ok is True
    note = tmp_path / "2026-05-04.md"
    assert note.exists(), "daily note should have been created"
    content = note.read_text(encoding="utf-8")

    # Frontmatter
    assert content.startswith("---\n")
    assert "date: 2026-05-04" in content
    assert "regime: BULL" in content
    assert "sharpe: 1.39" in content
    assert "trades: 16" in content
    assert 'pnl_today: "+$457.00"' in content
    assert "tags: [quantai, trading, daily]" in content

    # Prev-day backlink (yesterday calendar)
    assert "[[2026-05-03]]" in content

    # Full report body embedded verbatim inside a fenced text block
    assert "```text\n" in content
    assert "QuantAI Morning Report — 2026-05-04" in content
    assert "BUY:  3 | SELL: 0 | HOLD: 13" in content


def test_save_to_obsidian_skips_when_vault_missing(tmp_path, monkeypatch, caplog):
    """Cloud Run path: WSL mount doesn't exist → skip cleanly, no raise."""
    missing = tmp_path / "definitely-not-there"
    monkeypatch.setenv("OBSIDIAN_DAILY_DIR", str(missing))

    with caplog.at_level("INFO", logger="morning_report"):
        ok = morning_report.save_to_obsidian(_make_data())

    assert ok is False
    assert not missing.exists(), "must not create the vault directory"
    assert any(
        "Obsidian vault not present" in rec.getMessage()
        for rec in caplog.records
    ), "expected an INFO log explaining the skip"


def test_save_to_obsidian_preserves_existing_note(tmp_path, monkeypatch):
    """Pre-filled manual note must not be overwritten by the cron."""
    monkeypatch.setenv("OBSIDIAN_DAILY_DIR", str(tmp_path))
    note = tmp_path / "2026-05-04.md"
    original = "# Pre-filled by hand\n\nDo not clobber.\n"
    note.write_text(original, encoding="utf-8")

    ok = morning_report.save_to_obsidian(_make_data())

    assert ok is False
    assert note.read_text(encoding="utf-8") == original


def test_save_to_obsidian_handles_unknown_regime_and_no_sharpe(tmp_path, monkeypatch):
    """Day 1: regime not yet recorded + <2 P&L rows → still writes a note."""
    monkeypatch.setenv("OBSIDIAN_DAILY_DIR", str(tmp_path))
    data = morning_report.ReportData(
        message="QuantAI Morning Report — 2026-04-07\n\nNo data yet.",
        level="SUMMARY",
        today=date(2026, 4, 7),
        regime="",        # nothing in system_metrics yet
        sharpe=None,      # gate has <2 daily returns
        trades=0,
        pnl_today=0.0,
    )

    ok = morning_report.save_to_obsidian(data)
    assert ok is True
    content = (tmp_path / "2026-04-07.md").read_text(encoding="utf-8")

    assert "regime: UNKNOWN" in content   # explicit fallback, no blank value
    assert "sharpe: null" in content       # YAML null, parses cleanly
    assert "trades: 0" in content
    assert 'pnl_today: "+$0.00"' in content


def test_send_morning_report_calls_save_when_build_succeeds(monkeypatch, tmp_path):
    """send_morning_report must invoke save_to_obsidian after Telegram send.

    Stubs out _build_report_data + send_alert so the test doesn't need a
    DB or a network — proves the entry point wires Obsidian sync in.
    """
    monkeypatch.setenv("OBSIDIAN_DAILY_DIR", str(tmp_path))

    # Re-import so the module picks up the env (defensive — conftest also
    # reloads, but we set the env after that).
    importlib.reload(morning_report)
    monkeypatch.setattr(morning_report, "send_alert", lambda *a, **kw: True)

    data = _make_data()
    monkeypatch.setattr(morning_report, "_build_report_data", lambda: data)

    ok = morning_report.send_morning_report()
    assert ok is True
    assert (tmp_path / "2026-05-04.md").exists(), (
        "send_morning_report must trigger save_to_obsidian"
    )


def test_save_to_obsidian_default_dir_used_when_arg_and_env_absent(
    monkeypatch, tmp_path,
):
    """When neither arg nor env is provided, the module constant is used.

    Monkeypatch the constant rather than relying on the real WSL path so
    the test never pollutes the actual Obsidian vault on the dev box.
    """
    monkeypatch.delenv("OBSIDIAN_DAILY_DIR", raising=False)
    monkeypatch.setattr(
        morning_report, "_DEFAULT_OBSIDIAN_DAILY_DIR", str(tmp_path),
    )
    ok = morning_report.save_to_obsidian(_make_data(date(2026, 5, 4)))
    assert ok is True
    assert (tmp_path / "2026-05-04.md").exists()
