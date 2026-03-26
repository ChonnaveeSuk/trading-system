// trading-system/core/src/bridge/mod.rs
//
// gRPC bridge: receives AI signals from Python strategy service → converts to Orders.
//
// Phase 2 will implement the full tonic gRPC server.
//
// Data flow:
//   Python (Cloud Run) ──gRPC──► Rust bridge ──► risk check ──► OMS ──► broker
//
// IMPORTANT: The bridge converts signals into Orders. It does NOT bypass
// the risk engine. Every signal that becomes an order goes through check_order().

// TODO Phase 2: proto/signals.proto definition
// TODO Phase 2: tonic server implementation
// TODO Phase 2: signal → Order conversion with risk check

/// A trading signal received from the Python strategy layer.
#[derive(Debug, Clone)]
pub struct SignalPayload {
    pub strategy_id: String,
    pub symbol: String,
    /// Score in [0.0, 1.0]. Must be >= 0.55 or risk engine rejects.
    pub score: f64,
    /// "BUY", "SELL", or "HOLD" (HOLD signals are discarded here).
    pub direction: String,
    /// Suggested stop loss price. Risk engine validates it.
    pub suggested_stop_loss: Option<rust_decimal::Decimal>,
    /// Suggested quantity. Risk engine will cap at position size limit.
    pub suggested_quantity: Option<rust_decimal::Decimal>,
}
