// trading-system/core/src/order/manager.rs
//
// Order Management System (OMS) — full lifecycle implementation.
//
// Pipeline: signal → risk check → DB insert → broker submit → fill → position update → DB log
//
// Thread model:
//   OmsManager is cheaply Clone-able (all state behind Arc).
//   Call clone() to hand a copy to a tokio::spawn task.
//
// PostgreSQL tables written:
//   orders     — one row per order, status tracked from PENDING → FILLED / CANCELLED
//   fills      — one row per execution event
//   positions  — upserted on each fill; retained at quantity=0 for audit
//   risk_events — every risk rejection + halt is logged immutably

use std::{
    collections::HashMap,
    sync::Arc,
};

use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde_json;
use sqlx::PgPool;
use tokio::sync::RwLock;
use tracing::{error, info, warn};
use uuid::Uuid;

use crate::{
    broker::Broker,
    error::{RiskError, TradingError},
    risk::RiskEngine,
    types::{Fill, Order, OrderStatus, OrderType, Position, Side},
};

use super::PortfolioSnapshot;

// ──────────────────────────────────────────────────────────────────────────────
// OMS Manager
// ──────────────────────────────────────────────────────────────────────────────

/// The Order Management System.
///
/// Owns the order lifecycle from risk check through fill application.
/// All state is protected by `RwLock`; use `.clone()` for concurrency.
#[derive(Clone)]
pub struct OmsManager {
    pool: PgPool,
    broker: Arc<dyn Broker>,
    risk: Arc<RiskEngine>,
    /// In-flight and recently settled orders, keyed by client_order_id.
    orders: Arc<RwLock<HashMap<Uuid, Order>>>,
    /// Current open positions, keyed by symbol.
    positions: Arc<RwLock<HashMap<String, Position>>>,
    /// Portfolio snapshot updated on every fill and used for risk checks.
    portfolio: Arc<RwLock<PortfolioSnapshot>>,
}

impl OmsManager {
    // ── Constructor ───────────────────────────────────────────────────────────

    /// Create the OMS and load current state from PostgreSQL.
    ///
    /// Reads open orders and positions from the DB so a restart picks up
    /// where trading left off.
    pub async fn new(
        pool: PgPool,
        broker: Arc<dyn Broker>,
        starting_portfolio_value: Decimal,
    ) -> Result<Self, TradingError> {
        let risk = Arc::new(RiskEngine::new());

        let oms = Self {
            pool,
            broker,
            risk,
            orders: Arc::new(RwLock::new(HashMap::new())),
            positions: Arc::new(RwLock::new(HashMap::new())),
            portfolio: Arc::new(RwLock::new(PortfolioSnapshot {
                total_value: starting_portfolio_value,
                peak_value: starting_portfolio_value,
                daily_pnl: dec!(0),
                positions: HashMap::new(),
                open_order_count: 0,
            })),
        };

        oms.reload_from_db().await?;
        Ok(oms)
    }

    /// Load active orders and positions from DB on startup.
    async fn reload_from_db(&self) -> Result<(), TradingError> {
        // Load open positions
        let rows = sqlx::query!(
            r#"SELECT symbol, quantity, average_cost, realized_pnl, unrealized_pnl,
                      stop_loss, opened_at, updated_at
               FROM positions WHERE quantity != 0"#
        )
        .fetch_all(&self.pool)
        .await?;

        let mut positions = self.positions.write().await;
        for row in rows {
            positions.insert(
                row.symbol.clone(),
                Position {
                    symbol: row.symbol,
                    quantity: row.quantity,
                    average_cost: row.average_cost,
                    realized_pnl: row.realized_pnl,
                    unrealized_pnl: row.unrealized_pnl,
                    stop_loss: row.stop_loss,
                    opened_at: row.opened_at,
                    updated_at: row.updated_at,
                },
            );
        }

        // Count open orders for the snapshot
        let open_count: i64 = sqlx::query_scalar(
            "SELECT COUNT(*)::bigint FROM orders WHERE status IN ('PENDING', 'SUBMITTED')"
        )
        .fetch_one(&self.pool)
        .await
        .unwrap_or(Some(0))
        .unwrap_or(0);

        let mut portfolio = self.portfolio.write().await;
        portfolio.open_order_count = open_count as usize;
        portfolio.positions = positions.clone();

        info!(
            positions = positions.len(),
            open_orders = open_count,
            "OMS: state restored from DB"
        );
        Ok(())
    }

    // ── Submit ────────────────────────────────────────────────────────────────

    /// Submit an order through the full pipeline:
    /// risk check → DB insert → broker submit → status update.
    ///
    /// Returns the `client_order_id` on success, or a `TradingError` if
    /// the risk engine rejects or the broker returns an error.
    pub async fn submit(
        &self,
        order: Order,
        current_price: Decimal,
    ) -> Result<Uuid, TradingError> {
        let client_id = order.client_order_id;

        // ── 1. Risk check ──────────────────────────────────────────────────────
        {
            let portfolio = self.portfolio.read().await;
            let positions = self.positions.read().await;
            match self.risk.check_order(
                &order,
                &positions,
                portfolio.total_value,
                current_price,
                portfolio.open_order_count,
                portfolio.daily_pnl,
                portfolio.peak_value,
            ) {
                Ok(()) => {}
                Err(risk_err) => {
                    let (severity, event_type) = risk_event_meta(&risk_err);
                    warn!(
                        order_id = %client_id,
                        symbol   = %order.symbol,
                        reason   = %risk_err,
                        "OMS: risk check FAILED — order rejected"
                    );
                    let details = serde_json::json!({ "reason": risk_err.to_string() });
                    // Drop read locks before the await
                    drop(portfolio);
                    drop(positions);
                    self.log_risk_event(
                        event_type,
                        severity,
                        Some(&order.symbol),
                        Some(client_id),
                        details,
                    )
                    .await;
                    return Err(TradingError::Risk(risk_err));
                }
            }
        }

        // ── 2. Insert order to DB as PENDING ───────────────────────────────────
        self.db_insert_order(&order).await?;

        // ── 3. Track in memory + increment open order count ────────────────────
        {
            let mut orders = self.orders.write().await;
            orders.insert(client_id, order.clone());
        }
        {
            let mut portfolio = self.portfolio.write().await;
            portfolio.open_order_count += 1;
        }

        // ── 4. Submit to broker ────────────────────────────────────────────────
        let broker_id = match self.broker.submit_order(&order).await {
            Ok(id) => id,
            Err(e) => {
                error!(order_id = %client_id, error = %e, "OMS: broker rejected order");
                self.db_update_order_status(client_id, OrderStatus::Rejected, None).await?;
                let mut orders = self.orders.write().await;
                if let Some(o) = orders.get_mut(&client_id) {
                    o.status = OrderStatus::Rejected;
                }
                let mut portfolio = self.portfolio.write().await;
                portfolio.open_order_count = portfolio.open_order_count.saturating_sub(1);
                return Err(e);
            }
        };

        // ── 5. Update to SUBMITTED with broker_order_id ────────────────────────
        self.db_update_order_status(client_id, OrderStatus::Submitted, Some(&broker_id))
            .await?;
        {
            let mut orders = self.orders.write().await;
            if let Some(o) = orders.get_mut(&client_id) {
                o.status = OrderStatus::Submitted;
            }
        }

        info!(
            client_order_id = %client_id,
            broker_order_id = %broker_id,
            symbol = %order.symbol,
            side   = %order.side,
            qty    = %order.quantity,
            price  = %current_price,
            "OMS: order submitted"
        );

        Ok(client_id)
    }

    // ── Apply fill ────────────────────────────────────────────────────────────

    /// Apply an incoming fill: update order, upsert position, log to DB.
    ///
    /// This is called from the fill consumer task that drains `fill_rx`.
    pub async fn apply_fill(&self, fill: Fill) -> Result<(), TradingError> {
        let client_id = fill.client_order_id;

        info!(
            fill_id         = %fill.fill_id,
            client_order_id = %client_id,
            symbol          = %fill.symbol,
            side            = %fill.side,
            qty             = %fill.filled_quantity,
            price           = %fill.fill_price,
            commission      = %fill.commission,
            "OMS: fill received"
        );

        // ── 1. Insert fill to DB ───────────────────────────────────────────────
        self.db_insert_fill(&fill).await?;

        // ── 2. Update order status to FILLED ──────────────────────────────────
        self.db_update_order_status(client_id, OrderStatus::Filled, None).await?;
        {
            let mut orders = self.orders.write().await;
            if let Some(o) = orders.get_mut(&client_id) {
                o.status = OrderStatus::Filled;
            }
        }

        // ── 3. Upsert position ────────────────────────────────────────────────
        {
            let mut positions = self.positions.write().await;

            if let Some(pos) = positions.get_mut(&fill.symbol) {
                pos.apply_fill(&fill);
                self.db_upsert_position(pos).await?;
            } else {
                let mut new_pos = Position::from_fill(&fill);
                new_pos.stop_loss = {
                    let orders = self.orders.read().await;
                    orders.get(&client_id).and_then(|o| o.stop_loss)
                };
                self.db_upsert_position(&new_pos).await?;
                positions.insert(fill.symbol.clone(), new_pos);
            }
        }

        // ── 4. Update portfolio snapshot ──────────────────────────────────────
        {
            let mut portfolio = self.portfolio.write().await;

            // Cash impact: buy costs cash, sell returns cash
            let cash_impact = match fill.side {
                Side::Buy => -(fill.fill_price * fill.filled_quantity + fill.commission),
                Side::Sell => fill.fill_price * fill.filled_quantity - fill.commission,
            };
            portfolio.daily_pnl += cash_impact;
            portfolio.open_order_count = portfolio.open_order_count.saturating_sub(1);

            // Sync positions map in snapshot
            let positions = self.positions.read().await;
            portfolio.positions = positions.clone();
        }

        Ok(())
    }

    // ── Cancel ────────────────────────────────────────────────────────────────

    /// Cancel a submitted order.
    pub async fn cancel(&self, client_order_id: Uuid) -> Result<(), TradingError> {
        // TODO Phase 1.5: store broker_order_id on Order struct and call broker.cancel_order()
        // For now, only the DB record is updated; PaperBroker cancel is handled via broker_id
        // which will be wired up when Order carries its broker_id.
        self.db_update_order_status(client_order_id, OrderStatus::Cancelled, None).await?;
        {
            let mut orders = self.orders.write().await;
            if let Some(o) = orders.get_mut(&client_order_id) {
                o.status = OrderStatus::Cancelled;
            }
        }
        {
            let mut portfolio = self.portfolio.write().await;
            portfolio.open_order_count = portfolio.open_order_count.saturating_sub(1);
        }

        info!(client_order_id = %client_order_id, "OMS: order cancelled");
        Ok(())
    }

    // ── Query ─────────────────────────────────────────────────────────────────

    pub async fn get_order_status(&self, client_order_id: Uuid) -> Result<OrderStatus, TradingError> {
        let orders = self.orders.read().await;
        if let Some(o) = orders.get(&client_order_id) {
            return Ok(o.status);
        }
        // Fall back to DB for orders evicted from memory
        let row = sqlx::query!(
            "SELECT status FROM orders WHERE client_order_id = $1",
            client_order_id
        )
        .fetch_optional(&self.pool)
        .await?;

        match row {
            None => Err(TradingError::OrderManager(format!(
                "Order {client_order_id} not found"
            ))),
            Some(r) => parse_order_status(&r.status),
        }
    }

    /// Returns the current portfolio snapshot (cloned for thread safety).
    pub async fn portfolio_snapshot(&self) -> PortfolioSnapshot {
        self.portfolio.read().await.clone()
    }

    // ── Database helpers ──────────────────────────────────────────────────────

    async fn db_insert_order(&self, order: &Order) -> Result<(), TradingError> {
        let (order_type_str, limit_price, stop_price) = match &order.order_type {
            OrderType::Market => ("MARKET", None, None),
            OrderType::Limit { limit_price } => ("LIMIT", Some(*limit_price), None),
            OrderType::StopLimit { stop_price, limit_price } => {
                ("STOP_LIMIT", Some(*limit_price), Some(*stop_price))
            }
        };

        sqlx::query!(
            r#"INSERT INTO orders
               (client_order_id, symbol, side, order_type, quantity,
                limit_price, stop_price, stop_loss, signal_score, strategy_id,
                status, created_at, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)"#,
            order.client_order_id,
            order.symbol,
            order.side.to_string(),
            order_type_str,
            order.quantity,
            limit_price,
            stop_price,
            order.stop_loss,
            order.signal_score,
            order.strategy_id,
            order.status.to_string(),
            order.created_at,
            order.updated_at,
        )
        .execute(&self.pool)
        .await?;

        Ok(())
    }

    async fn db_update_order_status(
        &self,
        client_order_id: Uuid,
        status: OrderStatus,
        broker_order_id: Option<&str>,
    ) -> Result<(), TradingError> {
        sqlx::query!(
            r#"UPDATE orders
               SET status = $1, broker_order_id = COALESCE($2, broker_order_id),
                   updated_at = NOW()
               WHERE client_order_id = $3"#,
            status.to_string(),
            broker_order_id,
            client_order_id,
        )
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    async fn db_insert_fill(&self, fill: &Fill) -> Result<(), TradingError> {
        // Look up strategy_id for denormalization
        let strategy_id = {
            let orders = self.orders.read().await;
            orders
                .get(&fill.client_order_id)
                .and_then(|o| o.strategy_id.clone())
        };

        sqlx::query!(
            r#"INSERT INTO fills
               (fill_id, client_order_id, broker_order_id, symbol, side,
                filled_quantity, fill_price, commission, timestamp, strategy_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)"#,
            fill.fill_id,
            fill.client_order_id,
            fill.broker_order_id,
            fill.symbol,
            fill.side.to_string(),
            fill.filled_quantity,
            fill.fill_price,
            fill.commission,
            fill.timestamp,
            strategy_id,
        )
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    async fn db_upsert_position(&self, pos: &Position) -> Result<(), TradingError> {
        sqlx::query!(
            r#"INSERT INTO positions
               (symbol, quantity, average_cost, realized_pnl, unrealized_pnl,
                stop_loss, opened_at, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
               ON CONFLICT (symbol) DO UPDATE SET
                 quantity       = EXCLUDED.quantity,
                 average_cost   = EXCLUDED.average_cost,
                 realized_pnl   = EXCLUDED.realized_pnl,
                 unrealized_pnl = EXCLUDED.unrealized_pnl,
                 stop_loss      = EXCLUDED.stop_loss,
                 updated_at     = NOW()"#,
            pos.symbol,
            pos.quantity,
            pos.average_cost,
            pos.realized_pnl,
            pos.unrealized_pnl,
            pos.stop_loss,
            pos.opened_at,
            pos.updated_at,
        )
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    async fn log_risk_event(
        &self,
        event_type: &str,
        severity: &str,
        symbol: Option<&str>,
        order_id: Option<Uuid>,
        details: serde_json::Value,
    ) {
        let result = sqlx::query!(
            r#"INSERT INTO risk_events (event_type, severity, symbol, order_id, details)
               VALUES ($1, $2, $3, $4, $5)"#,
            event_type,
            severity,
            symbol,
            order_id,
            details,
        )
        .execute(&self.pool)
        .await;

        if let Err(e) = result {
            error!("OMS: failed to log risk event to DB: {e}");
        }
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────────────────

fn risk_event_meta(err: &RiskError) -> (&'static str, &'static str) {
    match err {
        RiskError::DailyLossHalt { .. } => ("HALT", "DAILY_LOSS_HALT"),
        RiskError::DrawdownHalt { .. } => ("HALT", "DRAWDOWN_HALT"),
        RiskError::TooManyOpenOrders { .. } => ("WARN", "TOO_MANY_OPEN_ORDERS"),
        RiskError::SignalScoreTooLow { .. } => ("WARN", "SIGNAL_SCORE_LOW"),
        RiskError::MissingSignalScore => ("WARN", "MISSING_SIGNAL_SCORE"),
        RiskError::MissingStopLoss { .. } => ("WARN", "MISSING_STOP_LOSS"),
        RiskError::InvalidStopLoss { .. } => ("WARN", "INVALID_STOP_LOSS"),
        RiskError::PositionTooLarge { .. } => ("WARN", "POSITION_TOO_LARGE"),
        RiskError::ExposureLimitExceeded { .. } => ("WARN", "EXPOSURE_LIMIT_EXCEEDED"),
    }
}

fn parse_order_status(s: &str) -> Result<OrderStatus, TradingError> {
    match s {
        "PENDING" => Ok(OrderStatus::Pending),
        "SUBMITTED" => Ok(OrderStatus::Submitted),
        "PARTIALLY_FILLED" => Ok(OrderStatus::PartiallyFilled),
        "FILLED" => Ok(OrderStatus::Filled),
        "CANCELLED" => Ok(OrderStatus::Cancelled),
        "REJECTED" => Ok(OrderStatus::Rejected),
        other => Err(TradingError::OrderManager(format!(
            "Unknown order status in DB: {other}"
        ))),
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// Integration tests (require live PostgreSQL)
// ──────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::broker::paper::{PaperBroker, PaperConfig};
    use crate::types::Side;
    use rust_decimal_macros::dec;

    async fn try_pool() -> Option<PgPool> {
        let url = std::env::var("DATABASE_URL").unwrap_or_else(|_| {
            "postgres://quantai:quantai_dev_2026@localhost:5432/quantai".into()
        });
        sqlx::PgPool::connect(&url).await.ok()
    }

    #[tokio::test]
    async fn submit_and_fill_integration() {
        let Some(pool) = try_pool().await else {
            eprintln!("PostgreSQL not available — skipping integration test");
            return;
        };

        // Clean up any state from prior test runs so the test is idempotent.
        sqlx::query("DELETE FROM fills WHERE strategy_id = 'test'")
            .execute(&pool).await.ok();
        sqlx::query("DELETE FROM orders WHERE strategy_id = 'test'")
            .execute(&pool).await.ok();
        sqlx::query("DELETE FROM positions WHERE symbol = 'AAPL'")
            .execute(&pool).await.ok();

        let (paper_broker, mut fill_rx) = PaperBroker::new(PaperConfig {
            base_latency_ms: 10,
            latency_jitter_ms: 5,
            ..Default::default()
        });

        paper_broker.on_price_update("AAPL", dec!(150.00)).await;

        let broker: Arc<dyn Broker> = Arc::new(paper_broker);
        let oms = OmsManager::new(pool, broker, dec!(100_000)).await.unwrap();

        let order = Order::new_market("AAPL", Side::Buy, dec!(10), dec!(140), 0.75, "test");
        let client_id = oms.submit(order, dec!(150.00)).await.unwrap();

        // Drain the fill
        let fill = tokio::time::timeout(
            tokio::time::Duration::from_millis(500),
            fill_rx.recv(),
        )
        .await
        .expect("timeout")
        .expect("channel closed");

        oms.apply_fill(fill).await.unwrap();

        let status = oms.get_order_status(client_id).await.unwrap();
        assert_eq!(status, OrderStatus::Filled);

        let positions = oms.positions.read().await;
        assert!(positions.contains_key("AAPL"));
        assert_eq!(positions["AAPL"].quantity, dec!(10));
    }
}
