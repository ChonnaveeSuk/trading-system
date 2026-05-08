# QuantAI: Line-by-Line Code Review

## Executive Summary
This deep code review covers the core execution and strategy layers of the QuantAI Trading System, specifically analyzing `momentum.py`, `alpaca_direct.py`, `economic_calendar.py`, and `run_strategy.py`. 

*(Note: `vix_filter.py` and `position_sizer.py` do not exist as standalone files; their logic has been explicitly embedded directly into `momentum.py`'s `update_vix` and position sizing blocks, which will be reviewed accordingly.)*

---

## 1. `strategy/src/signals/momentum.py`

### Purpose
The absolute core mathematical engine of QuantAI. It generates `BUY`, `SELL`, or `HOLD` signals based on a multi-layered evaluation of Dual Moving Averages, RSI Mean-Reversion, Bollinger Bands, and a "Trend Ride" pullback detector, while applying 8 defensive filters (Regime, VIX, Earnings, Calendar, Trend, Stop-Loss, Sector, Position).

### Key Classes & Functions
- **`MomentumConfig` (DataClass):**
  - *Responsibilities:* Holds all tuning parameters.
  - *Constants:* `fast_period=10` (default, but usually overridden to 5), `slow_period=30` (overridden to 15). `noise_filter_bps=5.0` (5 basis points). Changing `noise_filter_bps` to 1.0 would cause massive whipsaws on flat days. `atr_risk_pct=0.01` determines the fraction of portfolio risked.
- **`MomentumStrategy` (Class):**
  - *State Management:* Holds `_regime` and `_vix_state` across the symbol loop. This is critical: SPY and VIXY are fetched *once*, cached, and applied to all 16 symbols to avoid redundant DB queries. Lazy-loads `EconomicCalendar` and `EarningsCalendar`.
  - **`update_regime(spy_df)` / `update_vix(vixy_df)`:** Computes state based on proxy symbols. *Edge Case:* If `data_age_days > 30`, it defaults to `BULL` or `CALM`. *Issue:* Defaulting to permissive states on stale data is dangerous. It should fail closed (`BEAR` / `PANIC`) or halt the system.
  - **`generate_signal()`:** 
    - *Inputs:* `symbol`, `df`, `portfolio_value`, `as_of_date`.
    - *Logic Flow:*
      1. Indicator Calculation (MA, RSI, BB, ATR).
      2. Signal Detection (RSI extreme $\rightarrow$ Trend Ride $\rightarrow$ BB $\rightarrow$ MA Crossover).
      3. Scoring (0.55 base + up to 0.15 for MA spread + up to 0.30 for RSI extremity).
      4. Hard Gating (Regime, VIX, Calendar, Earnings).
      5. ATR Position Sizing (Capped at 5%).
    - *Edge Cases:* Handles FX sparse volume correctly by bypassing the volume check and applying a 4x noise threshold multiplier. Handles missing `unrealized_plpc` gracefully.

### Thread Safety & Memory
- *Thread Safety:* Python's GIL protects the basic state, but `MomentumStrategy` instances are strictly stateful (`_regime`, `_vix_state`). They are not thread-safe if shared across concurrent worker threads evaluating symbols asynchronously. Currently, the symbol loop in `run_strategy.py` is entirely synchronous, so this is safe.
- *Memory Usage:* Very efficient. Uses vectorized Pandas rolling windows. It only loads 90 days of history for each symbol into memory. No leaks detected.

### Suggested Improvements
```python
# 1. Fail Closed on Stale Proxy Data
if data_age_days > 7:
    logger.critical("SPY data stale. Failing closed.")
    self._regime = "BEAR"  # Instead of BULL
    return "BEAR"

# 2. Extract Position Sizer to its own pure function to simplify generate_signal
def _calculate_qty(portfolio_value, atr_val, curr_price, atr_risk_pct, vix_state):
    risk_dollars = portfolio_value * atr_risk_pct
    raw_qty = risk_dollars / atr_val
    max_qty = (portfolio_value * 0.05) / curr_price 
    qty = min(raw_qty, max_qty)
    if vix_state == "CAUTION":
         qty *= 0.5
    return qty
```

---

## 2. `strategy/src/bridge/alpaca_direct.py`

### Purpose
Bypasses the legacy Rust gRPC OMS entirely to submit REST API orders directly to Alpaca. It implements the critical Risk Engine rules (Sector concentration, max positions) in a serverless-friendly way.

### Key Classes & Functions
- **`AlpacaDirectClient` (Class):**
  - *State Management:* Maintains a `requests.Session()` and tracks `_submitted_symbols` as a set during a single run to prevent double-buys.
  - **`check_and_trigger_stops()`:** 
    - Fetches Alpaca positions, compares `unrealized_plpc` against `_effective_stop_loss_pct(symbol)`. If breached, fires a `DELETE` request. 
    - *Error Handling:* Excellent. Caught `requests.HTTPError` does not crash the loop; it logs the error and continues to evaluate the next symbol.
  - **`submit_signal()`:** 
    - Validates the `SignalResult`. 
    - Applies Guards:
      1. Already long (skips).
      2. Duplicate in session (skips).
      3. Pending cross-invocation order (skips).
      4. **Sector Concentration (The most critical code in the project):** Rejects if sector exceeds 3 positions or 30% notional.
    - *Data Flow:* Validated Signal $\rightarrow$ POST `/orders` $\rightarrow$ `_record_order_pg` $\rightarrow$ Returns `BridgeResponse`.

### Thread Safety & Memory
- *Thread Safety:* Not thread-safe due to `_submitted_symbols` set and `_session`. Must be instantiated per process/thread.
- *Memory Usage:* Minimal. Holds small JSON dictionaries of current positions.

### Suggested Improvements
```python
# The JSONL fallback logger catches DB connection issues but blindly appends. 
# We should add a file size limit check or log rotation to prevent infinite growth.
try:
    if os.path.exists(_FAILED_ORDERS_LOG) and os.path.getsize(_FAILED_ORDERS_LOG) > 10 * 1024 * 1024:
        logger.error("Fallback log exceeds 10MB. Manual intervention required.")
    else:
        with open(_FAILED_ORDERS_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
```

---

## 3. `strategy/src/filters/economic_calendar.py`

### Purpose
Maintains a hardcoded calendar of high-impact macro events (FOMC, CPI, NFP) and per-symbol corporate earnings. Blocks `BUY` orders on the day of or the day before these events to avoid extreme binary volatility.

### Key Classes & Functions
- **`EconomicCalendar` / `EarningsCalendar` (Classes):**
  - *State Management:* Immutable post-initialization. Initializes sorted lists of `EconomicEvent` and `EarningsEvent` dataclasses.
  - **`is_blackout_day(d: date)`:** Checks if date `d` equals the event date OR falls within `blackout_days_before`.
  - *Constants:* The entire 2026 calendar is hardcoded. NFP and CPI are auto-generated via datetime math (e.g., `_first_friday(year, month)`).
  - *Error Handling:* `event_within` elegantly avoids out-of-bounds errors by early exiting the sorted list loop.

### Thread Safety & Memory
- *Thread Safety:* 100% thread-safe. State is read-only after init.
- *Memory Usage:* Negligible. Less than 100 dataclass instances in memory.

### Suggested Improvements
- *Technical Debt:* The hardcoded `_EARNINGS_2026` array contains "projected" Q2/Q3/Q4 dates based on historical cadences. If an issuer shifts their earnings by a week, the system will blackout on the wrong dates. This requires manual maintenance.
```python
# Improvement: Add an explicit "projected" flag to the EarningsEvent dataclass.
# Then, in the Morning Report, warn the operator if a blackout is firing based on a projected date, prompting them to manually verify it on Investor Relations pages.
```

---

## 4. `strategy/run_strategy.py`

### Purpose
The CLI entrypoint and orchestrator. It manages the daily execution loop: fetching data, updating regime/VIX, checking the calendar, iterating over symbols to generate signals, evaluating stop-losses, and firing Telegram alerts.

### Key Classes & Functions
- **`run_live()`:**
  - *Data Flow:* `AlpacaDirectClient` init $\rightarrow$ `check_and_trigger_stops` $\rightarrow$ `update_regime(SPY)` $\rightarrow$ `update_vix(VIXY)` $\rightarrow$ `EconomicCalendar` check $\rightarrow$ Symbol Loop (fetch 90 days $\rightarrow$ `generate_signal` $\rightarrow$ `submit_signal`).
  - *Edge Cases:* Detects "stale" data inside the symbol loop: `if data_age_days > _LIVE_STALE_DAYS (7)`. If the latest bar is older than 7 calendar days, it completely skips the symbol. This protects against holidays but prevents trading on broken feeds.
- **`_check_and_record_regime_change()` & `_check_and_record_vix_change()`:**
  - Connects to Postgres to log the state to `system_metrics` and fires Telegram alerts if the state changed from the previous day. 

### Error Handling & Issues
- *Silent Failures:* The Telegram alert function `_telegram_alert` is wrapped in broad `except Exception` blocks and returns `False`. If Telegram is down, the system continues trading but flies blind operationally. This is acceptable for alerts, but dangerous if the operator relies on them to manually monitor the 90-day gate.
- *Database Management:* `psycopg2` connections are created, used, and closed repeatedly inside `_check_and_record_regime_change`, `_check_and_record_vix_change`, and `_check_max_drawdown_alert`. This is a classic N+1 connection overhead issue.

### Suggested Improvements
```python
# Database connection pooling / reuse in run_live
# Instead of opening/closing 3 distinct psycopg2 connections for metrics, 
# pass a single shared connection or rely on a connection pool.

# Example:
with psycopg2.connect(_database_url()) as conn:
    _check_and_record_regime_change(regime, spy_price, spy_ma200, conn)
    _check_and_record_vix_change(vix_state, vix_level, vix_price, conn)
    _check_max_drawdown_alert(conn)
```
