// trading-system/core/src/broker/paper.rs
//
// Paper trading simulator — realistic drop-in for IBKR in paper mode.
//
// Models:
//   - Fill latency: uniform random in [base ± jitter] ms (min 10 ms)
//   - Directional slippage: buy fills above reference, sell below
//   - IBKR fixed-rate commission: $0.005/share, $1 min, capped at 0.5% of value
//   - Limit order queue: filled when market price crosses the limit
//   - Stop-limit orders: queued on the limit leg (stop activation is future work)
//
// Usage:
//   let (broker, mut fill_rx) = PaperBroker::new(PaperConfig::default());
//   // Spawn consumer: poll fill_rx and call oms.apply_fill(fill)
//   // Feed prices:   broker.on_price_update("AAPL", dec!(150.00)).await;

use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
};

use async_trait::async_trait;
use chrono::Utc;
use rand::Rng;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use tokio::sync::{mpsc, Mutex};
use tracing::{debug, info, warn};
use uuid::Uuid;

use crate::{
    error::TradingError,
    types::{Fill, Order, OrderType, Side},
};

use super::Broker;

// ──────────────────────────────────────────────────────────────────────────────
// Configuration
// ──────────────────────────────────────────────────────────────────────────────

/// Tunable parameters for the paper trading simulator.
///
/// Defaults mirror realistic US equities market conditions on IBKR:
/// - 100 ms base latency (TWS round-trip on a good connection)
/// - 0.5 bps slippage (tight spread liquid names)
/// - $0.005/share commission (IBKR fixed-rate tier)
#[derive(Debug, Clone)]
pub struct PaperConfig {
    /// Base fill latency in milliseconds.
    pub base_latency_ms: u64,
    /// Uniform jitter +/- this many ms around base. Min effective latency: 10 ms.
    pub latency_jitter_ms: u64,
    /// One-way slippage in basis points (applied directionally per trade).
    pub slippage_bps: Decimal,
    /// Commission per share — mirrors IBKR fixed-rate pricing.
    pub commission_per_share: Decimal,
    /// Minimum commission per order (IBKR: $1.00).
    pub min_commission: Decimal,
    /// Maximum commission as a fraction of gross trade value (IBKR: 0.5%).
    pub max_commission_pct: Decimal,
}

impl Default for PaperConfig {
    fn default() -> Self {
        Self {
            base_latency_ms: 100,
            latency_jitter_ms: 50,
            slippage_bps: dec!(0.5),
            commission_per_share: dec!(0.005),
            min_commission: dec!(1.00),
            max_commission_pct: dec!(0.005),
        }
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// Internal state
// ──────────────────────────────────────────────────────────────────────────────

/// A limit or stop-limit order waiting for the market to cross its trigger.
#[derive(Debug, Clone)]
struct PendingLimitOrder {
    order: Order,
    broker_id: String,
    /// Price at which this order becomes a fill.
    trigger_price: Decimal,
}

// ──────────────────────────────────────────────────────────────────────────────
// PaperBroker
// ──────────────────────────────────────────────────────────────────────────────

/// Paper trading broker.
///
/// Implements the same [`Broker`] trait as the live IBKR broker so the order
/// manager can swap between them with no code changes.
///
/// Fills are delivered asynchronously on `fill_rx` (obtained from `new()`).
/// The caller must poll `fill_rx` and forward each fill to the OMS:
///
/// ```ignore
/// tokio::spawn(async move {
///     while let Some(fill) = fill_rx.recv().await {
///         oms.apply_fill(fill).await.unwrap();
///     }
/// });
/// ```
pub struct PaperBroker {
    config: PaperConfig,
    fill_tx: mpsc::Sender<Fill>,
    /// Limit / stop-limit orders waiting to be triggered.
    pending: Arc<Mutex<HashMap<String, PendingLimitOrder>>>,
    /// Monotonic counter for broker-assigned order IDs.
    counter: Arc<AtomicU64>,
    /// Latest known price per symbol — used for limit monitoring.
    last_prices: Arc<Mutex<HashMap<String, Decimal>>>,
}

impl PaperBroker {
    /// Creates a new paper broker.
    ///
    /// Returns `(broker, fill_rx)`. The caller owns `fill_rx` and must consume it.
    pub fn new(config: PaperConfig) -> (Self, mpsc::Receiver<Fill>) {
        let (fill_tx, fill_rx) = mpsc::channel(512);
        let broker = Self {
            config,
            fill_tx,
            pending: Arc::new(Mutex::new(HashMap::new())),
            counter: Arc::new(AtomicU64::new(1)),
            last_prices: Arc::new(Mutex::new(HashMap::new())),
        };
        (broker, fill_rx)
    }

    /// Feed a real-time price update.
    ///
    /// Triggers any pending limit orders whose conditions are now met.
    /// Call this from the market data tick handler.
    pub async fn on_price_update(&self, symbol: &str, price: Decimal) {
        {
            let mut prices = self.last_prices.lock().await;
            prices.insert(symbol.to_string(), price);
        }

        let triggered: Vec<String> = {
            let pending = self.pending.lock().await;
            pending
                .values()
                .filter(|p| p.order.symbol == symbol && self.is_triggered(p, price))
                .map(|p| p.broker_id.clone())
                .collect()
        };

        for broker_id in triggered {
            let entry = {
                let mut pending = self.pending.lock().await;
                pending.remove(&broker_id)
            };
            if let Some(pending_order) = entry {
                // Fill at reference price (no additional slippage for limit orders —
                // the limit price itself is the worst-case execution price).
                let fill = self.build_fill(&pending_order.order, &broker_id, price);
                if let Err(e) = self.fill_tx.send(fill).await {
                    warn!(broker_id, "Paper broker: failed to send limit fill: {e}");
                } else {
                    info!(
                        broker_id,
                        symbol,
                        price = %price,
                        "Paper broker: limit order triggered and filled"
                    );
                }
            }
        }
    }

    // ── Private helpers ────────────────────────────────────────────────────────

    fn is_triggered(&self, pending: &PendingLimitOrder, current_price: Decimal) -> bool {
        match pending.order.side {
            Side::Buy => current_price <= pending.trigger_price,
            Side::Sell => current_price >= pending.trigger_price,
        }
    }

    fn next_broker_id(&self) -> String {
        let n = self.counter.fetch_add(1, Ordering::Relaxed);
        format!("PAPER-{n:010}")
    }

    /// Applies directional slippage to a reference price.
    ///
    /// - Buy:  fill above reference (adverse)
    /// - Sell: fill below reference (adverse)
    fn apply_slippage(&self, reference_price: Decimal, side: Side) -> Decimal {
        let factor = self.config.slippage_bps / dec!(10_000);
        match side {
            Side::Buy => reference_price * (Decimal::ONE + factor),
            Side::Sell => reference_price * (Decimal::ONE - factor),
        }
    }

    /// IBKR fixed-rate commission formula.
    ///
    /// `commission = max($1.00, qty × $0.005), capped at 0.5% of gross value`
    fn calculate_commission(&self, quantity: Decimal, fill_price: Decimal) -> Decimal {
        let raw = quantity * self.config.commission_per_share;
        let floored = raw.max(self.config.min_commission);
        let cap = quantity * fill_price * self.config.max_commission_pct;
        floored.min(cap)
    }

    fn build_fill(&self, order: &Order, broker_id: &str, reference_price: Decimal) -> Fill {
        let fill_price = self.apply_slippage(reference_price, order.side);
        let commission = self.calculate_commission(order.quantity, fill_price);
        Fill {
            fill_id: Uuid::new_v4(),
            client_order_id: order.client_order_id,
            broker_order_id: Some(broker_id.to_string()),
            symbol: order.symbol.clone(),
            side: order.side,
            filled_quantity: order.quantity,
            fill_price,
            commission,
            timestamp: Utc::now(),
        }
    }

    /// Samples a random fill latency within [base ± jitter], minimum 10 ms.
    fn fill_latency(&self) -> tokio::time::Duration {
        let mut rng = rand::thread_rng();
        let jitter_range = (self.config.latency_jitter_ms * 2 + 1) as i64;
        let jitter = rng.gen_range(0..jitter_range) - self.config.latency_jitter_ms as i64;
        let ms = (self.config.base_latency_ms as i64 + jitter).max(10) as u64;
        tokio::time::Duration::from_millis(ms)
    }

    /// Returns the latest known price for a symbol, falling back to the
    /// order's embedded price (limit / stop) or zero for market orders.
    async fn reference_price_for(&self, order: &Order) -> Decimal {
        let prices = self.last_prices.lock().await;
        if let Some(&p) = prices.get(&order.symbol) {
            return p;
        }
        match &order.order_type {
            OrderType::Limit { limit_price } => *limit_price,
            OrderType::StopLimit { stop_price, .. } => *stop_price,
            OrderType::Market => Decimal::ZERO, // Caller should always seed a price first
        }
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// Broker trait implementation
// ──────────────────────────────────────────────────────────────────────────────

#[async_trait]
impl Broker for PaperBroker {
    async fn submit_order(&self, order: &Order) -> Result<String, TradingError> {
        let broker_id = self.next_broker_id();

        info!(
            broker_id = %broker_id,
            symbol    = %order.symbol,
            side      = %order.side,
            qty       = %order.quantity,
            order_type = ?order.order_type,
            "Paper broker: order received"
        );

        match &order.order_type {
            // ── Market order: fill after simulated latency ─────────────────────
            OrderType::Market => {
                let reference = self.reference_price_for(order).await;
                let fill = self.build_fill(order, &broker_id, reference);
                let tx = self.fill_tx.clone();
                let latency = self.fill_latency();
                let bid = broker_id.clone();

                tokio::spawn(async move {
                    tokio::time::sleep(latency).await;
                    debug!(broker_id = %bid, latency_ms = latency.as_millis(), "Paper: market fill");
                    if let Err(e) = tx.send(fill).await {
                        warn!("Paper broker: fill channel closed: {e}");
                    }
                });
            }

            // ── Limit order: fill immediately if favorable, else queue ─────────
            OrderType::Limit { limit_price } => {
                let (ref_price, immediate) = {
                    let prices = self.last_prices.lock().await;
                    let p = prices.get(&order.symbol).copied().unwrap_or(*limit_price);
                    let immediate = match order.side {
                        Side::Buy => p <= *limit_price,
                        Side::Sell => p >= *limit_price,
                    };
                    (p, immediate)
                };

                if immediate {
                    let fill = self.build_fill(order, &broker_id, ref_price);
                    let tx = self.fill_tx.clone();
                    let latency = self.fill_latency();
                    let bid = broker_id.clone();
                    tokio::spawn(async move {
                        tokio::time::sleep(latency).await;
                        debug!(broker_id = %bid, "Paper: limit fill (immediate)");
                        if let Err(e) = tx.send(fill).await {
                            warn!("Paper broker: fill channel closed: {e}");
                        }
                    });
                } else {
                    debug!(broker_id = %broker_id, limit = %limit_price, "Paper: limit order queued");
                    let mut pending = self.pending.lock().await;
                    pending.insert(
                        broker_id.clone(),
                        PendingLimitOrder {
                            order: order.clone(),
                            broker_id: broker_id.clone(),
                            trigger_price: *limit_price,
                        },
                    );
                }
            }

            // ── Stop-limit: queue on the limit leg (stop activation: future work)
            OrderType::StopLimit { limit_price, stop_price } => {
                debug!(
                    broker_id = %broker_id,
                    stop  = %stop_price,
                    limit = %limit_price,
                    "Paper: stop-limit order queued (stop activation not yet modeled)"
                );
                let mut pending = self.pending.lock().await;
                pending.insert(
                    broker_id.clone(),
                    PendingLimitOrder {
                        order: order.clone(),
                        broker_id: broker_id.clone(),
                        trigger_price: *limit_price,
                    },
                );
            }
        }

        Ok(broker_id)
    }

    async fn cancel_order(&self, broker_order_id: &str) -> Result<(), TradingError> {
        let mut pending = self.pending.lock().await;
        if pending.remove(broker_order_id).is_some() {
            info!(broker_id = broker_order_id, "Paper broker: order cancelled");
            Ok(())
        } else {
            Err(TradingError::Broker(format!(
                "Order '{broker_order_id}' not found — already filled or never existed"
            )))
        }
    }

    async fn health_check(&self) -> Result<(), TradingError> {
        // Paper broker is always healthy — no external dependencies.
        Ok(())
    }

    /// Update the price cache via the Broker trait (delegates to on_price_update).
    async fn on_price_update(&self, symbol: &str, price: rust_decimal::Decimal) {
        PaperBroker::on_price_update(self, symbol, price).await;
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
    use tokio::time::{timeout, Duration};

    fn make_buy(qty: Decimal, stop: Decimal) -> Order {
        Order::new_market("AAPL", Side::Buy, qty, stop, 0.75, "test")
    }

    fn make_buy_limit(qty: Decimal, limit: Decimal, stop: Decimal) -> Order {
        Order::new_limit("AAPL", Side::Buy, qty, limit, stop, 0.75, "test")
    }

    /// Market order must fill within (base_latency + jitter + buffer) ms.
    #[tokio::test]
    async fn market_order_fills() {
        let config = PaperConfig {
            base_latency_ms: 50,
            latency_jitter_ms: 20,
            ..Default::default()
        };
        let (broker, mut fill_rx) = PaperBroker::new(config);
        broker.on_price_update("AAPL", dec!(150.00)).await;

        let order = make_buy(dec!(10), dec!(140));
        broker.submit_order(&order).await.unwrap();

        let fill = timeout(Duration::from_millis(500), fill_rx.recv())
            .await
            .expect("timeout")
            .expect("channel closed");

        assert_eq!(fill.client_order_id, order.client_order_id);
        assert_eq!(fill.filled_quantity, dec!(10));
        // Fill price must be above reference (buy slippage)
        assert!(fill.fill_price > dec!(150.00));
        // Commission: max($1, 10 × $0.005) = max($1, $0.05) = $1.00
        assert_eq!(fill.commission, dec!(1.00));
    }

    /// Limit order below market should fill immediately.
    #[tokio::test]
    async fn limit_order_fills_immediately_when_favorable() {
        let (broker, mut fill_rx) = PaperBroker::new(PaperConfig {
            base_latency_ms: 20,
            latency_jitter_ms: 5,
            ..Default::default()
        });
        broker.on_price_update("AAPL", dec!(145.00)).await;

        // Buy limit at $150 with current price $145 — should fill immediately
        let order = make_buy_limit(dec!(5), dec!(150.00), dec!(135));
        broker.submit_order(&order).await.unwrap();

        let fill = timeout(Duration::from_millis(300), fill_rx.recv())
            .await
            .expect("timeout")
            .expect("channel closed");
        assert_eq!(fill.client_order_id, order.client_order_id);
    }

    /// Limit order above market is queued, fills when price drops.
    #[tokio::test]
    async fn limit_order_queued_then_triggered() {
        let (broker, mut fill_rx) = PaperBroker::new(PaperConfig {
            base_latency_ms: 10,
            latency_jitter_ms: 5,
            ..Default::default()
        });
        broker.on_price_update("AAPL", dec!(155.00)).await;

        // Buy limit at $150 — market is at $155, so queued
        let order = make_buy_limit(dec!(10), dec!(150.00), dec!(140));
        let broker_id = broker.submit_order(&order).await.unwrap();
        assert!(broker_id.starts_with("PAPER-"));

        // No fill yet
        assert!(fill_rx.try_recv().is_err());

        // Price drops below limit
        broker.on_price_update("AAPL", dec!(149.50)).await;

        let fill = timeout(Duration::from_millis(300), fill_rx.recv())
            .await
            .expect("timeout")
            .expect("channel closed");
        assert_eq!(fill.client_order_id, order.client_order_id);
    }

    /// Cancelling a queued limit order removes it from the pending map.
    #[tokio::test]
    async fn cancel_limit_order() {
        let (broker, mut fill_rx) = PaperBroker::new(PaperConfig::default());
        broker.on_price_update("AAPL", dec!(155.00)).await;

        let order = make_buy_limit(dec!(10), dec!(150.00), dec!(140));
        let broker_id = broker.submit_order(&order).await.unwrap();

        broker.cancel_order(&broker_id).await.unwrap();

        // Price drops — no fill should arrive because we cancelled
        broker.on_price_update("AAPL", dec!(149.00)).await;

        assert!(
            timeout(Duration::from_millis(200), fill_rx.recv())
                .await
                .is_err(),
            "Cancelled order must not produce a fill"
        );
    }

    #[tokio::test]
    async fn health_check_always_ok() {
        let (broker, _) = PaperBroker::new(PaperConfig::default());
        assert!(broker.health_check().await.is_ok());
    }

    #[test]
    fn commission_calculation() {
        let (broker, _) = PaperBroker::new(PaperConfig::default());
        // 10 shares at $100 → raw = $0.05, floored to $1.00, cap = $0.50 → $0.50
        // Wait: cap = 10 * 100 * 0.005 = $5.00, so floored=$1.00 wins → $1.00
        assert_eq!(broker.calculate_commission(dec!(10), dec!(100)), dec!(1.00));

        // 1000 shares at $5 → raw = $5.00, cap = 1000 * 5 * 0.005 = $25 → $5.00
        assert_eq!(broker.calculate_commission(dec!(1000), dec!(5)), dec!(5.00));
    }

    #[test]
    fn slippage_is_directional() {
        let (broker, _) = PaperBroker::new(PaperConfig {
            slippage_bps: dec!(1.0),
            ..Default::default()
        });
        let buy_price = broker.apply_slippage(dec!(100), Side::Buy);
        let sell_price = broker.apply_slippage(dec!(100), Side::Sell);
        assert!(buy_price > dec!(100));
        assert!(sell_price < dec!(100));
    }
}
