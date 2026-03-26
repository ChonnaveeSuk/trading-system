// trading-system/core/src/types.rs
//
// Canonical domain types for the execution engine.
// Rule: Decimal for ALL prices, quantities, P&L. Never f64 for financial values.

use chrono::{DateTime, Utc};
use rust_decimal::prelude::Signed;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

// ──────────────────────────────────────────────────────────────────────────────
// Enumerations
// ──────────────────────────────────────────────────────────────────────────────

/// Trade direction.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum Side {
    Buy,
    Sell,
}

impl Side {
    pub fn is_buy(&self) -> bool {
        matches!(self, Side::Buy)
    }

    /// Returns the sign multiplier: Buy = +1, Sell = -1.
    pub fn sign(&self) -> Decimal {
        match self {
            Side::Buy => Decimal::ONE,
            Side::Sell => Decimal::NEGATIVE_ONE,
        }
    }
}

impl std::fmt::Display for Side {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Side::Buy => write!(f, "BUY"),
            Side::Sell => write!(f, "SELL"),
        }
    }
}

/// Order execution type.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum OrderType {
    Market,
    Limit {
        limit_price: Decimal,
    },
    StopLimit {
        stop_price: Decimal,
        limit_price: Decimal,
    },
}

/// Order lifecycle state machine.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum OrderStatus {
    Pending,
    Submitted,
    PartiallyFilled,
    Filled,
    Cancelled,
    Rejected,
}

impl std::fmt::Display for OrderStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let s = match self {
            OrderStatus::Pending => "PENDING",
            OrderStatus::Submitted => "SUBMITTED",
            OrderStatus::PartiallyFilled => "PARTIALLY_FILLED",
            OrderStatus::Filled => "FILLED",
            OrderStatus::Cancelled => "CANCELLED",
            OrderStatus::Rejected => "REJECTED",
        };
        write!(f, "{s}")
    }
}

/// Order time-in-force policy.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum TimeInForce {
    /// Cancel at end of trading day.
    Day,
    /// Persist until explicitly cancelled.
    GoodTillCancelled,
    /// Fill immediately or cancel remainder.
    ImmediateOrCancel,
    /// Fill completely or cancel entirely.
    FillOrKill,
}

// ──────────────────────────────────────────────────────────────────────────────
// Market Data Types
// ──────────────────────────────────────────────────────────────────────────────

/// OHLCV bar for a symbol at a given timeframe.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Bar {
    pub symbol: String,
    pub timestamp: DateTime<Utc>,
    pub open: Decimal,
    pub high: Decimal,
    pub low: Decimal,
    pub close: Decimal,
    /// Raw traded volume.
    pub volume: Decimal,
    /// Volume-weighted average price for the bar.
    pub vwap: Option<Decimal>,
}

impl Bar {
    /// Midpoint price of the bar.
    pub fn mid(&self) -> Decimal {
        (self.high + self.low) / Decimal::TWO
    }

    /// True range: max(high-low, |high-prev_close|, |low-prev_close|).
    pub fn true_range(&self, prev_close: Decimal) -> Decimal {
        let hl = self.high - self.low;
        let hc = (self.high - prev_close).abs();
        let lc = (self.low - prev_close).abs();
        hl.max(hc).max(lc)
    }
}

/// Level-1 real-time tick (best bid/ask + last trade).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Tick {
    pub symbol: String,
    pub timestamp: DateTime<Utc>,
    pub bid: Decimal,
    pub ask: Decimal,
    pub last: Decimal,
    pub bid_size: Decimal,
    pub ask_size: Decimal,
    pub last_size: Decimal,
}

impl Tick {
    /// Bid-ask midpoint.
    pub fn mid(&self) -> Decimal {
        (self.bid + self.ask) / Decimal::TWO
    }

    /// Absolute spread in price units.
    pub fn spread(&self) -> Decimal {
        self.ask - self.bid
    }

    /// Spread as a fraction of the midpoint (useful for slippage estimates).
    pub fn spread_bps(&self) -> Decimal {
        if self.mid() == Decimal::ZERO {
            return Decimal::ZERO;
        }
        (self.spread() / self.mid()) * rust_decimal_macros::dec!(10_000)
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// Order Management Types
// ──────────────────────────────────────────────────────────────────────────────

/// An order to be submitted to the broker.
///
/// Every order MUST carry:
/// - `stop_loss` (risk engine rejects orders without one)
/// - `signal_score` (minimum 0.55 enforced by risk engine)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Order {
    /// Client-generated unique ID — used throughout the lifecycle.
    pub client_order_id: Uuid,
    pub symbol: String,
    pub side: Side,
    pub order_type: OrderType,
    pub quantity: Decimal,
    pub time_in_force: TimeInForce,
    /// Stop loss price — mandatory. Risk engine rejects orders without one.
    pub stop_loss: Option<Decimal>,
    /// Signal confidence [0.0, 1.0] — must be >= MIN_SIGNAL_SCORE (0.55).
    pub signal_score: Option<f64>,
    /// Identifies which strategy generated this order.
    pub strategy_id: Option<String>,
    pub status: OrderStatus,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

impl Order {
    /// Constructs a market order with all required risk fields populated.
    pub fn new_market(
        symbol: impl Into<String>,
        side: Side,
        quantity: Decimal,
        stop_loss: Decimal,
        signal_score: f64,
        strategy_id: impl Into<String>,
    ) -> Self {
        let now = Utc::now();
        Self {
            client_order_id: Uuid::new_v4(),
            symbol: symbol.into(),
            side,
            order_type: OrderType::Market,
            quantity,
            time_in_force: TimeInForce::Day,
            stop_loss: Some(stop_loss),
            signal_score: Some(signal_score),
            strategy_id: Some(strategy_id.into()),
            status: OrderStatus::Pending,
            created_at: now,
            updated_at: now,
        }
    }

    /// Constructs a limit order with all required risk fields populated.
    pub fn new_limit(
        symbol: impl Into<String>,
        side: Side,
        quantity: Decimal,
        limit_price: Decimal,
        stop_loss: Decimal,
        signal_score: f64,
        strategy_id: impl Into<String>,
    ) -> Self {
        let now = Utc::now();
        Self {
            client_order_id: Uuid::new_v4(),
            symbol: symbol.into(),
            side,
            order_type: OrderType::Limit { limit_price },
            quantity,
            time_in_force: TimeInForce::Day,
            stop_loss: Some(stop_loss),
            signal_score: Some(signal_score),
            strategy_id: Some(strategy_id.into()),
            status: OrderStatus::Pending,
            created_at: now,
            updated_at: now,
        }
    }

    pub fn is_terminal(&self) -> bool {
        matches!(
            self.status,
            OrderStatus::Filled | OrderStatus::Cancelled | OrderStatus::Rejected
        )
    }
}

/// A single execution (partial or full fill) of an order.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Fill {
    pub fill_id: Uuid,
    pub client_order_id: Uuid,
    /// Broker-assigned order ID (may differ from client_order_id).
    pub broker_order_id: Option<String>,
    pub symbol: String,
    pub side: Side,
    pub filled_quantity: Decimal,
    pub fill_price: Decimal,
    pub commission: Decimal,
    pub timestamp: DateTime<Utc>,
}

impl Fill {
    /// Total cash outflow/inflow for this fill (excluding commission).
    pub fn gross_value(&self) -> Decimal {
        self.filled_quantity * self.fill_price
    }

    /// Total cash outflow/inflow including commission.
    pub fn net_value(&self) -> Decimal {
        match self.side {
            Side::Buy => self.gross_value() + self.commission,
            Side::Sell => self.gross_value() - self.commission,
        }
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// Position Type
// ──────────────────────────────────────────────────────────────────────────────

/// Current holding in a single symbol.
///
/// quantity > 0 = long position
/// quantity < 0 = short position (not yet supported — placeholder)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Position {
    pub symbol: String,
    /// Signed quantity: positive = long, negative = short.
    pub quantity: Decimal,
    /// Average entry cost per share.
    pub average_cost: Decimal,
    /// Cumulative realized P&L from closed portions.
    pub realized_pnl: Decimal,
    /// Mark-to-market unrealized P&L at last tick.
    pub unrealized_pnl: Decimal,
    /// Active stop loss price for this position.
    pub stop_loss: Option<Decimal>,
    pub opened_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

impl Position {
    /// Creates a new position from the first fill.
    pub fn from_fill(fill: &Fill) -> Self {
        let signed_qty = fill.side.sign() * fill.filled_quantity;
        Self {
            symbol: fill.symbol.clone(),
            quantity: signed_qty,
            average_cost: fill.fill_price,
            realized_pnl: Decimal::ZERO,
            unrealized_pnl: Decimal::ZERO,
            stop_loss: None,
            opened_at: fill.timestamp,
            updated_at: fill.timestamp,
        }
    }

    /// Current market value at a given price.
    pub fn market_value(&self, current_price: Decimal) -> Decimal {
        self.quantity * current_price
    }

    /// Recalculates unrealized P&L at the current price.
    pub fn update_unrealized_pnl(&mut self, current_price: Decimal) {
        let cost_basis = self.quantity * self.average_cost;
        self.unrealized_pnl = self.quantity * current_price - cost_basis;
        self.updated_at = Utc::now();
    }

    /// Applies a new fill to this position (average-in or partial-close logic).
    pub fn apply_fill(&mut self, fill: &Fill) {
        let fill_signed_qty = fill.side.sign() * fill.filled_quantity;
        let new_qty = self.quantity + fill_signed_qty;

        if self.quantity.signum() == fill_signed_qty.signum() {
            // Adding to position — recalculate average cost
            let total_cost =
                self.quantity * self.average_cost + fill.filled_quantity * fill.fill_price;
            self.average_cost = total_cost / new_qty.abs();
        } else {
            // Reducing or flipping — realize P&L on the closed portion
            let closed_qty = self.quantity.abs().min(fill.filled_quantity);
            let pnl_per_share = match fill.side {
                Side::Sell => fill.fill_price - self.average_cost,
                Side::Buy => self.average_cost - fill.fill_price,
            };
            self.realized_pnl += closed_qty * pnl_per_share - fill.commission;
        }

        self.quantity = new_qty;
        self.updated_at = Utc::now();
    }

    pub fn is_flat(&self) -> bool {
        self.quantity == Decimal::ZERO
    }
}
