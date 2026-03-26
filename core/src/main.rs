// trading-system/core/src/main.rs
//
// QuantAI execution engine — binary entrypoint.
//
// Startup sequence:
//   1. Initialize structured logging
//   2. Load config (Phase 1: from GCP Secret Manager)
//   3. Verify MODE=paper — HALT if not paper
//   4. Connect PostgreSQL + Redis (health check)
//   5. Start gRPC bridge (Phase 2)
//   6. Start market data feed (Phase 1)
//   7. Start order manager event loop (Phase 1)

use anyhow::{Context, Result};
use tracing::info;
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt, EnvFilter};

#[tokio::main]
async fn main() -> Result<()> {
    // ── Logging ──────────────────────────────────────────────────────────────
    tracing_subscriber::registry()
        .with(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .with(tracing_subscriber::fmt::layer().json())
        .init();

    info!(
        version = env!("CARGO_PKG_VERSION"),
        "QuantAI execution engine starting"
    );

    // ── Safety: verify paper mode ─────────────────────────────────────────────
    // TODO Phase 1: load from GCP Secret Manager
    // For now, env var is the fallback — will be removed in Phase 1
    let mode = std::env::var("TRADING_MODE").unwrap_or_else(|_| "paper".into());
    anyhow::ensure!(
        mode.to_lowercase() == "paper",
        "SAFETY ABORT: TRADING_MODE is '{mode}', expected 'paper'. \
         Never run this system in live mode without explicit authorization."
    );
    info!(mode, "Trading mode verified");

    // ── Infrastructure health checks (Phase 1: real connections) ─────────────
    // TODO Phase 1: connect to PostgreSQL via sqlx
    // TODO Phase 1: connect to Redis via redis-rs
    // TODO Phase 1: verify GCP credentials + Pub/Sub topic exists
    info!("Infrastructure checks: TODO (Phase 1)");

    // ── Start services (Phase 1+) ─────────────────────────────────────────────
    // TODO Phase 1: broker::paper::PaperBroker::start()
    // TODO Phase 1: market_data::feed::IbkrFeed::start()
    // TODO Phase 1: order::manager::OrderManager::start()
    // TODO Phase 2: bridge::grpc::BridgeServer::start()

    info!("Phase 0 scaffold complete — all systems ready for Phase 1 implementation");
    info!("Run 'cargo test' to verify the risk engine passes all tests");

    Ok(())
}
