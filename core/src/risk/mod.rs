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
}

impl Default for RiskConfig {
    fn default() -> Self {
        Self {
            max_position_pct: dec!(0.05),   // 5%
            max_daily_loss_pct: dec!(0.10), // 10%
            max_drawdown_pct: dec!(0.20),   // 20%
            min_signal_score: 0.55,
            max_open_orders: 10,
        }
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
}
