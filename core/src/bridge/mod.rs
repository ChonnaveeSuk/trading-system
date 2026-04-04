// trading-system/core/src/bridge/mod.rs
//
// gRPC bridge: Python strategy service → Rust execution engine.
//
// Every incoming SignalRequest is:
//   1. Parsed into a typed Order
//   2. Passed through the risk engine (check_order)
//   3. Submitted to OmsManager (→ broker → fill consumer)
//
// The bridge NEVER bypasses the risk engine. A rejected signal returns
// SignalResponse { accepted: false, status: "REJECTED" }.
//
// Startup: call `serve(oms, addr)` from main.rs in a spawned task.

use std::{net::SocketAddr, str::FromStr};

use rust_decimal::Decimal;
use tonic::{transport::Server, Request, Response, Status};
use tracing::{info, warn};

use crate::{
    order::manager::OmsManager,
    types::{Order, Side},
};

// Include generated tonic/prost code. cargo build → build.rs → tonic-build.
pub mod proto {
    tonic::include_proto!("trading");
}

use proto::{
    trading_bridge_server::{TradingBridge, TradingBridgeServer},
    HealthRequest, HealthResponse, SignalRequest, SignalResponse,
};

// ─────────────────────────────────────────────────────────────────────────────
// Signal payload (internal type, kept for logging / future extensions)
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct SignalPayload {
    pub strategy_id: String,
    pub symbol: String,
    pub score: f64,
    pub direction: String,
    pub suggested_stop_loss: Option<Decimal>,
    pub suggested_quantity: Option<Decimal>,
}

// ─────────────────────────────────────────────────────────────────────────────
// gRPC service implementation
// ─────────────────────────────────────────────────────────────────────────────

pub struct BridgeService {
    oms: OmsManager,
}

impl BridgeService {
    pub fn new(oms: OmsManager) -> Self {
        Self { oms }
    }
}

#[tonic::async_trait]
impl TradingBridge for BridgeService {
    async fn submit_signal(
        &self,
        request: Request<SignalRequest>,
    ) -> Result<Response<SignalResponse>, Status> {
        let req = request.into_inner();

        // ── Parse direction ──────────────────────────────────────────────────
        let side = match req.direction.to_uppercase().as_str() {
            "BUY"  => Side::Buy,
            "SELL" => Side::Sell,
            other  => {
                warn!(direction = other, "Bridge: invalid direction — HOLD must be filtered client-side");
                return Ok(Response::new(SignalResponse {
                    accepted: false,
                    order_id: String::new(),
                    status:   "REJECTED".into(),
                    message:  format!("Invalid direction '{other}'. Send BUY or SELL only."),
                }));
            }
        };

        // ── Parse Decimal fields ─────────────────────────────────────────────
        let stop_loss = match Decimal::from_str(&req.stop_loss) {
            Ok(v) => v,
            Err(_) => return Ok(Response::new(SignalResponse {
                accepted: false,
                order_id: String::new(),
                status:   "ERROR".into(),
                message:  format!("Cannot parse stop_loss '{}'", req.stop_loss),
            })),
        };

        let quantity = match Decimal::from_str(&req.quantity) {
            Ok(v) if v > Decimal::ZERO => v,
            _ => return Ok(Response::new(SignalResponse {
                accepted: false,
                order_id: String::new(),
                status:   "ERROR".into(),
                message:  format!("Cannot parse quantity '{}' (must be > 0)", req.quantity),
            })),
        };

        let current_price = match Decimal::from_str(&req.current_price) {
            Ok(v) if v > Decimal::ZERO => v,
            _ => return Ok(Response::new(SignalResponse {
                accepted: false,
                order_id: String::new(),
                status:   "ERROR".into(),
                message:  format!("Cannot parse current_price '{}'", req.current_price),
            })),
        };

        info!(
            strategy  = %req.strategy_id,
            symbol    = %req.symbol,
            direction = %req.direction,
            score     = req.score,
            qty       = %quantity,
            price     = %current_price,
            "Bridge: signal received"
        );

        // ── Build order ──────────────────────────────────────────────────────
        let order = Order::new_market(
            req.symbol.clone(),
            side,
            quantity,
            stop_loss,
            req.score,
            req.strategy_id.clone(),
        );

        // ── Seed price into broker cache before market order fill ────────────
        // PaperBroker fills market orders at last_prices[symbol]. Without this,
        // it would fill at Decimal::ZERO causing a DB constraint violation.
        self.oms.update_price(&req.symbol, current_price).await;

        // ── Submit (risk check → broker) ─────────────────────────────────────
        match self.oms.submit(order, current_price).await {
            Ok(order_id) => {
                info!(order_id = %order_id, symbol = %req.symbol, "Bridge: order accepted");
                Ok(Response::new(SignalResponse {
                    accepted: true,
                    order_id: order_id.to_string(),
                    status:   "SUBMITTED".into(),
                    message:  format!("Order {order_id} submitted to paper broker"),
                }))
            }
            Err(e) => {
                warn!(error = %e, symbol = %req.symbol, "Bridge: order rejected by risk engine");
                Ok(Response::new(SignalResponse {
                    accepted: false,
                    order_id: String::new(),
                    status:   "REJECTED".into(),
                    message:  e.to_string(),
                }))
            }
        }
    }

    async fn health_check(
        &self,
        _request: Request<HealthRequest>,
    ) -> Result<Response<HealthResponse>, Status> {
        let snap = self.oms.portfolio_snapshot().await;
        Ok(Response::new(HealthResponse {
            healthy:         true,
            paper_mode:      true,
            portfolio_value: snap.total_value.to_string(),
            open_orders:     snap.open_order_count as i32,
            pubsub_active:   self.oms.has_pubsub(),
        }))
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Server startup
// ─────────────────────────────────────────────────────────────────────────────

/// Start the gRPC bridge server.
///
/// Binds to `addr` and runs until the process exits.
/// Call from main.rs via `tokio::spawn(bridge::serve(oms.clone(), addr))`.
pub async fn serve(oms: OmsManager, addr: SocketAddr) -> anyhow::Result<()> {
    let service = BridgeService::new(oms);
    info!(addr = %addr, "gRPC bridge listening — ready for Python signals");
    Server::builder()
        .add_service(TradingBridgeServer::new(service))
        .serve(addr)
        .await
        .map_err(|e| anyhow::anyhow!("gRPC server error: {e}"))
}
