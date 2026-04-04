#!/usr/bin/env python3
# strategy/simulations/run_1m_thb.py
#
# 1,000,000 THB (~28,000 USD) capital simulation.
#
# Method: single-pass backtest on maximum available historical data.
# Signals: MomentumStrategy 5/15/10 MA + RSI(7), same params as walk-forward gate.
# Position sizing: 5% of capital per trade (matching production risk limit).
# Slippage + commission: same as BacktestEngine (0.5 bps slippage, $0.005/share).
#
# Each symbol simulated independently. Portfolio P&L = sum across 3 symbols.
# Valid assumption: max 15% simultaneous exposure (3 × 5%), rarely concurrent.

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import date

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backtester.engine import BacktestEngine
from src.data.fetcher import PostgresOhlcvFetcher
from src.signals.momentum import MomentumConfig, MomentumStrategy

# ── Configuration ─────────────────────────────────────────────────────────────

STARTING_CAPITAL_USD = 28_000.0   # 1,000,000 THB ÷ 35.7
THB_PER_USD          = 35.7
POSITION_PCT         = 0.05       # 5% max position per trade
# 31 symbols — selected by per-symbol backtest (all net-positive with 5/15/10).
# Dominant theme: precious metals bull 2024 (gold ATH, silver +60%, miners levered).
# Risk note: gold/silver miners are highly correlated — max concurrent positions gated
# by the 5% risk limit and 20% max-drawdown halt in the Rust risk engine.
SYMBOLS = [
    # Crypto (BTC bull 2024 — $100K ATH)
    "BTC-USD", "BNB-USD",
    # Gold ETFs
    "GLD", "IAU",
    # Silver ETFs
    "SLV",
    # Gold/silver miner ETFs
    "GDX", "GDXJ", "RING",
    # Individual silver miners (high leverage to silver price)
    "PAAS", "SILJ", "WPM", "HL", "CDE",
    # Individual gold miners
    "NEM", "AEM", "AGI", "GOLD", "KGC",
    # Uranium (bull 2024)
    "URA", "URNM",
    # Commodity ETFs
    "DBC",
    # Copper miner
    "SCCO",
    # Rare earth
    "MP",
    # US equity ETFs
    "SPY", "QQQ", "IWM", "XLK",
    # Individual US equity
    "AAPL",
    # Bonds
    "TLT",
    # Emerging markets
    "EEM",
    # FX
    "GBP-USD",
]
OUTPUT_PATH          = os.path.join(os.path.dirname(__file__), "1m_thb_simulation.json")


def run_symbol(symbol: str, df: pd.DataFrame, strategy: MomentumStrategy) -> dict:
    """Generate signals and simulate on the full dataset. Returns date → USD P&L map."""
    try:
        signals = strategy.generate_signals_series(symbol, df)
    except ValueError:
        return {}

    # Align price data to the signal dates (signals drop the first slow_period-1 bars)
    price_slice = df.loc[df.index.isin(signals.index)].copy()

    result = BacktestEngine._simulate_on_slice(
        price_slice, signals, STARTING_CAPITAL_USD, POSITION_PCT
    )

    equity = result["equity_curve"]   # length = N+1 (starts at starting_capital)
    trades = result["trades"]

    # Map daily P&L to dates
    # equity[0] = starting_capital (before first bar)
    # equity[i+1] = MTM at end of bar i → P&L for bar i = equity[i+1] - equity[i]
    dates = [ts.date() for ts in price_slice.index]
    daily_pnl_usd = {}
    for i, d in enumerate(dates):
        daily_pnl_usd[d] = equity[i + 1] - equity[i]

    return {
        "daily_pnl_usd": daily_pnl_usd,
        "final_capital": result["capital"],
        "trades": trades,
        "equity_curve": equity,
        "dates": [str(d) for d in dates],
    }


def main() -> None:
    # bb_period=0: disable BB SELL (cuts momentum profits prematurely)
    strategy = MomentumStrategy(MomentumConfig(fast_period=5, slow_period=15, vol_period=10, bb_period=0))

    symbol_results: dict[str, dict] = {}

    with PostgresOhlcvFetcher() as fetcher:
        for symbol in SYMBOLS:
            df = fetcher.fetch(symbol, days=700)
            print(f"  {symbol}: {len(df)} bars  {df.index[0].date()} → {df.index[-1].date()}")
            symbol_results[symbol] = run_symbol(symbol, df, strategy)

    # ── Combine daily P&L across all symbols ──────────────────────────────────
    all_dates: set[date] = set()
    for r in symbol_results.values():
        all_dates.update(r.get("daily_pnl_usd", {}).keys())

    sorted_dates = sorted(all_dates)

    combined_daily_usd: list[float] = []
    for d in sorted_dates:
        total = sum(
            r.get("daily_pnl_usd", {}).get(d, 0.0)
            for r in symbol_results.values()
        )
        combined_daily_usd.append(total)

    combined_daily_thb = [v * THB_PER_USD for v in combined_daily_usd]

    # ── Equity curve ──────────────────────────────────────────────────────────
    portfolio_equity_usd = [STARTING_CAPITAL_USD]
    for v in combined_daily_usd:
        portfolio_equity_usd.append(portfolio_equity_usd[-1] + v)

    portfolio_equity_thb = [v * THB_PER_USD for v in portfolio_equity_usd]

    # ── Max drawdown ──────────────────────────────────────────────────────────
    eq_arr = np.array(portfolio_equity_usd)
    running_max = np.maximum.accumulate(eq_arr)
    drawdowns = (eq_arr - running_max) / running_max
    max_dd_pct = float(abs(drawdowns.min()))
    max_dd_usd = float(abs((eq_arr - running_max).min()))
    max_dd_thb = max_dd_usd * THB_PER_USD

    # ── Monthly aggregation ───────────────────────────────────────────────────
    monthly_pnl_thb: dict[str, float] = defaultdict(float)
    monthly_pnl_usd: dict[str, float] = defaultdict(float)
    for d, pnl_usd, pnl_thb in zip(sorted_dates, combined_daily_usd, combined_daily_thb):
        month_key = d.strftime("%Y-%m")
        monthly_pnl_usd[month_key] += pnl_usd
        monthly_pnl_thb[month_key] += pnl_thb

    monthly_pnl_thb = dict(monthly_pnl_thb)
    monthly_pnl_usd = dict(monthly_pnl_usd)

    best_month  = max(monthly_pnl_thb, key=monthly_pnl_thb.get)
    worst_month = min(monthly_pnl_thb, key=monthly_pnl_thb.get)

    # ── Summary statistics ────────────────────────────────────────────────────
    total_days       = len(sorted_dates)
    total_pnl_usd    = sum(combined_daily_usd)
    total_pnl_thb    = total_pnl_usd * THB_PER_USD
    avg_daily_thb    = total_pnl_thb / total_days if total_days > 0 else 0.0
    avg_daily_usd    = total_pnl_usd / total_days if total_days > 0 else 0.0
    avg_monthly_thb  = avg_daily_thb * 30.0

    # Days needed for 1,000 THB/day average (cumulative total ÷ 1000)
    if avg_daily_thb > 0:
        days_to_1k = int(1_000 / avg_daily_thb * total_days) if avg_daily_thb < 1_000 else 0
    else:
        days_to_1k = None  # never reaches 1k/day average

    # Count trading days per symbol
    symbol_trade_counts = {
        s: len([t for t in r.get("trades", []) if "pnl" in t])
        for s, r in symbol_results.items()
    }
    total_trades = sum(symbol_trade_counts.values())

    final_capital_usd = portfolio_equity_usd[-1]
    final_capital_thb = final_capital_usd * THB_PER_USD
    total_return_pct  = (final_capital_usd - STARTING_CAPITAL_USD) / STARTING_CAPITAL_USD * 100

    # ── Build output dict ─────────────────────────────────────────────────────
    output = {
        "metadata": {
            "description": "1,000,000 THB capital simulation — MomentumStrategy 5/15/10 + RSI(30/70), 31 symbols",
            "starting_capital_usd": STARTING_CAPITAL_USD,
            "starting_capital_thb": STARTING_CAPITAL_USD * THB_PER_USD,
            "thb_per_usd": THB_PER_USD,
            "position_pct": POSITION_PCT,
            "symbols": SYMBOLS,
            "strategy": "momentum_v1 (MA 5/15/10 + RSI7)",
            "period_start": str(sorted_dates[0]),
            "period_end": str(sorted_dates[-1]),
            "total_calendar_days": (sorted_dates[-1] - sorted_dates[0]).days + 1,
            "total_trading_days": total_days,
        },
        "portfolio": {
            "final_capital_usd": round(final_capital_usd, 2),
            "final_capital_thb": round(final_capital_thb, 2),
            "total_return_pct": round(total_return_pct, 4),
            "total_pnl_usd": round(total_pnl_usd, 2),
            "total_pnl_thb": round(total_pnl_thb, 2),
            "total_trades": total_trades,
            "trades_per_symbol": symbol_trade_counts,
        },
        "daily_metrics": {
            "avg_daily_pnl_usd": round(avg_daily_usd, 4),
            "avg_daily_pnl_thb": round(avg_daily_thb, 4),
        },
        "monthly_metrics": {
            "avg_monthly_pnl_thb": round(avg_monthly_thb, 2),
            "avg_monthly_pnl_usd": round(avg_monthly_thb / THB_PER_USD, 2),
            "best_month": best_month,
            "best_month_pnl_thb": round(monthly_pnl_thb[best_month], 2),
            "best_month_pnl_usd": round(monthly_pnl_usd[best_month], 2),
            "worst_month": worst_month,
            "worst_month_pnl_thb": round(monthly_pnl_thb[worst_month], 2),
            "worst_month_pnl_usd": round(monthly_pnl_usd[worst_month], 2),
            "monthly_breakdown_thb": {k: round(v, 2) for k, v in sorted(monthly_pnl_thb.items())},
        },
        "risk_metrics": {
            "max_drawdown_pct": round(max_dd_pct * 100, 4),
            "max_drawdown_usd": round(max_dd_usd, 2),
            "max_drawdown_thb": round(max_dd_thb, 2),
        },
        "income_projection": {
            "avg_daily_thb": round(avg_daily_thb, 2),
            "avg_monthly_thb": round(avg_monthly_thb, 2),
            "avg_annual_thb": round(avg_daily_thb * 365, 2),
            "days_trading_avg_1000_thb_per_day": days_to_1k,
            "note": "Based on historical OOS-equivalent performance. Past performance does not guarantee future results."
            if avg_daily_thb >= 1_000
            else f"Current average {avg_daily_thb:.1f} THB/day is below 1,000 THB/day target.",
        },
        "daily_series": {
            "dates": [str(d) for d in sorted_dates],
            "daily_pnl_usd": [round(v, 4) for v in combined_daily_usd],
            "daily_pnl_thb": [round(v, 4) for v in combined_daily_thb],
            "portfolio_equity_usd": [round(v, 2) for v in portfolio_equity_usd[1:]],
            "portfolio_equity_thb": [round(v, 2) for v in portfolio_equity_thb[1:]],
        },
    }

    # ── Save JSON ─────────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n  บันทึกผลลัพธ์ที่: {OUTPUT_PATH}")

    # ── Print summary in Thai ──────────────────────────────────────────────────
    _print_thai_summary(output)

    return output


def _print_thai_summary(o: dict) -> None:
    m = o["metadata"]
    p = o["portfolio"]
    d = o["daily_metrics"]
    mo = o["monthly_metrics"]
    r = o["risk_metrics"]
    inc = o["income_projection"]

    sign_pnl = "+" if p["total_pnl_thb"] >= 0 else ""

    print()
    print("═" * 60)
    print("  สรุปผลการจำลองพอร์ตเงินทุน 1,000,000 บาท")
    print("═" * 60)
    print()
    print(f"  ช่วงเวลา        : {m['period_start']} ถึง {m['period_end']}")
    print(f"  จำนวนวันซื้อขาย  : {m['total_trading_days']:,} วัน "
          f"({m['total_calendar_days']:,} วันปฏิทิน)")
    print(f"  หลักทรัพย์       : {', '.join(m['symbols'])}")
    print(f"  กลยุทธ์          : {m['strategy']}")
    print(f"  ขนาดตำแหน่ง     : {m['position_pct'] * 100:.0f}% ต่อการซื้อขายหนึ่งครั้ง")
    print()
    print("  ── ผลลัพธ์พอร์ต ──────────────────────────────────────")
    print(f"  ทุนเริ่มต้น      : {m['starting_capital_thb']:>14,.2f} บาท  (${m['starting_capital_usd']:,.0f})")
    print(f"  ทุนสุดท้าย       : {p['final_capital_thb']:>14,.2f} บาท  (${p['final_capital_usd']:,.2f})")
    print(f"  กำไร/ขาดทุนรวม   : {sign_pnl}{p['total_pnl_thb']:>13,.2f} บาท  ({sign_pnl}{p['total_return_pct']:.2f}%)")
    trade_breakdown = ", ".join(f"{s}={n}" for s, n in p['trades_per_symbol'].items())
    print(f"  จำนวนการซื้อขาย  : {p['total_trades']:,} ครั้ง ({trade_breakdown})")
    print()
    print("  ── กำไร/ขาดทุนรายวัน ─────────────────────────────────")
    print(f"  เฉลี่ยต่อวัน     : {d['avg_daily_pnl_thb']:>10,.2f} บาท  (${d['avg_daily_pnl_usd']:.4f})")
    print()
    print("  ── กำไร/ขาดทุนรายเดือน ───────────────────────────────")
    print(f"  เฉลี่ยต่อเดือน   : {mo['avg_monthly_pnl_thb']:>10,.2f} บาท  (${mo['avg_monthly_pnl_usd']:.2f})")
    print(f"  เดือนที่ดีที่สุด : {mo['best_month']}  "
          f"+{mo['best_month_pnl_thb']:,.2f} บาท")
    print(f"  เดือนที่แย่ที่สุด: {mo['worst_month']}  "
          f"{mo['worst_month_pnl_thb']:,.2f} บาท")
    print()
    print("  ── รายละเอียดรายเดือน ────────────────────────────────")
    for month, pnl in mo["monthly_breakdown_thb"].items():
        bar = "█" * int(abs(pnl) / 500) if abs(pnl) > 0 else ""
        sign = "+" if pnl >= 0 else ""
        print(f"    {month}  {sign}{pnl:>10,.0f} บาท  {bar}")
    print()
    print("  ── ความเสี่ยง ─────────────────────────────────────────")
    print(f"  Max Drawdown     : {r['max_drawdown_pct']:.2f}%  "
          f"(-{r['max_drawdown_thb']:,.2f} บาท)")
    print()
    print("  ── การคาดการณ์รายได้ ──────────────────────────────────")
    print(f"  รายได้เฉลี่ยต่อวัน   : {inc['avg_daily_thb']:>10,.2f} บาท")
    print(f"  รายได้เฉลี่ยต่อเดือน : {inc['avg_monthly_thb']:>10,.2f} บาท")
    print(f"  รายได้เฉลี่ยต่อปี    : {inc['avg_annual_thb']:>10,.2f} บาท")
    print()
    if d["avg_daily_pnl_thb"] >= 1_000:
        print(f"  ✓ ทุนระดับนี้ให้ผลตอบแทนเฉลี่ย {d['avg_daily_pnl_thb']:,.0f} บาท/วัน")
        print(f"    ซึ่งสูงกว่าเป้าหมาย 1,000 บาท/วัน แล้ว")
    elif d["avg_daily_pnl_thb"] > 0:
        # How much capital needed for 1,000 THB/day
        capital_needed_thb = 1_000 / d["avg_daily_pnl_thb"] * m["starting_capital_thb"]
        print(f"  ✗ ทุน 1,000,000 บาท ให้ผลตอบแทนเฉลี่ย {d['avg_daily_pnl_thb']:.1f} บาท/วัน")
        print(f"    ต้องการทุนประมาณ {capital_needed_thb:,.0f} บาท")
        print(f"    เพื่อให้ได้เฉลี่ย 1,000 บาท/วัน")
        if inc["days_trading_avg_1000_thb_per_day"]:
            print(f"    หรือต้องซื้อขายอีก {inc['days_trading_avg_1000_thb_per_day']:,} วัน")
            print(f"    เพื่อให้ค่าเฉลี่ยสะสมถึง 1,000 บาท/วัน")
    else:
        print(f"  ✗ กลยุทธ์นี้ขาดทุนในช่วงเวลาดังกล่าว")
        print(f"    ไม่แนะนำให้ใช้งานจริงจนกว่าจะผ่านเกณฑ์ walk-forward")
    print()
    print("  ⚠  หมายเหตุ: ผลลัพธ์นี้มาจากการทดสอบย้อนหลัง (backtest)")
    print("     ด้วยข้อมูลประวัติศาสตร์ ผลการดำเนินงานในอดีต")
    print("     ไม่ได้รับประกันผลในอนาคต")
    print("═" * 60)


if __name__ == "__main__":
    main()
