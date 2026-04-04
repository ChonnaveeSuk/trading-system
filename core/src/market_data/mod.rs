// trading-system/core/src/market_data/mod.rs
//
// Real-time market data feed.
//
// Phase 1 implementation: feed.rs — Redis-backed tick publisher + subscriber.
//
// Architecture: ticks arrive from Alpaca WebSocket (Phase 4) or a bridge process,
// get written to Redis, and are optionally published to GCP Pub/Sub
// (fire-and-forget, per ADR-002).

pub mod feed;

// Re-export the concrete feed type so callers don't need to reach into the submodule.
pub use feed::{FeedConfig, RedisFeed, SubscriptionHandle};

use crate::{error::TradingError, types::Tick};

/// Real-time data source trait.
///
/// Phase 1 uses `RedisFeed` directly. This trait exists for future abstraction
/// (e.g., swapping Redis for a direct WebSocket feed in Phase 2).
pub trait MarketDataFeed: Send + Sync {
    /// Subscribe to ticks for a symbol. Returns handle to unsubscribe.
    ///
    /// The `on_tick` callback is called from a spawned task — it must be
    /// `Send + 'static`. Heavy work should be offloaded via a channel.
    fn subscribe(
        &self,
        symbol: &str,
        on_tick: Box<dyn Fn(Tick) + Send + 'static>,
    ) -> Result<SubscriptionHandle, TradingError>;

    /// Unsubscribe all symbols and shut down.
    fn shutdown(&self) -> Result<(), TradingError>;
}
