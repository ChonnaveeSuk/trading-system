// trading-system/core/src/order/mod.rs
//
// Order Management System (OMS).
// Phase 1 will implement:
//   - manager.rs → Full order lifecycle: pending → submitted → filled + DB log
//
// Flow: signal → risk check → submit → track → fill → update position → log

use std::collections::HashMap;

use rust_decimal::Decimal;
use uuid::Uuid;

use crate::{error::TradingError, types::{Fill, Order, OrderStatus, Position}};

/// Summary of the current portfolio state. Passed into the risk engine.
#[derive(Debug, Clone)]
pub struct PortfolioSnapshot {
    /// Total portfolio value (cash + positions at current mark).
    pub total_value: Decimal,
    /// Highest portfolio value since last reset (for drawdown calculation).
    pub peak_value: Decimal,
    /// Today's P&L: realized + unrealized (negative = loss).
    pub daily_pnl: Decimal,
    /// All open positions keyed by symbol.
    pub positions: HashMap<String, Position>,
    /// Number of currently pending or submitted orders.
    pub open_order_count: usize,
}

/// Order manager interface. Phase 1 implements the concrete version backed
/// by PostgreSQL + Redis.
pub trait OrderManager: Send + Sync {
    /// Submit an order through the full pipeline:
    /// risk check → broker submit → track in DB → return assigned order ID.
    fn submit(
        &self,
        order: Order,
        current_price: Decimal,
        snapshot: &PortfolioSnapshot,
    ) -> impl std::future::Future<Output = Result<Uuid, TradingError>> + Send;

    /// Apply an incoming fill: update order status, update position, log to DB.
    fn apply_fill(
        &self,
        fill: Fill,
    ) -> impl std::future::Future<Output = Result<(), TradingError>> + Send;

    /// Cancel a submitted order.
    fn cancel(
        &self,
        client_order_id: Uuid,
    ) -> impl std::future::Future<Output = Result<(), TradingError>> + Send;

    /// Get current status of an order.
    fn get_order_status(
        &self,
        client_order_id: Uuid,
    ) -> impl std::future::Future<Output = Result<OrderStatus, TradingError>> + Send;
}

pub mod manager;
