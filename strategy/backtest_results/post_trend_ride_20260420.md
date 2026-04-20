# Post-trend_ride Backtest Results
**Date:** 2026-04-20  
**Commit:** d65d91d (feat: separate exit logic for trend_ride entries)  
**Config:** MA 5/15/10 + RSI(7) + trend_ride_rsi=45 + trend_ride_min_bars=10 + exit_gate(MA20>MA50) + trailing_stop=True (2.0x ATR)  
**Data:** 31 symbols, 2024-04-29 → 2026-04-15 (477+ bars per symbol)

---

## 1. Portfolio Metrics vs Baseline

| Metric | Baseline (pre-trend_ride) | No trend_ride (control) | **With trend_ride** | Delta vs baseline |
|--------|--------------------------|------------------------|---------------------|-------------------|
| Sharpe | 1.61 | 1.7516 | **1.3938** | −0.22 ⚠ |
| MaxDD | 8.86% | 9.66% | **12.56%** | +3.70pp ⚠ |
| Annual Return | 63.81% (total) | 65.81% (total) | **~62%** (total) | similar |
| Avg Daily P&L | 992 THB/day | 964.6 THB/day | **1009.4 THB/day** | +17 THB/day ✓ |
| Total Trades | 306 | 406 | **518** | +112 (+37%) |
| Trend_ride Entries | 0 | 0 | **191** | — |

**Key finding:** The control run (same date range, no trend_ride) shows Sharpe **1.75** and MaxDD **9.66%** — better than even the baseline. This confirms the period 2024-2026 is favourable for the base strategy. Adding trend_ride **degrades Sharpe by 0.36** and **raises MaxDD by 2.90pp** relative to the control on identical data.

---

## 2. AAPL Walk-Forward W0–W5 Progression

Previous W0–W5 best AAPL aggregate Sharpe: **1.96** (4/6 passing, trailing_stop=True)

| Window | OOS Period | Sharpe | MaxDD | Trades | Gate |
|--------|-----------|--------|-------|--------|------|
| W0 | 2025-08-06 → 2025-11-03 | 3.171 | 0.0% | 2 | ✓ PASS |
| W1 | 2025-09-05 → 2025-12-03 | 3.889 | 0.0% | 2 | ✓ PASS |
| W2 | 2025-10-06 → 2026-01-05 | 3.867 | 0.0% | 3 | ✓ PASS |
| W3 | 2025-11-04 → 2026-02-04 | 1.445 | 0.1% | 4 | ✓ PASS |
| W4 | 2025-12-04 → 2026-03-04 | 0.553 | 0.7% | 5 | ✗ FAIL |
| W5 | 2026-01-06 → 2026-03-25 | −0.467 | 1.0% | 5 | ✗ FAIL |

**AAPL aggregate Sharpe: 0.204** (was 1.96) — **severe regression**.  
W4/W5 are failing. W5 (−0.467) covers 2026-Jan to late-Mar, the AAPL correction period. Worst single trade: trend_ride entry 2026-02-12 at $261.74, exited 2026-02-28 at $183.99 (30% drop, −14,974 THB). The trailing stop (2x ATR ≈ $26) was overwhelmed by the gap-down.

Previous W5 was 0.08; with trend_ride W5 is now **−0.47** — worse, not better.

---

## 3. Trade Count Delta

| Category | Count | Share |
|----------|-------|-------|
| Total closed trades (with trend_ride) | 518 | 100% |
| Total closed trades (control, no trend_ride) | 406 | — |
| New trades from trend_ride | +112 net | +28% |
| trend_ride BUY entries | 191 | 37% of all trades |
| trend_ride wins | 94 / 191 | 49.2% win rate |
| trend_ride losses | 97 / 191 | 50.8% loss rate |
| trend_ride total P&L | +118,864 THB | +3,329 USD |
| — via RSI/normal SELL | 98 exits | +401,972 THB |
| — via trailing stop | 93 exits | **−283,108 THB** |

**Root cause of degradation:** The trailing stop fires on 93 of 191 trend_ride exits (49%) and destroys −283,108 THB. Normal exits earn +401,972 THB. Net positive (+118,864 THB) but the trail-stop losses inflate MaxDD and reduce Sharpe. The exits are concentrated in late 2025–early 2026 market corrections.

---

## 4. Per-Symbol Breakdown — Full 31 Symbols

### Gold Miners (focus group)

| Symbol | Bars | Trades | All P&L (THB) | TR Entries | TR P&L (THB) | TR Win Rate | WF Sharpe | WF Pass |
|--------|------|--------|---------------|-----------|--------------|-------------|-----------|---------|
| GDX | 477 | 11 | +25,301 | 3 | +9,124 | 2/3 | 1.337 | 7/8 |
| GDXJ | 477 | 14 | +34,610 | 6 | +187 | 2/6 | 1.396 | 7/8 |
| NEM | 477 | 18 | +25,523 | 9 | +14,475 | 6/9 | 0.714 | 7/8 |
| AEM | 477 | 16 | +54,012 | 7 | +25,626 | 6/7 | 2.477 | 7/8 |
| AGI | 477 | 13 | +36,757 | 3 | +1,746 | 2/3 | 1.206 | 8/8 |
| KGC | 477 | 12 | +28,885 | 7 | +9,237 | 4/7 | 1.845 | 7/8 |
| GOLD | 477 | 20 | +9,692 | 3 | +14,960 | 3/3 | 2.702 | 8/8 |
| PAAS | 477 | 14 | +34,455 | 5 | −4,737 | 1/5 | 1.538 | 8/8 |
| WPM | 477 | 17 | +40,818 | 9 | +15,329 | 6/9 | 2.164 | 6/8 |
| HL | 477 | 16 | +13,558 | 3 | **−7,862** | 1/3 | **−1.047** | 7/8 ⚠ |
| CDE | 477 | 16 | +84,484 | 7 | +310 | 4/7 | 1.637 | 8/8 |
| RING | 477 | 12 | +30,350 | 5 | +15,317 | 4/5 | 1.013 | 6/8 |
| SILJ | 477 | 13 | +27,293 | 6 | **−7,358** | 1/6 | 1.112 | 7/8 ⚠ |

**Gold miners summary:** Most walk-forward Sharpes remain solid (AEM 2.48, GOLD 2.70, KGC 1.85, WPM 2.16). HL and SILJ are the weakest trend_ride performers — both small/volatile silver names where 2x ATR trailing stops are overwhelmed by sharp drops.

### Other Symbols

| Symbol | Trades | All P&L (THB) | TR Entries | TR P&L (THB) | TR Win Rate |
|--------|--------|---------------|-----------|--------------|-------------|
| BTC-USD | 26 | +23,469 | 9 | +993 | 4/9 |
| BNB-USD | 26 | +31,771 | 11 | −2,025 | 4/11 |
| MP | 22 | **+71,107** | 8 | **+26,244** | 4/8 ⭐ |
| AAPL | 23 | **−8,191** | 5 | **−16,736** | 2/5 🚨 |
| URNM | 20 | **−7,843** | 4 | **−7,307** | 0/4 🚨 |
| QQQ | 20 | +9,831 | 9 | +2,734 | 3/9 |
| IWM | 15 | +2,193 | 5 | −6,296 | 1/5 ⚠ |
| DBC | 20 | +14,978 | 9 | +5,691 | 5/9 |
| SCCO | 19 | +19,758 | 5 | +4,218 | 2/5 |
| SPY | 18 | +10,676 | 5 | +2,848 | 3/5 |
| TLT | 19 | −1,218 | 9 | −704 | 3/9 |
| EEM | 18 | +16,424 | 8 | +3,651 | 4/8 |
| XLK | 20 | +21,906 | 8 | +3,729 | 4/8 |
| SLV | 14 | +22,960 | 7 | +3,394 | 4/7 |
| GLD | 13 | +7,672 | 6 | +7,367 | 4/6 |
| IAU | 13 | +7,198 | 6 | +7,544 | 4/6 |
| URA | 18 | +18,656 | 4 | −2,833 | 1/4 |

---

## 5. Trend_ride Trade Log — Worst/Best and Notable Exits

### Worst trend_ride trades (by P&L)

| Symbol | Entry | Exit | Exit Reason | P&L (THB) |
|--------|-------|------|-------------|-----------|
| AAPL | 2026-02-12 | 2026-02-28 | SELL (trail) | **−14,974** |
| MP | 2025-10-21 | 2025-11-04 | SELL (trail) | −14,155 |
| AAPL | 2026-02-12 | 2026-02-28 | SELL (trail) | −14,974 |
| CDE | 2025-10-21 | 2025-11-03 | SELL (trail) | −9,627 |
| CDE | 2025-02-14 | 2025-02-21 | SELL (trail) | −9,081 |
| HL | 2024-07-25 | 2024-08-02 | SELL (trail) | −6,943 |
| HL | 2025-02-14 | 2026-04-04 | SELL (trail) | −6,769 |
| URNM | 2026-02-04 | 2026-03-05 | SELL | −2,549 (total 4 URNM = −7,307) |
| SILJ | 2024-10-31 | 2024-11-11 | SELL (trail) | −5,037 |
| PAAS | 2026-03-05 | 2026-03-18 | SELL (trail) | −7,058 |

**Pattern:** All worst trades are trailing stop exits during sharp corrections. The 2x ATR stop is insufficient for small-cap miners (CDE, HL, SILJ) and tech (AAPL, URNM) which can drop 15-30% in days.

### Best trend_ride trades (by P&L)

| Symbol | Entry | Exit | Exit Reason | P&L (THB) |
|--------|-------|------|-------------|-----------|
| MP | 2025-07-01 | 2025-07-10 | SELL | **+22,670** |
| MP | 2025-10-01 | 2025-10-13 | SELL | +21,606 |
| GDXJ | 2026-01-30 | 2026-02-26 | SELL | +11,974 |
| RING | 2026-01-30 | 2026-02-26 | SELL | +10,544 |
| GDX | 2026-01-30 | 2026-02-26 | SELL | +10,557 |
| PAAS | 2026-01-30 | 2026-02-26 | SELL | +12,008 |
| AEM | 2026-01-30 | 2026-02-23 | SELL | +13,564 |
| CDE | 2026-01-30 | 2026-02-26 | SELL | +15,984 |
| WPM | 2026-01-30 | 2026-02-24 | SELL | +9,890 |

**Pattern:** The Jan-Feb 2026 gold bull run generated a cluster of excellent trend_ride exits across miners on 2026-01-30 entries. All exited via normal SELL (RSI overbought or MA crossover), not trailing stop. This is the ideal trend_ride scenario working as designed.

---

## 6. Red Flags

### 🚨 High Severity

| Issue | Detail |
|-------|--------|
| AAPL: only losing symbol overall | Total P&L −8,191 THB, trend_ride contributes −16,736 THB (5 entries, 2/5 win rate). Single worst trade: −14,974 THB (Feb 2026 correction). |
| URNM: all 4 trend_ride entries losing | 0/4 win rate, −7,307 THB. Uranium stocks are low-volume and highly volatile — trend_ride entries get stopped out immediately. |
| Trailing stop interaction | 93 trail exits on trend_ride positions net −283,108 THB. The 2x ATR stop is too tight for volatile trend_ride entries during market corrections. |

### ⚠ Medium Severity

| Issue | Detail |
|-------|--------|
| AAPL walk-forward collapse | Aggregate Sharpe fell from 1.96 to 0.204. W4 (0.55) and W5 (−0.47) failing. W5 was 0.08 before — now worse. |
| HL walk-forward Sharpe −1.047 | HL is a sub-$10 silver miner. Trend_ride entries (1/3 win rate, −7,862 THB) are driving negative OOS Sharpe. |
| Portfolio Sharpe regression | 1.75 (no trend_ride) → 1.39 (with trend_ride): −0.36 degradation |
| Portfolio MaxDD increase | 9.66% (no trend_ride) → 12.56% (with trend_ride): +2.90pp increase |
| IWM trend_ride | 1/5 win rate, −6,296 THB. Small caps volatile in corrections. |

---

## 7. Recommendation

**By user-defined criteria:**
- MaxDD 12.56% → 10–13% range → **CAUTION zone**
- Sharpe 1.394 → 1.2–1.5 range → **CAUTION zone**
- Neither threshold crossed for ROLLBACK (MaxDD < 13%, Sharpe > 1.2)

### ✅ PROCEED WITH CAUTION — add alert

**Rationale:**
1. Trend_ride generates +118,864 THB net positive P&L across 191 trades (+1.45x win/loss ratio) — the signal itself has edge.
2. The degradation comes from the 2x ATR trailing stop interaction: pullback entries + tight stops = frequent stop-outs in corrections. This is a **stop-size problem**, not a signal-quality problem.
3. Gold miners (the highest-alpha symbols) mostly look healthy in walk-forward: AEM 2.48, GOLD 2.70, KGC 1.85.
4. Paper trading only — MaxDD gate is 15% (we're at 12.56%).

**Immediate actions before tonight's 22:00 UTC run:**

| Action | Detail |
|--------|--------|
| ⚠ Add MaxDD monitoring alert | Alert at 10% (pre-trigger warning). Already exists at 8% in `_check_max_drawdown_alert()`. Lower threshold to 8% warn / 12% critical. |
| 🔴 Disable trend_ride for AAPL in live symbols | AAPL trend_ride is −16,736 THB net, W5 negative. Consider adding AAPL to a `trend_ride_excluded` list. |
| 🔴 Disable trend_ride for URNM and HL | Both showing 0/4 and 1/3 win rates. Not worth the downside. |
| 📋 Monitor trail-stop rate on trend_ride | If > 50% of trend_ride exits continue hitting trail stops, increase multiplier from 2.0x to 3.0x or 4.0x specifically for trend_ride entries. |
| 📋 7-day review | After 7 paper trading days, compare live trend_ride entries vs predictions here. If AAPL or small miners generate more trail-stop exits, disable per-symbol. |

**Bigger picture:** The core finding is that trend_ride + 2x ATR trailing stop = a mismatched pair. Trend_ride enters pullbacks in volatile stocks; 2x ATR stops get hit during those same pullbacks. The exit gate (MA20 > MA50) correctly handles the MA crossover problem but can't prevent trailing stops from firing on real corrections. The right fix is either a wider stop for trend_ride entries or disabling trailing_stop specifically for trend_ride positions — which requires per-entry state tracking (deferred Phase 2 work from the exit logic session).

---

## 8. Walk-Forward Summary — Gold Miners

| Symbol | WF Sharpe | MaxDD | Pass Rate | TR Entries |
|--------|-----------|-------|-----------|-----------|
| AEM | 2.477 | 0.6% | 7/8 (88%) | 7 |
| GOLD | 2.702 | 1.0% | 8/8 (100%) | 3 |
| WPM | 2.164 | 0.6% | 6/8 (75%) | 9 |
| KGC | 1.845 | 0.7% | 7/8 (88%) | 7 |
| CDE | 1.637 | 1.1% | 8/8 (100%) | 7 |
| PAAS | 1.538 | 0.5% | 8/8 (100%) | 5 |
| GDX | 1.337 | 0.5% | 7/8 (88%) | 3 |
| GDXJ | 1.396 | 0.5% | 7/8 (88%) | 6 |
| AGI | 1.206 | 0.7% | 8/8 (100%) | 3 |
| RING | 1.013 | 0.5% | 6/8 (75%) | 5 |
| NEM | 0.714 | 0.6% | 7/8 (88%) | 9 |
| SILJ | 1.112 | 0.4% | 7/8 (88%) | 6 |
| HL | **−1.047** | 0.6% | 7/8 (88%) | 3 |

Gold miners overall remain solid. The walk-forward gate pass rates are 75–100%. HL is the exception and should be watched.

---

## Appendix: Run Parameters

```
Config: MomentumConfig(fast_period=5, slow_period=15, vol_period=10, bb_period=0,
                        trend_ride_rsi=45.0, trend_ride_min_bars=10,
                        trend_ride_exit_fast=20, trend_ride_exit_slow=50)
BacktestConfig: trailing_stop=True, trailing_stop_atr_mult=2.0
Capital: $28,000 USD (1,000,000 THB @ 35.7)
Position size: 5% per trade
Commission: $0.005/share (min $1, max 0.5%)
Slippage: 0.5 bps one-way
Period: 2024-04-29 → 2026-04-15 (477+ bars)
Regime: BULL (live SPY vs MA200)
```
