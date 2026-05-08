#!/usr/bin/env python3
# trading-system/scripts/morning_report.py
#
# Sends a comprehensive morning report via Telegram after the daily cron job.
# Called as the final step of run_daily.sh (replaces --daily-summary).
#
# Report sections:
#   🌍 Market Regime  — latest SPY/MA200 from system_metrics
#   📈 Signals         — yesterday's BUY/SELL/HOLD counts from signals JSON
#   💰 P&L Summary    — today, week, cumulative, open positions
#   🎯 90-Day Gate    — Sharpe, MaxDD, trade count, days elapsed
#   ⏭ Next run       — next scheduled Cloud Run execution
#
# Usage:
#   python3 scripts/morning_report.py
#
# Always non-fatal: DB failures return empty data; Telegram failures log a warning.

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS_DIR)
from telegram_alert import send_alert
from _db import database_url as _database_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("morning_report")
_SIGNALS_FILE = "/tmp/quantai_signals_today.json"
_PAPER_START = date(2026, 4, 29)
_DEFAULT_OBSIDIAN_DAILY_DIR = (
    "/mnt/c/Users/Chonn/Obsidian/MyBrain/10 Projects/QuantAI/Daily"
)

_REGIME_EMOJI = {"BULL": "\U0001f7e2", "NEUTRAL": "\U0001f7e1", "BEAR": "\U0001f534"}
# 😰 CALM, ⚠️ CAUTION, 🚨 PANIC
_VIX_EMOJI = {"CALM": "\U0001f630", "CAUTION": "⚠️", "PANIC": "\U0001f6a8"}
# 📅 next event,  ⛔ blackout today
_CAL_NEXT_EMOJI = "\U0001f4c5"
_CAL_BLOCK_EMOJI = "⛔"


# ── DB helpers ────────────────────────────────────────────────────────────────


# ── Sector mapping ────────────────────────────────────────────────────────────
# Single source of truth lives in strategy/src/signals/momentum.py so the live
# sector gate (alpaca_direct.py) and this morning report can never drift apart.
_STRATEGY_DIR = os.path.join(os.path.dirname(_SCRIPTS_DIR), "strategy")
sys.path.insert(0, _STRATEGY_DIR)
from src.signals.momentum import SYMBOL_TO_SECTOR, sector_for as _sector_for  # noqa: E402

# Re-export for backwards compatibility with anything that imported from here.
SECTOR_MAP = SYMBOL_TO_SECTOR


def _connect():
    import psycopg2
    return psycopg2.connect(_database_url())


def _compute_regime_from_ohlcv() -> dict:
    """Compute regime fresh from the latest 250 SPY bars in `ohlcv`.

    Bypasses the date-window filter on PostgresOhlcvFetcher.fetch — that path
    returned only ~32 bars on Cloud SQL when the seeded history was older than
    the lookback window.  Pulling the latest 250 rows by row order gives a
    stable MA200 input regardless of seed freshness.
    """
    import pandas as pd
    _STRATEGY_DIR_LOCAL = os.path.join(os.path.dirname(_SCRIPTS_DIR), "strategy")
    if _STRATEGY_DIR_LOCAL not in sys.path:
        sys.path.insert(0, _STRATEGY_DIR_LOCAL)
    from src.signals.momentum import MomentumStrategy, MomentumConfig  # noqa: E402

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT timestamp, close
                FROM ohlcv
                WHERE symbol = 'SPY'
                ORDER BY timestamp DESC
                LIMIT 250
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        return {}
    df = pd.DataFrame(rows, columns=["timestamp", "close"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["close"] = df["close"].astype(float)
    df = df.set_index("timestamp").sort_index()
    strat = MomentumStrategy(MomentumConfig())
    regime = strat.update_regime(df)
    spy_price = float(strat._spy_price or 0.0)
    spy_ma200 = float(strat._spy_ma200 or 0.0)
    delta_pct = ((spy_price - spy_ma200) / spy_ma200 * 100.0) if spy_ma200 > 0 else 0.0
    return {
        "regime": regime,
        "spy_price": spy_price,
        "spy_ma200": spy_ma200,
        "delta_pct": delta_pct,
    }


def _query_regime() -> dict:
    """Return the current regime + SPY/MA200/delta dict.

    Tries `system_metrics.market_regime` first (single-source-of-truth shared
    with Grafana), then falls back to a fresh computation from `ohlcv` so the
    morning report is never stuck on UNKNOWN if the daily strategy run hasn't
    written the row yet.
    """
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT labels, recorded_at
                    FROM system_metrics
                    WHERE metric_name = 'market_regime'
                    ORDER BY recorded_at DESC
                    LIMIT 1
                """)
                row = cur.fetchone()
                if row:
                    labels, recorded_at = row
                    if not isinstance(labels, dict):
                        labels = json.loads(labels or "{}")
                    cached = {
                        "regime": labels.get("regime", ""),
                        "spy_price": float(labels.get("spy_price", 0.0)),
                        "spy_ma200": float(labels.get("spy_ma200", 0.0)),
                        "delta_pct": float(labels.get("delta_pct", 0.0)),
                    }
                    if cached["regime"] and cached["spy_price"] > 0 and cached["spy_ma200"] > 0:
                        return cached
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Regime query failed: %s", e)

    try:
        fresh = _compute_regime_from_ohlcv()
        if fresh:
            return fresh
    except Exception as e:
        logger.warning("Regime fallback (ohlcv) failed: %s", e)
    return {}


def _build_calendar_section(today: date) -> Optional[str]:
    """Compose the morning-report calendar block.

    Shows:
      - ⛔ today's blackout reason (if any)
      - 📅 next upcoming event with day-distance
    Returns None when both are absent (calendar import or call failed).
    """
    try:
        from src.filters.economic_calendar import EconomicCalendar
    except Exception as e:
        logger.debug("EconomicCalendar import failed (non-fatal): %s", e)
        return None
    cal = EconomicCalendar()
    lines: list[str] = []
    blackout = cal.blackout_reason(today)
    if blackout:
        lines.append(f"{_CAL_BLOCK_EMOJI} BLACKOUT: {blackout} — BUY blocked today")
    nxt = cal.get_next_event(today)
    if nxt:
        ev, days_away = nxt
        if days_away == 0:
            when = "today"
        elif days_away == 1:
            when = "tomorrow"
        else:
            when = f"{days_away} days away"
        lines.append(
            f"{_CAL_NEXT_EMOJI} Next event: {ev.kind.value} "
            f"({ev.event_date.strftime('%b %-d')}) — {when}"
        )
    return "\n".join(lines) if lines else None


def _build_earnings_section(today: date, lookahead_days: int = 7) -> Optional[str]:
    """Compose the per-symbol earnings block.

    Shows:
      ⛔ symbols with earnings TODAY or TOMORROW (BUY blocked)
      📅 other earnings within the lookahead window for situational awareness

    Returns None if the calendar is unavailable or there are no upcoming
    earnings in the window.  Only the 9 stocks in the production universe
    are tracked — ETFs/crypto never appear here.
    """
    try:
        from src.filters.economic_calendar import EarningsCalendar
    except Exception as e:
        logger.debug("EarningsCalendar import failed (non-fatal): %s", e)
        return None

    ec = EarningsCalendar()
    end = today + timedelta(days=lookahead_days)
    upcoming = ec.events_in_window(today, end)
    if not upcoming:
        return None

    blocked: list[str] = []
    weekly: list[str] = []
    for ev in upcoming:
        days_away = (ev.event_date - today).days
        when = (
            "today"     if days_away == 0 else
            "tomorrow"  if days_away == 1 else
            ev.event_date.strftime("%b %-d")
        )
        if days_away <= ec.blackout_days_before:
            blocked.append(f"  ⛔ {ev.symbol}: earnings {when} — BUY blocked")
        else:
            weekly.append(f"  • {ev.symbol} ({when}) — {ev.description}")

    lines = ["📅 Earnings Watch"]
    if blocked:
        lines.extend(blocked)
    if weekly:
        lines.append("  This week:")
        lines.extend(weekly)
    return "\n".join(lines)


def _query_vix() -> dict:
    """Return latest VIX state row from system_metrics, or {} if unavailable."""
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT labels, recorded_at
                    FROM system_metrics
                    WHERE metric_name = 'vix_state'
                    ORDER BY recorded_at DESC
                    LIMIT 1
                """)
                row = cur.fetchone()
                if row:
                    labels, _ = row
                    if not isinstance(labels, dict):
                        labels = json.loads(labels or "{}")
                    return {
                        "vix_state": labels.get("vix_state", ""),
                        "vix_level": float(labels.get("vix_level", 0.0)),
                        "vix_price": float(labels.get("vix_price", 0.0)),
                    }
        finally:
            conn.close()
    except Exception as e:
        logger.warning("VIX query failed: %s", e)
    return {}


def _query_pnl() -> dict:
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                today = date.today()

                cur.execute(
                    "SELECT COALESCE(total_pnl, 0), ending_value FROM daily_pnl WHERE trading_date = %s",
                    (today,),
                )
                row = cur.fetchone()
                today_pnl = float(row[0]) if row else 0.0
                ending_value = float(row[1]) if (row and row[1] is not None) else None

                if ending_value is not None:
                    cumulative_pnl = ending_value - 100_000.0
                else:
                    cur.execute("SELECT COALESCE(SUM(realized_pnl), 0) FROM daily_pnl")
                    r = cur.fetchone()
                    cumulative_pnl = float(r[0]) if r else 0.0

                # Week P&L: last 7 calendar days covers Mon–Fri window
                cur.execute(
                    "SELECT COALESCE(SUM(total_pnl), 0) FROM daily_pnl "
                    "WHERE trading_date > %s AND trading_date <= %s",
                    (today - timedelta(days=7), today),
                )
                r = cur.fetchone()
                week_pnl = float(r[0]) if r else 0.0

                cur.execute("SELECT COUNT(*) FROM positions WHERE quantity != 0")
                r = cur.fetchone()
                open_positions = int(r[0]) if r else 0

                return {
                    "today_pnl": today_pnl,
                    "week_pnl": week_pnl,
                    "cumulative_pnl": cumulative_pnl,
                    "open_positions": open_positions,
                }
        finally:
            conn.close()
    except Exception as e:
        logger.warning("P&L query failed: %s", e)
    return {"today_pnl": 0.0, "week_pnl": 0.0, "cumulative_pnl": 0.0, "open_positions": 0}


def _query_gate() -> dict:
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                today = date.today()
                days_elapsed = max(1, (today - _PAPER_START).days + 1)

                cur.execute(
                    "SELECT trading_date, starting_value, ending_value, num_trades "
                    "FROM daily_pnl WHERE trading_date >= %s ORDER BY trading_date",
                    (_PAPER_START,),
                )
                rows = cur.fetchall()

                total_trades = sum(int(r[3] or 0) for r in rows)

                # Max drawdown (peak-to-trough on ending_value)
                max_dd_pct = 0.0
                peak: Optional[float] = None
                daily_returns: list[float] = []
                prev_val: Optional[float] = None

                for r in rows:
                    sv = float(r[1])
                    ev = float(r[2]) if r[2] is not None else None

                    if prev_val is None:
                        prev_val = sv

                    if ev is not None:
                        if peak is None or ev > peak:
                            peak = ev
                        if peak and peak > 0:
                            dd = (peak - ev) / peak * 100.0
                            max_dd_pct = max(max_dd_pct, dd)
                        if prev_val > 0:
                            daily_returns.append((ev - prev_val) / prev_val)
                        prev_val = ev

                # Annualised Sharpe from daily returns
                sharpe: Optional[float] = None
                if len(daily_returns) >= 2:
                    import statistics
                    mean_r = statistics.mean(daily_returns)
                    std_r = statistics.stdev(daily_returns)
                    if std_r > 0:
                        sharpe = (mean_r / std_r) * (252 ** 0.5)

                # Consecutive days without a new trade (from most recent)
                no_trade_days = 0
                for r in reversed(rows):
                    if int(r[3] or 0) == 0:
                        no_trade_days += 1
                    else:
                        break

                return {
                    "days_elapsed": days_elapsed,
                    "total_trades": total_trades,
                    "max_dd_pct": max_dd_pct,
                    "sharpe": sharpe,
                    "no_trade_days": no_trade_days,
                }
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Gate query failed: %s", e)
    return {"days_elapsed": 1, "total_trades": 0, "max_dd_pct": 0.0, "sharpe": None}


def _query_ab_attribution(yesterday: date) -> dict:
    """Return per-signal-type fill counts + position stats.

    Returns:
        {'momentum':   {'fills', 'positions', 'unrealized'},
         'trend_ride': {'fills', 'positions', 'unrealized'}}

    Always non-fatal: returns zero-filled dict on DB error.
    """
    result = {
        "momentum":   {"fills": 0, "positions": 0, "unrealized": 0.0},
        "trend_ride": {"fills": 0, "positions": 0, "unrealized": 0.0},
    }
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                # Yesterday's fills, joined with orders for signal_type
                ystart = datetime.combine(yesterday, datetime.min.time(), tzinfo=timezone.utc)
                yend = ystart + timedelta(days=1)
                cur.execute(
                    "SELECT o.signal_type, COUNT(*) "
                    "FROM fills f JOIN orders o USING (client_order_id) "
                    "WHERE f.timestamp >= %s AND f.timestamp < %s "
                    "GROUP BY o.signal_type",
                    (ystart, yend),
                )
                for st, cnt in cur.fetchall():
                    if st in result:
                        result[st]["fills"] = int(cnt)

                # Active positions (qty != 0)
                cur.execute(
                    "SELECT signal_type, COUNT(*), COALESCE(SUM(unrealized_pnl), 0) "
                    "FROM positions WHERE quantity != 0 "
                    "GROUP BY signal_type"
                )
                for st, cnt, unreal in cur.fetchall():
                    if st in result:
                        result[st]["positions"] = int(cnt)
                        result[st]["unrealized"] = float(unreal)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("A/B attribution query failed: %s", e)
    return result


def _query_stop_loss_risk(stop_pct: float, warn_pct: float) -> list[dict]:
    """Return positions with unrealized_pnl_pct ≤ -warn_pct, sorted worst-first.

    Each entry: {symbol, qty, avg_cost, unrealized_pnl, unrealized_pct, breached}
    where:
      - unrealized_pct = unrealized_pnl / (|qty| * avg_cost)
      - breached = True if unrealized_pct ≤ -stop_pct (i.e. should already
        have been stopped on the next live run).

    Source is the local positions table — same view used by sector
    concentration. Always non-fatal: returns [] on DB error.
    """
    rows: list[dict] = []
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT symbol, quantity, average_cost, "
                    "COALESCE(unrealized_pnl, 0) "
                    "FROM positions WHERE quantity != 0"
                )
                raw = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Stop-loss risk query failed: %s", e)
        return rows

    for sym, qty, avg_cost, unreal in raw:
        try:
            qty_f = float(qty or 0)
            avg_f = float(avg_cost or 0)
            unreal_f = float(unreal or 0)
        except (TypeError, ValueError):
            continue
        cost_basis = abs(qty_f) * avg_f
        if cost_basis <= 0:
            continue
        pct = unreal_f / cost_basis
        if pct > -abs(warn_pct):
            continue
        rows.append({
            "symbol": sym,
            "qty": qty_f,
            "avg_cost": avg_f,
            "unrealized_pnl": unreal_f,
            "unrealized_pct": pct,
            "breached": pct <= -abs(stop_pct),
        })
    rows.sort(key=lambda r: r["unrealized_pct"])
    return rows


def _query_sector_concentration() -> dict:
    """Return sector-level exposure from current open positions.

    Returns:
        {
          "by_sector": {sector: {"count", "notional", "unrealized"}, ...},
          "total_notional": float,
          "largest_sector": (name, pct) | None,
        }
    Notional = |quantity * average_cost|. Always non-fatal on DB error.
    """
    result: dict = {"by_sector": {}, "total_notional": 0.0, "largest_sector": None}
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT symbol, quantity, average_cost, "
                    "COALESCE(unrealized_pnl, 0) "
                    "FROM positions WHERE quantity != 0"
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Sector concentration query failed: %s", e)
        return result

    total = 0.0
    for sym, qty, avg_cost, unreal in rows:
        qty = float(qty or 0)
        avg_cost = float(avg_cost or 0)
        unreal = float(unreal or 0)
        notional = abs(qty * avg_cost)
        sector = _sector_for(sym)
        entry = result["by_sector"].setdefault(
            sector, {"count": 0, "notional": 0.0, "unrealized": 0.0}
        )
        entry["count"] += 1
        entry["notional"] += notional
        entry["unrealized"] += unreal
        total += notional

    result["total_notional"] = total
    if total > 0 and result["by_sector"]:
        largest = max(result["by_sector"].items(), key=lambda kv: kv[1]["notional"])
        result["largest_sector"] = (largest[0], largest[1]["notional"] / total * 100)
    return result


def _load_signals() -> dict:
    try:
        with open(_SIGNALS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_pnl(v: float) -> str:
    return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"


def _next_run_label() -> str:
    """Next Mon–Fri 22:00 UTC run, with Thailand offset."""
    now = datetime.now(timezone.utc)
    candidate = now.replace(hour=22, minute=0, second=0, microsecond=0)
    if now >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    day = candidate.strftime("%a %Y-%m-%d")
    th_hour = (22 + 7) % 24
    return f"{day} 22:00 UTC ({th_hour:02d}:00 TH)"


# ── Report builder ────────────────────────────────────────────────────────────

@dataclass
class ReportData:
    """Structured snapshot of the morning report.

    Carries both the rendered Telegram message + alert level (consumed by
    `send_alert`) and the headline metrics needed to write a daily note to
    the Obsidian vault as YAML frontmatter (regime, sharpe, trades,
    pnl_today).  `today` is the trading-day date the report covers.
    """
    message: str
    level: str
    today: date
    regime: str
    sharpe: Optional[float]
    trades: int
    pnl_today: float


def build_report() -> tuple[str, str]:
    """Return (message_body, alert_level).

    Thin wrapper over `_build_report_data()` kept for backwards compatibility
    with existing tests that unpack a 2-tuple.
    """
    data = _build_report_data()
    return data.message, data.level


def _build_report_data() -> ReportData:
    """Render the report and return the full structured snapshot."""
    today = date.today()

    # "yesterday" label — skip weekends
    yesterday = today - timedelta(days=1)
    while yesterday.weekday() >= 5:
        yesterday -= timedelta(days=1)
    yesterday_label = yesterday.strftime("%b %d")

    regime_data = _query_regime()
    vix_data = _query_vix()
    pnl_data = _query_pnl()
    gate_data = _query_gate()
    signals_data = _load_signals()

    # ── Regime ────────────────────────────────────────────────────────────────
    regime = regime_data.get("regime", "")
    spy_price = regime_data.get("spy_price", 0.0)
    spy_ma200 = regime_data.get("spy_ma200", 0.0)
    delta_pct = regime_data.get("delta_pct", 0.0)

    r_emoji = _REGIME_EMOJI.get(regime, "\u26aa")
    if regime and spy_price and spy_ma200:
        regime_section = (
            f"\U0001f30d Market Regime: {r_emoji} {regime}\n"
            f"SPY: ${spy_price:.2f} | MA200: ${spy_ma200:.2f} | {delta_pct:+.1f}%"
        )
    elif regime:
        regime_section = f"\U0001f30d Market Regime: {r_emoji} {regime}"
    else:
        regime_section = "\U0001f30d Market Regime: \u26aa UNKNOWN (no data yet)"

    # \u2500\u2500 VIX (volatility) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    vix_state = vix_data.get("vix_state", "")
    vix_level = vix_data.get("vix_level", 0.0)
    vix_price = vix_data.get("vix_price", 0.0)
    if vix_state and vix_level > 0:
        v_emoji = _VIX_EMOJI.get(vix_state, "\u26aa")
        vix_section = f"{v_emoji} VIX: {vix_level:.1f} ({vix_state})"
    elif vix_state:
        # State recorded but level==0 \u2192 VIXY OHLCV missing/stale on Cloud Run
        # (e.g. seed_alpaca skipped VIXY because the IEX feed didn't return bars).
        # Show "N/A" rather than "0.0" so the dashboard isn't misleading.
        vix_section = "\u26a0\ufe0f VIX: N/A (data unavailable)"
    else:
        vix_section = None

    # \u2500\u2500 Economic calendar \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    cal_section = _build_calendar_section(today)
    earnings_section = _build_earnings_section(today)

    # ── Signals ───────────────────────────────────────────────────────────────
    buy_count = int(signals_data.get("buy", 0))
    sell_count = int(signals_data.get("sell", 0))
    hold_count = int(signals_data.get("hold", 0))
    total_signals = buy_count + sell_count + hold_count

    if total_signals == 0:
        signals_section = (
            f"\U0001f4c8 Signals ({yesterday_label})\n"
            "No signal data (strategy may not have run live)"
        )
    else:
        sig_line = f"BUY:  {buy_count} | SELL: {sell_count} | HOLD: {hold_count}"
        orders_submitted = int(signals_data.get("orders_submitted", 0))
        if orders_submitted > 0:
            sig_line += f"\nOrders submitted: {orders_submitted}"
        if buy_count == 0 and (sell_count > 0 or hold_count > 0):
            if regime == "BEAR":
                blocked = "BUY blocked by: Regime (BEAR — all BUY suppressed)"
            elif regime == "NEUTRAL":
                blocked = "BUY blocked by: Regime (NEUTRAL — reduced scores)"
            else:
                blocked = "Waiting: MA crossover or RSI pullback to entry zone"
            sig_line += f"\n{blocked}"
        signals_section = f"\U0001f4c8 Signals ({yesterday_label})\n{sig_line}"

    # ── A/B Attribution (skip if no activity) ────────────────────────────────
    ab_data = _query_ab_attribution(yesterday)
    mom = ab_data["momentum"]
    tr  = ab_data["trend_ride"]
    ab_total = mom["fills"] + tr["fills"] + mom["positions"] + tr["positions"]
    if ab_total > 0:
        ab_section = (
            f"\U0001f9ea A/B Attribution\n"
            f"Yesterday fills: Mom {mom['fills']} | TR {tr['fills']}\n"
            f"Active pos:      Mom {mom['positions']} | TR {tr['positions']}\n"
            f"Unrealized:      Mom {_fmt_pnl(mom['unrealized'])} | TR {_fmt_pnl(tr['unrealized'])}"
        )
    else:
        ab_section = None
    # ── Stop-loss risk (positions at or near hard stop) ───────────────────────
    # Mirrors the hard-stop trigger in alpaca_direct.check_and_trigger_stops.
    from src.signals.momentum import MomentumConfig
    _slcfg = MomentumConfig()
    stop_pct = _slcfg.stop_loss_pct
    warn_pct = _slcfg.stop_loss_warn_pct
    stop_rows = _query_stop_loss_risk(stop_pct, warn_pct)
    if stop_rows:
        lines = ["\U0001f6d1 Stop Loss Watch"]
        for r in stop_rows:
            pct = r["unrealized_pct"] * 100
            if r["breached"]:
                icon = "\U0001f6d1"  # 🛑 — should be stopped on next live run
                tag = f"BREACHED stop -{stop_pct*100:.1f}%"
            else:
                icon = "⚠"  # ⚠ — approaching stop
                tag = f"approaching stop -{stop_pct*100:.1f}%"
            lines.append(
                f"  {icon} {r['symbol']:<6} {pct:+6.2f}%  "
                f"{_fmt_pnl(r['unrealized_pnl'])} — {tag}"
            )
        stop_section = "\n".join(lines)
    else:
        stop_section = None

    # ── Sector Concentration (skip if no positions) ───────────────────────────
    # Mirrors the live-trading sector gate in alpaca_direct.py.
    from src.bridge.alpaca_direct import _MAX_SECTOR_POSITIONS, _MAX_SECTOR_PCT
    sector_count_cap = _MAX_SECTOR_POSITIONS
    sector_pct_cap = float(_MAX_SECTOR_PCT) * 100  # e.g. 30.0
    sector_data = _query_sector_concentration()
    if sector_data["by_sector"]:
        lines = ["\U0001f3af Sector Exposure"]
        by_s = sector_data["by_sector"]
        total = sector_data["total_notional"] or 1.0
        warnings_lines: list[str] = []
        for sec, info in sorted(by_s.items(), key=lambda kv: -kv[1]["notional"]):
            pct = info["notional"] / total * 100
            lines.append(
                f"  {sec:<20} {info['count']:>2}pos  "
                f"{pct:>5.1f}%  {_fmt_pnl(info['unrealized'])}"
            )
            if info["count"] >= sector_count_cap:
                warnings_lines.append(
                    f"  \U0001f6a8 {sec} at {info['count']}/{sector_count_cap} "
                    f"position cap \u2014 new BUYs blocked"
                )
            if pct > sector_pct_cap:
                warnings_lines.append(
                    f"  \u26a0 {sec} {pct:.0f}% of book > "
                    f"{sector_pct_cap:.0f}% sector cap"
                )
        if sector_data["largest_sector"]:
            name, pct = sector_data["largest_sector"]
            if pct > 50:
                warnings_lines.append(
                    f"  \u26a0 HIGH CONCENTRATION: {name} {pct:.0f}% of book"
                )
        lines.extend(warnings_lines)
        sector_section = "\n".join(lines)
    else:
        sector_section = None


    # ── P&L ──────────────────────────────────────────────────────────────────
    today_pnl = pnl_data.get("today_pnl", 0.0)
    week_pnl = pnl_data.get("week_pnl", 0.0)
    cumulative_pnl = pnl_data.get("cumulative_pnl", 0.0)
    open_positions = pnl_data.get("open_positions", 0)

    pnl_section = (
        f"\U0001f4b0 P&L Summary\n"
        f"Today:      {_fmt_pnl(today_pnl)}\n"
        f"Week:       {_fmt_pnl(week_pnl)}\n"
        f"Cumulative: {_fmt_pnl(cumulative_pnl)}\n"
        f"Open positions: {open_positions}"
    )

    # ── Gate ─────────────────────────────────────────────────────────────────
    days_elapsed = gate_data.get("days_elapsed", 1)
    total_trades = gate_data.get("total_trades", 0)
    max_dd_pct = gate_data.get("max_dd_pct", 0.0)
    sharpe = gate_data.get("sharpe")

    if sharpe is None:
        sharpe_str = "N/A (no trades yet)" if total_trades == 0 else "N/A (insufficient data)"
        sharpe_badge = ""
    elif days_elapsed < 30:
        sharpe_str = f"{sharpe:.2f} (early — <30 days)"
        sharpe_badge = " \u2705" if sharpe >= 1.0 else ""
    else:
        sharpe_str = f"{sharpe:.2f}"
        sharpe_badge = " \u2705" if sharpe >= 1.0 else (" \U0001f6a8" if sharpe < 0 else "")

    dd_badge = (
        " \u2705" if max_dd_pct < 8.0
        else (" \u26a0\ufe0f" if max_dd_pct < 15.0 else " \U0001f6a8")
    )

    no_trade_days = gate_data.get("no_trade_days", 0)
    no_trade_str = f" ({no_trade_days}d no new trades)" if no_trade_days >= 3 else ""
    gate_section = (
        f"\U0001f3af 90-Day Gate Progress (Day {days_elapsed}/90)\n"
        f"Sharpe:  {sharpe_str}{sharpe_badge}\n"
        f"MaxDD:   {max_dd_pct:.2f}%{dd_badge} (gate: <15%)\n"
        f"Trades:  {total_trades}{no_trade_str}"
    )

    # ── Next run ──────────────────────────────────────────────────────────────
    next_section = f"\u23ed Next run: {_next_run_label()}"

    # ── Alert level ───────────────────────────────────────────────────────────
    if max_dd_pct > 15.0:
        level = "CRITICAL"
    elif max_dd_pct > 8.0:
        level = "WARNING"
    else:
        level = "SUMMARY"

    sections = [
        f"QuantAI Morning Report \u2014 {today.isoformat()}",
        regime_section,
    ]
    if vix_section:
        sections.append(vix_section)
    if cal_section:
        sections.append(cal_section)
    if earnings_section:
        sections.append(earnings_section)
    sections.append(signals_section)
    if ab_section:
        sections.append(ab_section)
    if stop_section:
        sections.append(stop_section)
    if sector_section:
        sections.append(sector_section)
    sections.extend([pnl_section, gate_section, next_section])
    message = "\n\n".join(sections)
    if len(message) > 3800:  # leave room for safety margin
        if regime and spy_price and spy_ma200:
            sections[1] = (
                f"\U0001f30d {r_emoji} {regime} | "
                f"SPY ${spy_price:.0f} | MA200 ${spy_ma200:.0f} | "
                f"{delta_pct:+.1f}%"
            )
        message = "\n\n".join(sections)

    return ReportData(
        message=message,
        level=level,
        today=today,
        regime=regime or "",
        sharpe=sharpe,
        trades=total_trades,
        pnl_today=today_pnl,
    )


# ── Obsidian sync ─────────────────────────────────────────────────────────────

def _format_obsidian_note(data: ReportData) -> str:
    """Render the daily-note markdown body for an Obsidian vault.

    Frontmatter exposes the headline metrics so the QuantAI MOC's Dataview
    queries can aggregate across days; the full Telegram report body is
    embedded in a fenced text block to preserve emoji + alignment.
    """
    yesterday = data.today - timedelta(days=1)
    sharpe_str = f"{data.sharpe:.2f}" if data.sharpe is not None else "null"
    pnl_str = _fmt_pnl(data.pnl_today)
    regime = data.regime or "UNKNOWN"
    frontmatter = (
        "---\n"
        f"date: {data.today.isoformat()}\n"
        f"regime: {regime}\n"
        f"sharpe: {sharpe_str}\n"
        f"trades: {data.trades}\n"
        f'pnl_today: "{pnl_str}"\n'
        "tags: [quantai, trading, daily]\n"
        "---\n"
    )
    return (
        f"{frontmatter}\n"
        f"# {data.today.isoformat()} — Morning Report\n\n"
        f"← [[{yesterday.isoformat()}]]\n\n"
        "```text\n"
        f"{data.message}\n"
        "```\n\n"
        "---\n"
        "_Auto-generated by `scripts/morning_report.py`._\n"
    )


def save_to_obsidian(
    data: ReportData,
    daily_dir: Optional[str] = None,
) -> bool:
    """Write the morning report as a daily note to the Obsidian vault.

    Behaviour:
      - Vault directory is taken from `OBSIDIAN_DAILY_DIR` env, falling back
        to the WSL default `/mnt/c/.../QuantAI/Daily`.
      - Returns False (and logs INFO) if the directory does not exist —
        Cloud Run never has the WSL mount, so this is the graceful skip.
      - Returns False if `YYYY-MM-DD.md` already exists — manual notes that
        the user pre-fills (see `Daily/2026-04-25.md`) are never overwritten.
      - All write errors are caught and logged WARNING; the function never
        raises so it can't take down the daily cron.
    """
    daily_dir = daily_dir or os.environ.get(
        "OBSIDIAN_DAILY_DIR", _DEFAULT_OBSIDIAN_DAILY_DIR,
    )
    if not os.path.isdir(daily_dir):
        logger.info(
            "Obsidian vault not present (%s); skipping daily-note sync.",
            daily_dir,
        )
        return False

    note_path = os.path.join(daily_dir, f"{data.today.isoformat()}.md")
    if os.path.exists(note_path):
        logger.info(
            "Obsidian daily note already exists (%s); preserving manual edits.",
            note_path,
        )
        return False

    try:
        body = _format_obsidian_note(data)
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(body)
        logger.info("Obsidian daily note written: %s", note_path)
        return True
    except OSError as e:
        logger.warning("Obsidian sync failed (non-fatal): %s", e)
        return False


# ── Entry point ───────────────────────────────────────────────────────────────

def send_morning_report() -> bool:
    """Build, send via Telegram, and sync to Obsidian. Always non-fatal."""
    try:
        data = _build_report_data()
        logger.info("Sending morning report (level=%s)…", data.level)
        ok = send_alert(data.message, level=data.level)
        try:
            save_to_obsidian(data)
        except Exception as e:  # noqa: BLE001 — never break the cron on sync
            logger.warning("Obsidian sync failed (non-fatal): %s", e)
        return ok
    except Exception as e:
        logger.warning("Morning report failed (non-fatal): %s", e)
        return False


def main() -> None:
    ok = send_morning_report()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
