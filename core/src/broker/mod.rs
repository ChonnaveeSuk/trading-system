// trading-system/core/src/broker/mod.rs
//
// Broker abstraction layer.
//   - paper.rs  → Local paper trading simulator (slippage + latency)
//   - alpaca.rs → Alpaca Markets REST broker (paper: paper-api.alpaca.markets)

use async_trait::async_trait;

use crate::{
    error::TradingError,
    types::Order,
};

/// Broker trait — paper simulator and Alpaca both implement this.
/// The order manager holds a Box<dyn Broker> and never cares which is active.
#[async_trait]
pub trait Broker: Send + Sync {
    /// Submit an order to the broker. Returns broker-assigned order ID.
    async fn submit_order(&self, order: &Order) -> Result<String, TradingError>;

    /// Cancel a previously submitted order.
    async fn cancel_order(&self, broker_order_id: &str) -> Result<(), TradingError>;

    /// Check broker connectivity.
    async fn health_check(&self) -> Result<(), TradingError>;

    /// Update the latest known market price for a symbol.
    ///
    /// PaperBroker uses this to determine the fill price for market orders.
    /// The default no-op is correct for live brokers (Alpaca), which fill
    /// at the real market price regardless.
    async fn on_price_update(&self, _symbol: &str, _price: rust_decimal::Decimal) {}
}

pub mod alpaca;
pub mod paper;
