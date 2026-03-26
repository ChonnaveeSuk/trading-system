// trading-system/core/src/lib.rs
//
// QuantAI Core — execution engine library crate.
// Binary entrypoint is main.rs. Tests use this lib directly.

// TradingError is intentionally rich; boxing all variants degrades ergonomics
// without meaningful benefit in a single-process trading engine.
#![allow(clippy::result_large_err)]

pub mod bridge;
pub mod broker;
pub mod error;
pub mod gcp;
pub mod market_data;
pub mod order;
pub mod risk;
pub mod types;

// Re-export the most commonly used types at the crate root for ergonomics.
pub use error::{RiskError, TradingError};
pub use types::{Bar, Fill, Order, OrderStatus, OrderType, Position, Side, Tick, TimeInForce};
