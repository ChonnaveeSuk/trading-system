// trading-system/core/src/broker/mod.rs
//
// Broker abstraction layer.
// Phase 1 will implement:
//   - paper.rs  → Paper trading simulator (slippage + latency)
//   - ibkr.rs   → Interactive Brokers TWS connection (port 7497)

use async_trait::async_trait;

use crate::{
    error::TradingError,
    types::Order,
};

/// Broker trait — paper simulator and IBKR both implement this.
/// The order manager holds a Box<dyn Broker> and never cares which is active.
#[async_trait]
pub trait Broker: Send + Sync {
    /// Submit an order to the broker. Returns broker-assigned order ID.
    async fn submit_order(&self, order: &Order) -> Result<String, TradingError>;

    /// Cancel a previously submitted order.
    async fn cancel_order(&self, broker_order_id: &str) -> Result<(), TradingError>;

    /// Check broker connectivity.
    async fn health_check(&self) -> Result<(), TradingError>;
}

pub mod ibkr;
pub mod paper;
