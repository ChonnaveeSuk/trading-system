// trading-system/core/src/main.rs
//
// QuantAI execution engine — binary entrypoint.
//
// Startup sequence:
//   1. Initialize structured logging
//   2. Verify TRADING_MODE=paper (abort if not)
//   3. Connect PostgreSQL + Redis (health checks)
//   4. Start paper broker + fill consumer task
//   5. Start order manager (loads state from DB)
//   6. Start GCP Pub/Sub client (non-fatal if unavailable)
//   7. Run until shutdown signal (Ctrl-C / SIGTERM)

use std::sync::Arc;

use anyhow::{Context, Result};
use rust_decimal_macros::dec;
use tracing::{error, info, warn};
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt, EnvFilter};

use quantai_core::{
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
        redis_url: redis_url.clone(),
        symbols: vec![],
        tick_ttl_secs: 86_400,
    };
    let feed = RedisFeed::connect(feed_config).await.context("Failed to connect to Redis")?;
    feed.ping().await.context("Redis ping failed")?;
    info!("Redis connected");

    // ── 5. Paper broker ───────────────────────────────────────────────────────
    let (paper_broker, mut fill_rx) = PaperBroker::new(PaperConfig::default());
    let broker = Arc::new(paper_broker);

    // ── 6. Order manager ──────────────────────────────────────────────────────
    // Starting capital: read from daily_pnl or use hardcoded $100,000 default.
    let starting_value = dec!(100_000);
    let oms = OmsManager::new(pool.clone(), broker.clone(), starting_value)
        .await
        .context("Failed to initialize OMS")?;
    let oms_for_fills = oms.clone();

    info!("Order manager initialized (starting value: ${starting_value})");

    // ── 7. Fill consumer task ─────────────────────────────────────────────────
    // Drains fill_rx and forwards to OMS. Runs for the lifetime of the engine.
    tokio::spawn(async move {
        info!("Fill consumer task started");
        while let Some(fill) = fill_rx.recv().await {
            if let Err(e) = oms_for_fills.apply_fill(fill).await {
                error!("Fill apply error: {e}");
            }
        }
        warn!("Fill consumer task exited — broker channel closed");
    });

    // ── 8. GCP Pub/Sub (non-fatal) ────────────────────────────────────────────
    match GcpConfig::from_env() {
        Ok(gcp_config) => {
            let http = reqwest::Client::new();
            let pubsub = PubSubClient::new(gcp_config, http);
            match pubsub.verify_connectivity().await {
                Ok(()) => info!("GCP Pub/Sub credentials verified"),
                Err(e) => warn!("GCP Pub/Sub unavailable (non-fatal): {e}"),
            }
        }
        Err(e) => warn!("GCP config not set (non-fatal): {e}"),
    }

    // ── 9. Ready ──────────────────────────────────────────────────────────────
    info!(
        "QuantAI engine ready — paper trading mode active. \
         Waiting for signals from strategy layer (Phase 2)."
    );

    // Wait for shutdown signal
    tokio::signal::ctrl_c().await.context("Failed to listen for ctrl-c")?;
    info!("Shutdown signal received — exiting cleanly");

    Ok(())
}
