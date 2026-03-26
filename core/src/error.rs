// trading-system/core/src/error.rs
//
// Unified error hierarchy for the QuantAI execution engine.
// Use TradingError at binary boundaries; RiskError internally for risk module.

use rust_decimal::Decimal;
use thiserror::Error;
use uuid::Uuid;

/// Top-level error type for the execution engine.
/// Library code returns this; binary code wraps with anyhow.
#[derive(Debug, Error)]
pub enum TradingError {
    #[error("Risk check failed: {0}")]
    Risk(#[from] RiskError),

    #[error("Broker error: {0}")]
    Broker(String),

    #[error("Market data error: {0}")]
    MarketData(String),

    #[error("Order manager error: {0}")]
    OrderManager(String),

    #[error("Database error: {0}")]
    Database(#[from] sqlx::Error),

    #[error("Redis error: {0}")]
    Redis(#[from] redis::RedisError),

    #[error("GCP error: {0}")]
    Gcp(String),

    #[error("gRPC error: {0}")]
    Grpc(#[from] tonic::Status),

    #[error("Configuration error: {0}")]
    Config(String),

    #[error("Serialization error: {0}")]
    Serialization(#[from] serde_json::Error),

    #[error("System halted: {reason}")]
    Halted { reason: String },
}

/// Granular risk violations — returned by RiskEngine::check_order.
/// Each variant maps to a distinct, actionable condition.
#[derive(Debug, Error)]
pub enum RiskError {
    /// Daily loss limit breached — ALL trading halted until reset.
    #[error(
        "DAILY LOSS HALT: {loss_pct:.2}% daily loss >= {limit_pct:.2}% limit. \
         All trading halted until manual reset."
    )]
    DailyLossHalt { loss_pct: f64, limit_pct: f64 },

    /// Peak-to-trough drawdown limit breached — ALL trading halted.
    #[error(
        "DRAWDOWN HALT: {drawdown_pct:.2}% drawdown >= {limit_pct:.2}% limit. \
         All trading halted until manual reset."
    )]
    DrawdownHalt { drawdown_pct: f64, limit_pct: f64 },

    /// Too many simultaneous open orders.
    #[error("Open order limit reached: {current} open >= max {max}")]
    TooManyOpenOrders { current: usize, max: usize },

    /// Signal confidence below the minimum threshold.
    #[error("Signal score {score:.4} below minimum {minimum:.4} — order rejected")]
    SignalScoreTooLow { score: f64, minimum: f64 },

    /// Every order must carry a signal score — naked orders are not allowed.
    #[error("Order missing signal_score field — all orders require a signal score")]
    MissingSignalScore,

    /// Every order must have a stop loss — no naked positions.
    #[error("Order {order_id} has no stop_loss — naked positions are forbidden")]
    MissingStopLoss { order_id: Uuid },

    /// Stop loss is on the wrong side of the current price.
    #[error("Invalid stop loss: {reason}")]
    InvalidStopLoss { reason: String },

    /// Single-order value exceeds portfolio percentage limit.
    #[error(
        "Position too large: ${requested:.2} > max ${maximum:.2} \
         ({pct:.1}% of ${portfolio_value:.2} portfolio)"
    )]
    PositionTooLarge {
        requested: Decimal,
        maximum: Decimal,
        portfolio_value: Decimal,
        pct: f64,
    },

    /// Adding to an existing position would breach the per-symbol exposure cap.
    #[error("Total exposure for symbol would reach ${total:.2} > max ${maximum:.2}")]
    ExposureLimitExceeded { total: Decimal, maximum: Decimal },
}
