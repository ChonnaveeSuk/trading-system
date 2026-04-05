// trading-system/core/src/risk/mod.rs
//
// Risk engine — the sacred module. ALL orders pass through here before submission.
// Limits are hardcoded; no runtime override is possible.
//
// Golden rule: if check_order returns Err, the order DIES HERE. It never reaches
// the broker. Not in paper mode, not in live mode. Ever.

use std::collections::HashMap;

use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use tracing::{info, warn};

use crate::{
    error::RiskError,
    types::{Order, Position, Side},
};

// ──────────────────────────────────────────────────────────────────────────────
// Risk Configuration — hardcoded constants, not loaded from config/env
// ──────────────────────────────────────────────────────────────────────────────

/// Non-negotiable risk limits. Mirrors the values in CLAUDE.md.
/// These are NOT configurable at runtime to prevent accidental overrides.
#[derive(Debug, Clone)]
pub struct RiskConfig {
    /// Max single-order value as fraction of portfolio. Hardcoded: 5%.
    pub max_position_pct: Decimal,
    /// Daily loss fraction that triggers a full trading halt. Hardcoded: 10%.
    pub max_daily_loss_pct: Decimal,
    /// Peak-to-trough drawdown fraction that triggers a halt. Hardcoded: 20%.
    pub max_drawdown_pct: Decimal,
    /// Minimum signal confidence score to permit a trade. Hardcoded: 0.55.
    pub min_signal_score: f64,
    /// Maximum simultaneous open orders. Hardcoded: 10.
    pub max_open_orders: usize,
    /// Enable ATR-based position sizing when computing order quantities.
    /// When true, callers should use `size_from_atr` before submitting.
    /// check_order itself remains stateless (ADR-003) — it only validates
    /// that the resulting size fits within max_position_pct.
    pub atr_sizing: bool,
    /// Enable trailing stop loss management for open positions.
    /// When true, callers should create a `TrailingStopState` on BUY and
    /// call `state.update(price)` on each new bar to advance the ratchet.
    pub trailing_stop: bool,
}

impl Default for RiskConfig {
    fn default() -> Self {
        Self {
            max_position_pct: dec!(0.05),   // 5%
            max_daily_loss_pct: dec!(0.10), // 10%
            max_drawdown_pct: dec!(0.20),   // 20%
            min_signal_score: 0.55,
            max_open_orders: 10,
            atr_sizing: true,
            trailing_stop: true,
        }
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// ATR Utility Functions
// ──────────────────────────────────────────────────────────────────────────────

/// Compute ATR(period) from aligned high/low/close slices.
///
/// Uses Wilder's smoothed average (simple rolling mean for simplicity/speed).
/// Returns `None` when there are fewer than `period + 1` bars or if any bar
/// contains a non-finite value.
///
/// # Arguments
/// - `highs`  — slice of bar high prices (must be same length as `lows`/`closes`)
/// - `lows`   — slice of bar low prices
/// - `closes` — slice of bar close prices
/// - `period` — ATR lookback period (14 is the Wilder standard)
///
/// # Example
/// ```
/// use quantai_core::risk::atr_from_bars;
/// let atr = atr_from_bars(
///     &[102.0, 103.5, 101.0],
///     &[99.5,  101.0, 99.0],
///     &[101.0, 102.0, 100.0],
///     2,
/// );
/// assert!(atr.is_some());
/// ```
pub fn atr_from_bars(highs: &[f64], lows: &[f64], closes: &[f64], period: usize) -> Option<f64> {
    let n = highs.len();
    if n != lows.len() || n != closes.len() {
        return None;
    }
    if period == 0 || n < period + 1 {
        return None;
    }

    // True Range for each bar (requires previous close)
    let mut tr_values: Vec<f64> = Vec::with_capacity(n - 1);
    for i in 1..n {
        let h = highs[i];
        let l = lows[i];
        let prev_c = closes[i - 1];
        if !h.is_finite() || !l.is_finite() || !prev_c.is_finite() {
            return None;
        }
        let tr = (h - l)
            .max((h - prev_c).abs())
            .max((l - prev_c).abs());
        tr_values.push(tr);
    }

    // Simple rolling mean over the last `period` TR values
    let start = tr_values.len().saturating_sub(period);
    let window = &tr_values[start..];
    if window.len() < period {
        return None;
    }
    let atr = window.iter().sum::<f64>() / period as f64;
    if atr.is_finite() && atr > 0.0 {
        Some(atr)
    } else {
        None
    }
}

/// Compute ATR-based position size (number of units/shares).
///
/// Formula: `qty = (account_equity × risk_pct) / (atr × atr_multiplier)`
/// The result is capped at `max_position_pct × account_equity / price`.
///
/// # Arguments
/// - `account_equity`   — total portfolio value in USD
/// - `risk_pct`         — fraction of equity to risk per ATR unit (e.g. 0.01 = 1%)
/// - `atr`              — ATR value for the symbol (must be > 0)
/// - `atr_multiplier`   — stop width in ATR units (default 2.0 per task spec)
/// - `max_position_pct` — hard cap as fraction of equity (e.g. 0.05 = 5%)
/// - `price`            — current price of the instrument (must be > 0)
///
/// Returns `None` when inputs are invalid (non-positive price/atr, zero equity).
pub fn size_from_atr(
    account_equity: f64,
    risk_pct: f64,
    atr: f64,
    atr_multiplier: f64,
    max_position_pct: f64,
    price: f64,
) -> Option<f64> {
    if account_equity <= 0.0 || atr <= 0.0 || price <= 0.0 || atr_multiplier <= 0.0 {
        return None;
    }
    let risk_dollars = account_equity * risk_pct;
    let raw_qty = risk_dollars / (atr * atr_multiplier);
    let max_qty = (account_equity * max_position_pct) / price;
    let qty = raw_qty.min(max_qty);
    if qty > 0.0 {
        Some(qty)
    } else {
        None
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// Trailing Stop State
// ──────────────────────────────────────────────────────────────────────────────

/// Per-symbol trailing stop state, owned by the caller (e.g. OmsManager).
///
/// The trailing stop is a one-way ratchet for long positions:
///   - `trail_distance` is fixed at entry (1.5 × ATR at the time of BUY)
///   - `high_watermark` ratchets up with each new price high after entry
///   - `current_stop` = `high_watermark` − `trail_distance` (never moves down)
///
/// ADR-003: this struct holds per-position state; `check_order` itself remains
/// stateless. The OmsManager (or position manager) is responsible for creating,
/// updating, and discarding `TrailingStopState` on BUY / SELL events.
///
/// # Example
/// ```
/// use quantai_core::risk::TrailingStopState;
/// use rust_decimal_macros::dec;
///
/// let mut ts = TrailingStopState::new(dec!(100.0), 2.0, 1.5);
/// // Price rises — stop ratchets up
/// ts.update(dec!(110.0));
/// assert!(ts.current_stop() > dec!(100.0));
/// // Price falls back — stop holds
/// let stop_before = ts.current_stop();
/// ts.update(dec!(105.0));
/// assert_eq!(ts.current_stop(), stop_before);
/// ```
#[derive(Debug, Clone)]
pub struct TrailingStopState {
    /// Highest price seen since the position was opened.
    pub high_watermark: Decimal,
    /// Fixed trail distance: ATR_at_entry × multiplier (never changes after entry).
    pub trail_distance: Decimal,
    /// Current stop price. Only moves up (one-way ratchet for longs).
    current_stop: Decimal,
}

impl TrailingStopState {
    /// Create a new trailing stop at entry.
    ///
    /// # Arguments
    /// - `entry_price`  — fill price at which the long was opened
    /// - `atr`          — ATR value at entry (must be > 0; clamped to 0 if negative)
    /// - `atr_mult`     — multiplier for the trail distance (e.g. 1.5)
    pub fn new(entry_price: Decimal, atr: f64, atr_mult: f64) -> Self {
        let atr_pos = atr.max(0.0);
        let trail_dist_f = atr_pos * atr_mult;
        // SAFETY: trail_dist_f is finite and non-negative (product of clamped f64s)
        let trail_distance = Decimal::try_from(trail_dist_f).unwrap_or(Decimal::ZERO);
        let current_stop = entry_price - trail_distance;
        Self {
            high_watermark: entry_price,
            trail_distance,
            current_stop,
        }
    }

    /// Update the trailing stop with the latest price.
    ///
    /// Ratchets `high_watermark` up when `current_price` exceeds it, and
    /// advances `current_stop` = `high_watermark` − `trail_distance`.
    /// The stop **never moves down** — it is a one-way ratchet for longs.
    ///
    /// Returns the updated stop price.
    pub fn update(&mut self, current_price: Decimal) -> Decimal {
        if current_price > self.high_watermark {
            self.high_watermark = current_price;
            let new_stop = self.high_watermark - self.trail_distance;
            // One-way ratchet: only advance if new stop is higher
            if new_stop > self.current_stop {
                self.current_stop = new_stop;
            }
        }
        self.current_stop
    }

    /// Returns the current stop price.
    pub fn current_stop(&self) -> Decimal {
        self.current_stop
    }

    /// Returns `true` when `current_price` has fallen to or below the stop.
    pub fn is_triggered(&self, current_price: Decimal) -> bool {
        current_price <= self.current_stop
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// Risk Engine
// ──────────────────────────────────────────────────────────────────────────────

/// The risk engine. Stateless — all state is passed in per call.
///
/// Statelessness is intentional: the engine has no hidden state that could
/// drift, and it's trivially testable. The caller (order manager) owns the
/// portfolio state and passes it in at check time.
#[derive(Debug, Clone)]
pub struct RiskEngine {
    config: RiskConfig,
}

impl RiskEngine {
    /// Creates a risk engine with the hardcoded default limits.
    /// This is the only constructor you should use in production.
    pub fn new() -> Self {
        Self {
            config: RiskConfig::default(),
        }
    }

    /// Creates a risk engine with custom config. ONLY for testing.
    /// Never call this in production code paths.
    #[cfg(test)]
    pub fn with_config(config: RiskConfig) -> Self {
        Self { config }
    }

    /// Full pre-trade risk check. Must pass BEFORE any order is submitted.
    ///
    /// Checks are ordered from most severe (halt conditions) to least severe
    /// so that the most critical failures are surfaced first.
    ///
    /// # Arguments
    /// - `order`              — The order to validate.
    /// - `positions`          — Current open positions, keyed by symbol.
    /// - `portfolio_value`    — Total portfolio value at current mark-to-market.
    /// - `current_price`      — Latest traded price for the order's symbol.
    /// - `open_order_count`   — Number of currently pending/submitted orders.
    /// - `daily_pnl`          — Today's realized + unrealized P&L (negative = loss).
    /// - `peak_portfolio_value` — Highest portfolio value since inception/reset.
    ///
    /// ADR-003: stateless design intentionally requires all state as arguments.
    #[allow(clippy::too_many_arguments)]
    pub fn check_order(
        &self,
        order: &Order,
        positions: &HashMap<String, Position>,
        portfolio_value: Decimal,
        current_price: Decimal,
        open_order_count: usize,
        daily_pnl: Decimal,
        peak_portfolio_value: Decimal,
    ) -> Result<(), RiskError> {
        // ── 1. HALT CONDITIONS — These override everything ─────────────────────
        self.check_daily_loss_halt(daily_pnl, portfolio_value)?;
        self.check_drawdown_halt(portfolio_value, peak_portfolio_value)?;

        // ── 2. Open orders cap ─────────────────────────────────────────────────
        if open_order_count >= self.config.max_open_orders {
            warn!(
                symbol = %order.symbol,
                open_orders = open_order_count,
                max = self.config.max_open_orders,
                "Open order limit reached"
            );
            return Err(RiskError::TooManyOpenOrders {
                current: open_order_count,
                max: self.config.max_open_orders,
            });
        }

        // ── 3. Signal score ────────────────────────────────────────────────────
        match order.signal_score {
            None => {
                warn!(order_id = %order.client_order_id, "Order missing signal_score");
                return Err(RiskError::MissingSignalScore);
            }
            Some(score) if score < self.config.min_signal_score => {
                warn!(
                    order_id = %order.client_order_id,
                    score,
                    minimum = self.config.min_signal_score,
                    "Signal score below threshold"
                );
                return Err(RiskError::SignalScoreTooLow {
                    score,
                    minimum: self.config.min_signal_score,
                });
            }
            _ => {}
        }

        // ── 4. Stop loss present ───────────────────────────────────────────────
        let stop_loss = order.stop_loss.ok_or_else(|| {
            warn!(order_id = %order.client_order_id, "Order missing stop_loss");
            RiskError::MissingStopLoss {
                order_id: order.client_order_id,
            }
        })?;

        // ── 5. Stop loss direction validity ────────────────────────────────────
        match order.side {
            Side::Buy => {
                if stop_loss >= current_price {
                    return Err(RiskError::InvalidStopLoss {
                        reason: format!(
                            "BUY stop_loss {stop_loss} must be BELOW current price {current_price}"
                        ),
                    });
                }
            }
            Side::Sell => {
                if stop_loss <= current_price {
                    return Err(RiskError::InvalidStopLoss {
                        reason: format!(
                            "SELL stop_loss {stop_loss} must be ABOVE current price {current_price}"
                        ),
                    });
                }
            }
        }

        // ── 6. Position size (new order alone) ────────────────────────────────
        let order_value = order.quantity * current_price;
        let max_allowed = portfolio_value * self.config.max_position_pct;

        if order_value > max_allowed {
            // f64::MAX is a safe sentinel — the trading decision is already made;
            // this value only surfaces in the error message / log.
            let pct = (order_value / portfolio_value * dec!(100))
                .try_into()
                .unwrap_or(f64::MAX);
            warn!(
                symbol = %order.symbol,
                order_value = %order_value,
                max_allowed = %max_allowed,
                "Position size exceeds limit"
            );
            return Err(RiskError::PositionTooLarge {
                requested: order_value,
                maximum: max_allowed,
                portfolio_value,
                pct,
            });
        }

        // ── 7. Total symbol exposure (existing + new) ─────────────────────────
        if let Some(existing) = positions.get(&order.symbol) {
            let existing_exposure = existing.quantity.abs() * current_price;
            let total_exposure = existing_exposure + order_value;
            if total_exposure > max_allowed {
                warn!(
                    symbol = %order.symbol,
                    total_exposure = %total_exposure,
                    max_allowed = %max_allowed,
                    "Total symbol exposure would exceed limit"
                );
                return Err(RiskError::ExposureLimitExceeded {
                    total: total_exposure,
                    maximum: max_allowed,
                });
            }
        }

        info!(
            order_id = %order.client_order_id,
            symbol = %order.symbol,
            side = %order.side,
            quantity = %order.quantity,
            "Risk check PASSED"
        );
        Ok(())
    }

    // ── Private helpers ────────────────────────────────────────────────────────

    fn check_daily_loss_halt(
        &self,
        daily_pnl: Decimal,
        portfolio_value: Decimal,
    ) -> Result<(), RiskError> {
        if daily_pnl >= Decimal::ZERO || portfolio_value == Decimal::ZERO {
            return Ok(());
        }
        let loss_pct = daily_pnl.abs() / portfolio_value;
        if loss_pct >= self.config.max_daily_loss_pct {
            // Sentinel fallbacks: halt decision is already made; values are display-only.
            let loss_f = loss_pct.try_into().unwrap_or(f64::MAX);
            let limit_f = self.config.max_daily_loss_pct.try_into().unwrap_or(0.10);
            warn!(loss_pct = loss_f, limit_pct = limit_f, "DAILY LOSS HALT TRIGGERED");
            return Err(RiskError::DailyLossHalt {
                loss_pct: loss_f * 100.0,
                limit_pct: limit_f * 100.0,
            });
        }
        Ok(())
    }

    fn check_drawdown_halt(
        &self,
        current_value: Decimal,
        peak_value: Decimal,
    ) -> Result<(), RiskError> {
        if peak_value == Decimal::ZERO {
            return Ok(());
        }
        let drawdown = (peak_value - current_value) / peak_value;
        if drawdown >= self.config.max_drawdown_pct {
            // Sentinel fallbacks: halt decision is already made; values are display-only.
            let dd_f = drawdown.try_into().unwrap_or(f64::MAX);
            let limit_f = self.config.max_drawdown_pct.try_into().unwrap_or(0.20);
            warn!(drawdown_pct = dd_f, limit_pct = limit_f, "DRAWDOWN HALT TRIGGERED");
            return Err(RiskError::DrawdownHalt {
                drawdown_pct: dd_f * 100.0,
                limit_pct: limit_f * 100.0,
            });
        }
        Ok(())
    }

    pub fn config(&self) -> &RiskConfig {
        &self.config
    }
}

impl Default for RiskEngine {
    fn default() -> Self {
        Self::new()
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// Tests
// ──────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{Order, Side};
    use rust_decimal_macros::dec;

    fn make_engine() -> RiskEngine {
        RiskEngine::new()
    }

    /// Helper: a valid market buy order that passes all risk checks given a
    /// $100,000 portfolio and $100.00 current price.
    fn valid_buy(symbol: &str, qty: Decimal, stop: Decimal) -> Order {
        Order::new_market(symbol, Side::Buy, qty, stop, 0.75, "test_strategy")
    }

    fn empty_positions() -> HashMap<String, Position> {
        HashMap::new()
    }

    // ── Happy path ─────────────────────────────────────────────────────────────

    #[test]
    fn valid_order_passes() {
        let engine = make_engine();
        // 10 shares at $100 = $1,000 order value
        // Portfolio = $100,000 → 1% < 5% limit ✓
        let order = valid_buy("AAPL", dec!(10), dec!(95));
        let result = engine.check_order(
            &order,
            &empty_positions(),
            dec!(100_000),
            dec!(100),
            0,
            dec!(0),
            dec!(100_000),
        );
        assert!(result.is_ok(), "Expected Ok but got: {result:?}");
    }

    // ── Stop loss checks ───────────────────────────────────────────────────────

    #[test]
    fn rejects_order_missing_stop_loss() {
        let engine = make_engine();
        let mut order = valid_buy("AAPL", dec!(10), dec!(95));
        order.stop_loss = None;
        let result = engine.check_order(
            &order,
            &empty_positions(),
            dec!(100_000),
            dec!(100),
            0,
            dec!(0),
            dec!(100_000),
        );
        assert!(matches!(result, Err(RiskError::MissingStopLoss { .. })));
    }

    #[test]
    fn rejects_buy_stop_loss_above_price() {
        let engine = make_engine();
        // stop_loss = $105, current_price = $100 — invalid for a BUY
        let order = valid_buy("AAPL", dec!(10), dec!(105));
        let result = engine.check_order(
            &order,
            &empty_positions(),
            dec!(100_000),
            dec!(100),
            0,
            dec!(0),
            dec!(100_000),
        );
        assert!(matches!(result, Err(RiskError::InvalidStopLoss { .. })));
    }

    #[test]
    fn rejects_sell_stop_loss_below_price() {
        let engine = make_engine();
        // Short sell at $100 with stop at $95 — stop should be ABOVE for a sell
        let order = Order::new_market("AAPL", Side::Sell, dec!(10), dec!(95), 0.75, "test");
        let result = engine.check_order(
            &order,
            &empty_positions(),
            dec!(100_000),
            dec!(100),
            0,
            dec!(0),
            dec!(100_000),
        );
        assert!(matches!(result, Err(RiskError::InvalidStopLoss { .. })));
    }

    // ── Signal score checks ────────────────────────────────────────────────────

    #[test]
    fn rejects_low_signal_score() {
        let engine = make_engine();
        let mut order = valid_buy("AAPL", dec!(10), dec!(95));
        order.signal_score = Some(0.40); // below 0.55 minimum
        let result = engine.check_order(
            &order,
            &empty_positions(),
            dec!(100_000),
            dec!(100),
            0,
            dec!(0),
            dec!(100_000),
        );
        assert!(matches!(result, Err(RiskError::SignalScoreTooLow { .. })));
    }

    #[test]
    fn rejects_missing_signal_score() {
        let engine = make_engine();
        let mut order = valid_buy("AAPL", dec!(10), dec!(95));
        order.signal_score = None;
        let result = engine.check_order(
            &order,
            &empty_positions(),
            dec!(100_000),
            dec!(100),
            0,
            dec!(0),
            dec!(100_000),
        );
        assert!(matches!(result, Err(RiskError::MissingSignalScore)));
    }

    #[test]
    fn accepts_minimum_signal_score_boundary() {
        let engine = make_engine();
        let mut order = valid_buy("AAPL", dec!(10), dec!(95));
        order.signal_score = Some(0.55); // exactly at boundary — should pass
        let result = engine.check_order(
            &order,
            &empty_positions(),
            dec!(100_000),
            dec!(100),
            0,
            dec!(0),
            dec!(100_000),
        );
        assert!(result.is_ok(), "Boundary score 0.55 should pass");
    }

    // ── Position size checks ───────────────────────────────────────────────────

    #[test]
    fn rejects_oversized_position() {
        let engine = make_engine();
        // 60 shares at $100 = $6,000 = 6% of $100,000 — exceeds 5% limit
        let order = valid_buy("AAPL", dec!(60), dec!(95));
        let result = engine.check_order(
            &order,
            &empty_positions(),
            dec!(100_000),
            dec!(100),
            0,
            dec!(0),
            dec!(100_000),
        );
        assert!(matches!(result, Err(RiskError::PositionTooLarge { .. })));
    }

    #[test]
    fn accepts_max_boundary_position() {
        let engine = make_engine();
        // Exactly 5% of $100,000 at $100/share = 50 shares = $5,000
        let order = valid_buy("AAPL", dec!(50), dec!(95));
        let result = engine.check_order(
            &order,
            &empty_positions(),
            dec!(100_000),
            dec!(100),
            0,
            dec!(0),
            dec!(100_000),
        );
        assert!(result.is_ok(), "Exactly 5% should pass");
    }

    #[test]
    fn rejects_order_exceeding_total_exposure() {
        let engine = make_engine();
        let mut positions = HashMap::new();
        // Already hold 40 shares at $100 = $4,000 = 4% exposure
        let mut existing = Position::from_fill(&crate::types::Fill {
            fill_id: uuid::Uuid::new_v4(),
            client_order_id: uuid::Uuid::new_v4(),
            broker_order_id: None,
            symbol: "AAPL".into(),
            side: Side::Buy,
            filled_quantity: dec!(40),
            fill_price: dec!(100),
            commission: dec!(1),
            timestamp: chrono::Utc::now(),
        });
        existing.quantity = dec!(40);
        existing.average_cost = dec!(100);
        positions.insert("AAPL".into(), existing);

        // Trying to add 20 more shares = $2,000 → total $6,000 = 6% > 5% limit
        let order = valid_buy("AAPL", dec!(20), dec!(95));
        let result = engine.check_order(
            &order,
            &positions,
            dec!(100_000),
            dec!(100),
            0,
            dec!(0),
            dec!(100_000),
        );
        assert!(matches!(result, Err(RiskError::ExposureLimitExceeded { .. })));
    }

    // ── Halt condition checks ──────────────────────────────────────────────────

    #[test]
    fn halts_on_daily_loss_breach() {
        let engine = make_engine();
        // -$11,000 loss on $100,000 portfolio = 11% > 10% limit
        let order = valid_buy("AAPL", dec!(10), dec!(95));
        let result = engine.check_order(
            &order,
            &empty_positions(),
            dec!(100_000),
            dec!(100),
            0,
            dec!(-11_000), // daily_pnl
            dec!(100_000),
        );
        assert!(matches!(result, Err(RiskError::DailyLossHalt { .. })));
    }

    #[test]
    fn halts_on_drawdown_breach() {
        let engine = make_engine();
        // Portfolio at $78,000, peak was $100,000 → 22% drawdown > 20% limit
        let order = valid_buy("AAPL", dec!(10), dec!(95));
        let result = engine.check_order(
            &order,
            &empty_positions(),
            dec!(78_000),  // current
            dec!(100),
            0,
            dec!(0),
            dec!(100_000), // peak
        );
        assert!(matches!(result, Err(RiskError::DrawdownHalt { .. })));
    }

    #[test]
    fn no_halt_at_exactly_limit() {
        let engine = make_engine();
        // Exactly 10% daily loss — should still halt (>= not >)
        let order = valid_buy("AAPL", dec!(10), dec!(95));
        let result = engine.check_order(
            &order,
            &empty_positions(),
            dec!(100_000),
            dec!(100),
            0,
            dec!(-10_000), // exactly 10%
            dec!(100_000),
        );
        assert!(matches!(result, Err(RiskError::DailyLossHalt { .. })));
    }

    #[test]
    fn no_halt_below_limit() {
        let engine = make_engine();
        // 9.9% daily loss — should NOT halt
        let order = valid_buy("AAPL", dec!(10), dec!(95));
        let result = engine.check_order(
            &order,
            &empty_positions(),
            dec!(100_000),
            dec!(100),
            0,
            dec!(-9_900), // 9.9%
            dec!(100_000),
        );
        // May fail for other reasons (size etc.) but NOT DailyLossHalt
        assert!(!matches!(result, Err(RiskError::DailyLossHalt { .. })));
    }

    // ── Open orders cap ────────────────────────────────────────────────────────

    #[test]
    fn rejects_when_too_many_open_orders() {
        let engine = make_engine();
        let order = valid_buy("AAPL", dec!(10), dec!(95));
        let result = engine.check_order(
            &order,
            &empty_positions(),
            dec!(100_000),
            dec!(100),
            10, // already at limit
            dec!(0),
            dec!(100_000),
        );
        assert!(matches!(result, Err(RiskError::TooManyOpenOrders { .. })));
    }

    // ── ATR config flag ────────────────────────────────────────────────────────

    #[test]
    fn atr_sizing_enabled_by_default() {
        assert!(RiskConfig::default().atr_sizing);
    }

    // ── atr_from_bars ─────────────────────────────────────────────────────────

    #[test]
    fn atr_from_bars_basic() {
        // 15 bars of synthetic data: high = close+1, low = close-1 → TR ≈ 2
        let closes: Vec<f64> = (0..15).map(|i| 100.0 + i as f64 * 0.1).collect();
        let highs: Vec<f64>  = closes.iter().map(|c| c + 1.0).collect();
        let lows:  Vec<f64>  = closes.iter().map(|c| c - 1.0).collect();
        let atr = atr_from_bars(&highs, &lows, &closes, 14);
        assert!(atr.is_some(), "Expected valid ATR");
        // With high-low = 2 and prev-close gaps < 2, ATR ≈ 2.0
        let val = atr.unwrap();
        assert!(val > 0.0 && val <= 3.0, "ATR={val} out of expected range");
    }

    #[test]
    fn atr_from_bars_returns_none_insufficient_bars() {
        let closes = vec![100.0, 101.0, 102.0];
        let highs  = closes.iter().map(|c| c + 0.5).collect::<Vec<_>>();
        let lows   = closes.iter().map(|c| c - 0.5).collect::<Vec<_>>();
        // period=14 but only 3 bars → None
        assert!(atr_from_bars(&highs, &lows, &closes, 14).is_none());
    }

    #[test]
    fn atr_from_bars_returns_none_on_zero_period() {
        let closes = vec![100.0; 20];
        let highs  = closes.iter().map(|c| c + 1.0).collect::<Vec<_>>();
        let lows   = closes.iter().map(|c| c - 1.0).collect::<Vec<_>>();
        assert!(atr_from_bars(&highs, &lows, &closes, 0).is_none());
    }

    #[test]
    fn atr_from_bars_returns_none_on_mismatched_lengths() {
        let highs  = vec![101.0, 102.0, 103.0];
        let lows   = vec![99.0, 100.0];  // length mismatch
        let closes = vec![100.0, 101.0, 102.0];
        assert!(atr_from_bars(&highs, &lows, &closes, 2).is_none());
    }

    #[test]
    fn atr_from_bars_rejects_non_finite() {
        let closes = vec![100.0; 16];
        let mut highs: Vec<f64> = closes.iter().map(|c| c + 1.0).collect();
        highs[5] = f64::NAN; // inject NaN
        let lows: Vec<f64> = closes.iter().map(|c| c - 1.0).collect();
        assert!(atr_from_bars(&highs, &lows, &closes, 14).is_none());
    }

    // ── size_from_atr ─────────────────────────────────────────────────────────

    #[test]
    fn size_from_atr_basic() {
        // equity=$100k, risk=1%, ATR=$2, multiplier=2.0 → raw=500 shares
        // 5% cap at $100/share = 50 shares → capped at 50
        let qty = size_from_atr(100_000.0, 0.01, 2.0, 2.0, 0.05, 100.0);
        assert!(qty.is_some());
        let q = qty.unwrap();
        assert!((q - 50.0).abs() < 0.01, "Expected ~50 shares (capped), got {q}");
    }

    #[test]
    fn size_from_atr_uncapped() {
        // equity=$100k, risk=1%, ATR=$50, multiplier=2.0 → raw=10 shares
        // 5% cap at $100/share = 50 shares → 10 < 50, not capped
        let qty = size_from_atr(100_000.0, 0.01, 50.0, 2.0, 0.05, 100.0);
        assert!(qty.is_some());
        let q = qty.unwrap();
        assert!((q - 10.0).abs() < 0.01, "Expected 10 shares, got {q}");
    }

    #[test]
    fn size_from_atr_returns_none_on_invalid_inputs() {
        assert!(size_from_atr(0.0,       0.01, 2.0, 2.0, 0.05, 100.0).is_none(), "zero equity");
        assert!(size_from_atr(100_000.0, 0.01, 0.0, 2.0, 0.05, 100.0).is_none(), "zero atr");
        assert!(size_from_atr(100_000.0, 0.01, 2.0, 2.0, 0.05, 0.0  ).is_none(), "zero price");
        assert!(size_from_atr(100_000.0, 0.01, 2.0, 0.0, 0.05, 100.0).is_none(), "zero multiplier");
    }

    // ── TrailingStopState ──────────────────────────────────────────────────────

    #[test]
    fn trailing_stop_enabled_by_default() {
        assert!(RiskConfig::default().trailing_stop);
    }

    #[test]
    fn trailing_stop_new_sets_correct_initial_stop() {
        // entry=100, atr=2.0, mult=1.5 → trail_distance=3.0, initial_stop=97.0
        let ts = TrailingStopState::new(dec!(100.0), 2.0, 1.5);
        // initial stop = entry − trail_distance = 100 − 3 = 97
        assert_eq!(ts.current_stop(), dec!(97.0));
        assert_eq!(ts.high_watermark, dec!(100.0));
    }

    #[test]
    fn trailing_stop_ratchets_up_on_new_high() {
        let mut ts = TrailingStopState::new(dec!(100.0), 2.0, 1.5);
        // Price rises to 110 → watermark=110, stop=110-3=107
        ts.update(dec!(110.0));
        assert_eq!(ts.high_watermark, dec!(110.0));
        assert_eq!(ts.current_stop(), dec!(107.0));
    }

    #[test]
    fn trailing_stop_one_way_ratchet_does_not_retreat() {
        let mut ts = TrailingStopState::new(dec!(100.0), 2.0, 1.5);
        ts.update(dec!(110.0));  // stop advances to 107
        let stop_after_high = ts.current_stop();

        // Price falls back to 105 (above stop — no trigger)
        ts.update(dec!(105.0));
        // Stop must NOT retreat below 107
        assert_eq!(ts.current_stop(), stop_after_high,
            "Stop retreated when price fell: was {stop_after_high}, now {}",
            ts.current_stop());
    }

    #[test]
    fn trailing_stop_is_triggered_at_stop_price() {
        let ts = TrailingStopState::new(dec!(100.0), 2.0, 1.5);
        // Not triggered while above stop
        assert!(!ts.is_triggered(dec!(100.0)));
        assert!(!ts.is_triggered(dec!(98.0)));  // still above initial stop=97
        // Triggered at or below the stop
        assert!(ts.is_triggered(dec!(97.0)));
        assert!(ts.is_triggered(dec!(90.0)));
    }

    #[test]
    fn trailing_stop_not_triggered_while_above_stop() {
        let mut ts = TrailingStopState::new(dec!(100.0), 2.0, 1.5);
        ts.update(dec!(115.0));  // stop now at 112
        // 113 is above the stop (112) — must NOT trigger
        assert!(!ts.is_triggered(dec!(113.0)));
    }

    #[test]
    fn trailing_stop_zero_atr_falls_back_to_zero_distance() {
        // Edge case: ATR = 0 → trail_distance = 0; stop is AT entry price
        let ts = TrailingStopState::new(dec!(100.0), 0.0, 1.5);
        assert_eq!(ts.trail_distance, Decimal::ZERO);
        // Any price ≤ entry immediately triggers
        assert!(ts.is_triggered(dec!(100.0)));
    }

    #[test]
    fn trailing_stop_update_returns_current_stop() {
        let mut ts = TrailingStopState::new(dec!(100.0), 2.0, 1.5);
        let returned = ts.update(dec!(112.0));
        assert_eq!(returned, ts.current_stop());
    }
}
