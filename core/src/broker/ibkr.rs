// trading-system/core/src/broker/ibkr.rs
//
// Interactive Brokers TWS broker implementation.
//
// Phase 1 status: connection management skeleton.
// The IBKR EClient protocol will be implemented in Phase 1.5 once the
// paper trading loop is validated end-to-end.
//
// Connection parameters:
//   - Paper trading port: 7497 (sourced from Secret Manager: ibkr-paper-port)
//   - Live trading port:  7496 (NEVER used until Phase 4 authorization)
//   - Client ID: 1 (configurable via IBKR_CLIENT_ID env var)
//
// Architecture:
//   IbkrBroker wraps a long-lived TCP connection to TWS / IB Gateway.
//   Orders are submitted as EClient messages; fills arrive as EWrapper callbacks.
//   The fill handler sends fills to a tokio mpsc channel consumed by the OMS.
//
// Thread model:
//   - reader task: tokio::spawn reading incoming TWS messages
//   - writer:      tokio::sync::Mutex<TcpStream::write_half>
//   - fill_tx:     mpsc::Sender<Fill> → OMS

use async_trait::async_trait;
use tracing::{info, warn};

use crate::{
    error::TradingError,
    types::{Fill, Order},
};

use super::Broker;

// ──────────────────────────────────────────────────────────────────────────────
// Configuration
// ──────────────────────────────────────────────────────────────────────────────

/// IBKR TWS / IB Gateway connection parameters.
#[derive(Debug, Clone)]
pub struct IbkrConfig {
    /// TWS host — always 127.0.0.1 for local TWS.
    pub host: String,
    /// TWS port — 7497 for paper, 7496 for live. NEVER set to 7496 in paper mode.
    pub port: u16,
    /// Client ID — must be unique per TWS connection.
    pub client_id: i32,
    /// Connection timeout in seconds.
    pub connect_timeout_secs: u64,
}

impl Default for IbkrConfig {
    fn default() -> Self {
        Self {
            host: "127.0.0.1".into(),
            port: 7497, // Paper trading port — ALWAYS
            client_id: 1,
            connect_timeout_secs: 10,
        }
    }
}

impl IbkrConfig {
    /// Load from environment variables with Secret Manager override in Phase 1.
    pub fn from_env() -> Self {
        let port: u16 = std::env::var("IBKR_PORT")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(7497);

        // Safety: reject any attempt to use the live port here.
        // The live port (7496) requires Phase 4 authorization and a separate
        // code path that doesn't exist yet.
        let port = if port == 7496 {
            warn!("IBKR_PORT=7496 (live) rejected — defaulting to 7497 (paper)");
            7497
        } else {
            port
        };

        Self {
            host: std::env::var("IBKR_HOST").unwrap_or_else(|_| "127.0.0.1".into()),
            port,
            client_id: std::env::var("IBKR_CLIENT_ID")
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(1),
            connect_timeout_secs: 10,
        }
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// IbkrBroker
// ──────────────────────────────────────────────────────────────────────────────

/// Interactive Brokers broker.
///
/// Implements the [`Broker`] trait. The OMS holds a `Box<dyn Broker>` and
/// switches between `PaperBroker` and `IbkrBroker` based on `TRADING_MODE`.
///
/// # Current state (Phase 1)
///
/// Connection management and the EClient message loop are stubbed out.
/// `submit_order` and `cancel_order` return `Err(TradingError::Broker(...))`.
/// `health_check` attempts a TCP connect to verify TWS is listening.
///
/// Full EClient implementation follows in Phase 1.5.
pub struct IbkrBroker {
    config: IbkrConfig,
}

impl IbkrBroker {
    /// Creates a new IBKR broker. Call `connect()` before submitting orders.
    pub fn new(config: IbkrConfig) -> Self {
        Self { config }
    }

    /// Attempt TCP connection to TWS / IB Gateway.
    ///
    /// Returns Ok if the port is reachable. Full EClient handshake
    /// is implemented in Phase 1.5.
    pub async fn connect(&self) -> Result<(), TradingError> {
        use tokio::net::TcpStream;
        use tokio::time::{timeout, Duration};

        let addr = format!("{}:{}", self.config.host, self.config.port);
        info!(addr = %addr, client_id = self.config.client_id, "IBKR: attempting TCP connect");

        let result = timeout(
            Duration::from_secs(self.config.connect_timeout_secs),
            TcpStream::connect(&addr),
        )
        .await;

        match result {
            Ok(Ok(_)) => {
                info!(addr = %addr, "IBKR: TCP connection established");
                // TODO Phase 1.5: Send EClient handshake, start reader task
                Ok(())
            }
            Ok(Err(e)) => {
                warn!(addr = %addr, error = %e, "IBKR: TCP connect failed");
                Err(TradingError::Broker(format!(
                    "Cannot connect to TWS at {addr}: {e}"
                )))
            }
            Err(_) => {
                warn!(addr = %addr, "IBKR: TCP connect timed out");
                Err(TradingError::Broker(format!(
                    "TWS at {addr} did not respond within {}s — is TWS running?",
                    self.config.connect_timeout_secs
                )))
            }
        }
    }
}

#[async_trait]
impl Broker for IbkrBroker {
    async fn submit_order(&self, order: &Order) -> Result<String, TradingError> {
        // TODO Phase 1.5: encode as EClient PlaceOrder message and write to TWS
        Err(TradingError::Broker(format!(
            "IBKR submit_order not yet implemented (Phase 1.5) — \
             order {} rejected",
            order.client_order_id
        )))
    }

    async fn cancel_order(&self, broker_order_id: &str) -> Result<(), TradingError> {
        // TODO Phase 1.5: encode as EClient CancelOrder message
        Err(TradingError::Broker(format!(
            "IBKR cancel_order not yet implemented (Phase 1.5) — \
             broker_id {broker_order_id}"
        )))
    }

    async fn health_check(&self) -> Result<(), TradingError> {
        self.connect().await
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// Fill type placeholder (fills arrive from TWS EWrapper callbacks)
// ──────────────────────────────────────────────────────────────────────────────

/// Phase 1.5: EWrapper fill handler will convert TWS `execDetails` messages
/// into `Fill` structs and send them on this channel.
///
/// ```ignore
/// // Pattern for the reader task:
/// async fn handle_exec_details(fill_tx: mpsc::Sender<Fill>, exec: IbkrExecDetails) {
///     let fill = Fill { /* ... */ };
///     fill_tx.send(fill).await.ok();
/// }
/// ```
pub type FillSender = tokio::sync::mpsc::Sender<Fill>;
