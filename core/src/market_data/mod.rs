// trading-system/core/src/market_data/mod.rs
//
// Real-time market data feed.
// Phase 1 will implement:
//   - feed.rs → WebSocket tick feed → Redis hot cache
//
// Architecture: ticks arrive on WebSocket, get written to Redis,
// and are optionally published to GCP Pub/Sub (fire-and-forget).

use crate::{error::TradingError, types::Tick};

/// Subscription handle returned by subscribe().
/// Drop to unsubscribe.
pub struct SubscriptionHandle {
    // TODO Phase 1: tokio::sync::oneshot::Sender<()> for cancellation
    _private: (),
}

/// Real-time data source. Phase 1 implements the IBKR feed.
pub trait MarketDataFeed: Send + Sync {
    /// Subscribe to ticks for a symbol. Returns handle to unsubscribe.
    fn subscribe(
        &self,
        symbol: &str,
        on_tick: impl Fn(Tick) + Send + 'static,
    ) -> Result<SubscriptionHandle, TradingError>;

    /// Unsubscribe all symbols and shut down.
    fn shutdown(&self) -> Result<(), TradingError>;
}

// TODO Phase 1: pub mod feed;
