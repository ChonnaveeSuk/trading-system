// trading-system/core/src/market_data/feed.rs
//
// Redis-backed market data feed.
//
// Architecture:
//   - publish_tick():    writes to Redis hash (latest tick) + PUBLISH to channel
//   - subscribe():       spawns a tokio task that SUBSCRIBEs to Redis pub/sub
//                        and calls the user callback on each tick
//   - get_latest_tick(): reads the latest tick snapshot from Redis
//
// Redis key schema:
//   tick:{SYMBOL}            → JSON-encoded Tick (latest snapshot)
//   ticks:{SYMBOL}           → pub/sub channel for real-time delivery
//
// In Phase 1 the Alpaca data source is stubbed; ticks are pushed to Redis
// externally (e.g., from the Python strategy layer via redis-py, or from a
// separate Alpaca-to-Redis bridge process). The feed layer is source-agnostic.

use std::sync::Arc;

use futures::StreamExt;
use redis::{aio::ConnectionManager, AsyncCommands, Client};
use serde_json;
use tokio::sync::oneshot;
use tracing::{debug, error, info, warn};

use crate::{
    error::TradingError,
    types::Tick,
};

// ──────────────────────────────────────────────────────────────────────────────
// Configuration
// ──────────────────────────────────────────────────────────────────────────────

/// Feed configuration.
#[derive(Debug, Clone)]
pub struct FeedConfig {
    /// Redis URL — e.g. "redis://127.0.0.1:6379"
    pub redis_url: String,
    /// Symbols to subscribe to on start-up.
    pub symbols: Vec<String>,
    /// TTL for latest-tick snapshots in Redis (seconds). Default: 86400 (1 day).
    pub tick_ttl_secs: u64,
}

impl FeedConfig {
    pub fn from_env() -> Result<Self, TradingError> {
        let redis_url = std::env::var("REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379".into());
        Ok(Self {
            redis_url,
            symbols: vec![],
            tick_ttl_secs: 86_400,
        })
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// Subscription handle
// ──────────────────────────────────────────────────────────────────────────────

/// Returned by [`RedisFeed::subscribe`]. Drop to cancel the subscription.
pub struct SubscriptionHandle {
    symbol: String,
    cancel_tx: Option<oneshot::Sender<()>>,
}

impl SubscriptionHandle {
    pub fn symbol(&self) -> &str {
        &self.symbol
    }

    /// Explicitly cancel the subscription without dropping the handle.
    pub fn cancel(mut self) {
        if let Some(tx) = self.cancel_tx.take() {
            let _ = tx.send(());
        }
    }
}

impl Drop for SubscriptionHandle {
    fn drop(&mut self) {
        if let Some(tx) = self.cancel_tx.take() {
            let _ = tx.send(());
        }
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// RedisFeed
// ──────────────────────────────────────────────────────────────────────────────

/// Redis-backed market data feed.
///
/// Provides tick storage (latest snapshot) and real-time pub/sub delivery.
#[derive(Clone)]
pub struct RedisFeed {
    config: Arc<FeedConfig>,
    conn: ConnectionManager,
    /// Separate client for creating pub/sub connections (can't reuse pool for subscribe).
    client: Client,
}

impl RedisFeed {
    /// Create a new feed and verify Redis connectivity.
    pub async fn connect(config: FeedConfig) -> Result<Self, TradingError> {
        let client = Client::open(config.redis_url.as_str())?;
        let conn = ConnectionManager::new(client.clone()).await?;

        info!(url = %config.redis_url, "Redis feed connected");

        Ok(Self {
            config: Arc::new(config),
            conn,
            client,
        })
    }

    // ── Publisher API (called from Alpaca data bridge / paper broker) ──────────

    /// Publish a tick to Redis.
    ///
    /// Stores the latest snapshot at `tick:{symbol}` and publishes to
    /// `ticks:{symbol}` for real-time subscribers.
    pub async fn publish_tick(&self, tick: &Tick) -> Result<(), TradingError> {
        let json = serde_json::to_string(tick)?;
        let snapshot_key = tick_key(&tick.symbol);
        let channel = tick_channel(&tick.symbol);

        let mut conn = self.conn.clone();

        // SET tick:AAPL <json> EX 86400
        redis::cmd("SET")
            .arg(&snapshot_key)
            .arg(&json)
            .arg("EX")
            .arg(self.config.tick_ttl_secs)
            .exec_async(&mut conn)
            .await?;

        // PUBLISH ticks:AAPL <json>
        let receivers: i64 = conn.publish(&channel, &json).await?;

        debug!(
            symbol = %tick.symbol,
            last   = %tick.last,
            receivers,
            "Feed: tick published"
        );
        Ok(())
    }

    // ── Subscriber API ────────────────────────────────────────────────────────

    /// Fetch the latest tick snapshot for a symbol from Redis.
    ///
    /// Returns `None` if no tick has been published for this symbol yet.
    pub async fn get_latest_tick(&self, symbol: &str) -> Result<Option<Tick>, TradingError> {
        let mut conn = self.conn.clone();
        let json: Option<String> = conn.get(tick_key(symbol)).await?;

        match json {
            None => Ok(None),
            Some(s) => {
                let tick: Tick = serde_json::from_str(&s)?;
                Ok(Some(tick))
            }
        }
    }

    /// Subscribe to real-time ticks for a symbol.
    ///
    /// Spawns a dedicated tokio task that listens on the Redis pub/sub channel
    /// `ticks:{symbol}` and calls `on_tick` for each message received.
    ///
    /// Drop (or explicitly call `.cancel()` on) the returned handle to stop.
    pub async fn subscribe(
        &self,
        symbol: impl Into<String>,
        on_tick: impl Fn(Tick) + Send + 'static,
    ) -> Result<SubscriptionHandle, TradingError> {
        let symbol: String = symbol.into();
        let channel = tick_channel(&symbol);
        let (cancel_tx, mut cancel_rx) = oneshot::channel::<()>();

        // Open a dedicated pub/sub connection (can't multiplex SET/GET on this)
        let mut pubsub_conn = self.client.get_async_pubsub().await?;
        pubsub_conn.subscribe(&channel).await?;

        let sym_clone = symbol.clone();
        tokio::spawn(async move {
            info!(symbol = %sym_clone, channel = %channel, "Feed: subscription started");

            // Pin the stream so it can be polled inside select!
            let stream = pubsub_conn.on_message();
            tokio::pin!(stream);

            loop {
                tokio::select! {
                    biased;

                    _ = &mut cancel_rx => {
                        info!(symbol = %sym_clone, "Feed: subscription cancelled");
                        break;
                    }

                    msg = stream.next() => {
                        match msg {
                            None => {
                                warn!(symbol = %sym_clone, "Feed: pub/sub connection closed");
                                break;
                            }
                            Some(m) => {
                                let payload: redis::RedisResult<String> = m.get_payload();
                                match payload {
                                    Err(e) => error!(symbol = %sym_clone, "Feed: bad payload: {e}"),
                                    Ok(json) => match serde_json::from_str::<Tick>(&json) {
                                        Err(e) => error!(symbol = %sym_clone, "Feed: parse error: {e}"),
                                        Ok(tick) => on_tick(tick),
                                    },
                                }
                            }
                        }
                    }
                }
            }
        });

        Ok(SubscriptionHandle {
            symbol,
            cancel_tx: Some(cancel_tx),
        })
    }

    // ── Utility ───────────────────────────────────────────────────────────────

    /// Ping Redis to verify connectivity.
    pub async fn ping(&self) -> Result<(), TradingError> {
        let mut conn = self.conn.clone();
        redis::cmd("PING").exec_async(&mut conn).await?;
        Ok(())
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// Key helpers
// ──────────────────────────────────────────────────────────────────────────────

fn tick_key(symbol: &str) -> String {
    format!("tick:{symbol}")
}

fn tick_channel(symbol: &str) -> String {
    format!("ticks:{symbol}")
}

// ──────────────────────────────────────────────────────────────────────────────
// Tests
// ──────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Utc;
    use rust_decimal_macros::dec;

    fn make_tick(symbol: &str) -> Tick {
        Tick {
            symbol: symbol.to_string(),
            timestamp: Utc::now(),
            bid: dec!(149.95),
            ask: dec!(150.05),
            last: dec!(150.00),
            bid_size: dec!(100),
            ask_size: dec!(200),
            last_size: dec!(50),
        }
    }

    async fn try_connect() -> Option<RedisFeed> {
        let config = FeedConfig {
            redis_url: "redis://127.0.0.1:6379".into(),
            symbols: vec![],
            tick_ttl_secs: 60,
        };
        RedisFeed::connect(config).await.ok()
    }

    #[tokio::test]
    async fn publish_and_retrieve() {
        let Some(feed) = try_connect().await else {
            eprintln!("Redis not available — skipping integration test");
            return;
        };
        let tick = make_tick("AAPL");
        feed.publish_tick(&tick).await.unwrap();

        let retrieved = feed.get_latest_tick("AAPL").await.unwrap().unwrap();
        assert_eq!(retrieved.symbol, "AAPL");
        assert_eq!(retrieved.last, dec!(150.00));
    }

    #[tokio::test]
    async fn get_nonexistent_tick_returns_none() {
        let Some(feed) = try_connect().await else {
            return;
        };
        // Use a unique symbol unlikely to exist
        let result = feed.get_latest_tick("ZZZZ_TEST_9999").await.unwrap();
        assert!(result.is_none());
    }
}
