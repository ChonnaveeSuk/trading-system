// trading-system/core/src/main.rs
//
// QuantAI execution engine — binary entrypoint.
//
// Startup sequence:
//   1. Initialize structured logging
//   2. Verify TRADING_MODE=paper (abort if not)
//   3. Connect PostgreSQL + Redis (health checks)
//   4. Initialize paper broker + OMS (loads state from DB)
//   5. Wire GCP Pub/Sub into OMS (non-fatal if unavailable)
//   6. Start fill consumer task (now has GCP wired)
//   7. Run until shutdown signal (Ctrl-C / SIGTERM)

use std::sync::Arc;

use anyhow::{Context, Result};
use rust_decimal_macros::dec;
use tracing::{error, info, warn};
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt, EnvFilter};

use quantai_core::{
    bridge,
    broker::paper::{PaperBroker, PaperConfig},
    gcp::{pubsub::PubSubClient, GcpConfig},
    market_data::feed::{FeedConfig, RedisFeed},
    order::manager::OmsManager,
};

#[tokio::main]
async fn main() -> Result<()> {
    // ── 1. Logging ────────────────────────────────────────────────────────────
    tracing_subscriber::registry()
        .with(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info,quantai_core=debug".into()))
        .with(tracing_subscriber::fmt::layer().json())
        .init();

    info!(
        version = env!("CARGO_PKG_VERSION"),
        "QuantAI execution engine starting"
    );

    // ── 2. Safety: verify paper mode ─────────────────────────────────────────
    // Phase 1: env var fallback. Phase 2: load from GCP Secret Manager.
    let mode = std::env::var("TRADING_MODE").unwrap_or_else(|_| "paper".into());
    anyhow::ensure!(
        mode.to_lowercase() == "paper",
        "SAFETY ABORT: TRADING_MODE='{mode}' — expected 'paper'. \
         Live mode requires explicit Phase 4 authorization."
    );
    info!(mode = %mode, "Trading mode verified: PAPER ONLY");

    // ── 3. PostgreSQL ─────────────────────────────────────────────────────────
    let db_url = std::env::var("DATABASE_URL").unwrap_or_else(|_| {
        let pw = std::env::var("POSTGRES_PASSWORD").unwrap_or_else(|_| "quantai_dev_2026".into());
        let host = std::env::var("POSTGRES_HOST").unwrap_or_else(|_| "localhost".into());
        let port = std::env::var("POSTGRES_PORT").unwrap_or_else(|_| "5432".into());
        let db = std::env::var("POSTGRES_DB").unwrap_or_else(|_| "quantai".into());
        let user = std::env::var("POSTGRES_USER").unwrap_or_else(|_| "quantai".into());
        format!("postgres://{user}:{pw}@{host}:{port}/{db}")
    });

    info!("Connecting to PostgreSQL…");
    let pool = sqlx::PgPool::connect(&db_url)
        .await
        .context("Failed to connect to PostgreSQL — is Docker running? (`docker compose up -d`)")?;
    info!("PostgreSQL connected");

    // ── 4. Redis ──────────────────────────────────────────────────────────────
    let redis_url = std::env::var("REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379".into());

    info!("Connecting to Redis…");
    let feed_config = FeedConfig {
        redis_url,
        symbols: vec![],
        tick_ttl_secs: 86_400,
    };
    let _feed = RedisFeed::connect(feed_config).await.context("Failed to connect to Redis")?;
    _feed.ping().await.context("Redis ping failed")?;
    info!("Redis connected");

    // ── 5. Paper broker ───────────────────────────────────────────────────────
    let (paper_broker, fill_rx) = PaperBroker::new(PaperConfig::default());
    let broker = Arc::new(paper_broker);

    // ── 6. Order manager ──────────────────────────────────────────────────────
    let starting_value = dec!(100_000);
    let oms = OmsManager::new(pool.clone(), broker.clone(), starting_value)
        .await
        .context("Failed to initialize OMS")?;

    info!("Order manager initialized (starting value: ${starting_value})");

    // ── 7. GCP Pub/Sub (non-fatal — per ADR-002) ─────────────────────────────
    // Wire BEFORE spawning the fill consumer so the consumer gets the GCP-enabled OMS.
    // If credentials aren't available, trading continues in local-only mode.
    let oms = match GcpConfig::from_env() {
        Err(e) => {
            warn!("GCP_PROJECT_ID not set — running in local-only mode (non-fatal): {e}");
            oms
        }
        Ok(gcp_config) => {
            let http = reqwest::Client::new();
            let pubsub = PubSubClient::new(gcp_config, http);
            match pubsub.verify_connectivity().await {
                Ok(()) => {
                    info!("GCP Pub/Sub credentials verified — fills will stream to BigQuery");
                    oms.with_pubsub(pubsub)
                }
                Err(e) => {
                    warn!("GCP Pub/Sub unavailable — local-only mode (non-fatal): {e}");
                    oms
                }
            }
        }
    };

    // ── 8. gRPC bridge server ─────────────────────────────────────────────────
    // Listens on localhost:50051 for Python strategy signals.
    // Runs in its own task — gRPC errors do NOT crash the engine.
    let grpc_addr: std::net::SocketAddr = std::env::var("GRPC_ADDR")
        .unwrap_or_else(|_| "[::1]:50051".into())
        .parse()
        .context("Invalid GRPC_ADDR")?;

    let oms_for_grpc = oms.clone();
    tokio::spawn(async move {
        if let Err(e) = bridge::serve(oms_for_grpc, grpc_addr).await {
            error!("gRPC bridge exited: {e}");
        }
    });
    info!(addr = %grpc_addr, "gRPC bridge spawned — Python signals accepted");

    // ── 9. Fill consumer task ─────────────────────────────────────────────────
    // Drains fill_rx and forwards to OMS (which fire-and-forgets to GCP).
    // Must be spawned AFTER GCP is wired so the OMS clone carries pubsub.
    let oms_for_fills = oms.clone();
    let mut fill_rx = fill_rx;
    tokio::spawn(async move {
        info!("Fill consumer task started");
        while let Some(fill) = fill_rx.recv().await {
            if let Err(e) = oms_for_fills.apply_fill(fill).await {
                error!("Fill apply error: {e}");
            }
        }
        warn!("Fill consumer task exited — broker channel closed");
    });

    // ── 10. Ready ─────────────────────────────────────────────────────────────
    info!(
        pubsub_active = oms.has_pubsub(),
        "QuantAI engine ready — paper trading active. Awaiting signals (Phase 2)."
    );

    // Wait for shutdown signal
    tokio::signal::ctrl_c().await.context("Failed to listen for ctrl-c")?;
    info!("Shutdown signal received — exiting cleanly");

    Ok(())
}
