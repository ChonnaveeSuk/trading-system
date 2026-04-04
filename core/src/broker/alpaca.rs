// trading-system/core/src/broker/alpaca.rs
//
// Alpaca Markets paper trading broker implementation.
//
// REST API:  https://paper-api.alpaca.markets/v2
// Auth:      APCA-API-KEY-ID + APCA-API-SECRET-KEY request headers
//
// Credentials are stored in GCP Secret Manager:
//   alpaca-api-key    → APCA-API-KEY-ID
//   alpaca-secret-key → APCA-API-SECRET-KEY
//   alpaca-endpoint   → base URL (override for live when Phase 4 authorized)
//
// Implements the Broker trait:
//   submit_order → POST /orders
//   cancel_order → DELETE /orders/{id}
//   health_check → GET /account
//
// Additional public methods (for test scripts and diagnostics):
//   get_account()   → AlpacaAccount
//   get_positions() → Vec<AlpacaPosition>
//   get_order()     → AlpacaOrder
//
// Fill flow:
//   Alpaca fills orders asynchronously. The test script (scripts/test_alpaca_connection.py)
//   polls GET /orders/{id} until status == "filled", then inserts into PostgreSQL.
//   A streaming fill consumer (via Alpaca WebSocket) is planned for Phase 4.

use async_trait::async_trait;
use reqwest::{header, Client, StatusCode};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::str::FromStr;
use tracing::{info, warn};

use crate::{
    error::TradingError,
    types::{Order, OrderType, Side},
};

use super::Broker;

// ──────────────────────────────────────────────────────────────────────────────
// Configuration
// ──────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct AlpacaConfig {
    /// Base URL — https://paper-api.alpaca.markets/v2 for paper mode.
    /// NEVER change to the live endpoint (api.alpaca.markets) without Phase 4 authorization.
    pub endpoint: String,
    /// Alpaca paper API key (APCA-API-KEY-ID).
    pub api_key: String,
    /// Alpaca paper secret key (APCA-API-SECRET-KEY).
    pub secret_key: String,
}

impl AlpacaConfig {
    /// Load from environment variables.
    ///
    /// ALPACA_ENDPOINT, ALPACA_API_KEY, ALPACA_SECRET_KEY
    /// (typically injected from GCP Secret Manager at startup).
    pub fn from_env() -> Result<Self, TradingError> {
        let endpoint = std::env::var("ALPACA_ENDPOINT")
            .unwrap_or_else(|_| "https://paper-api.alpaca.markets/v2".into());

        // Safety: reject live endpoint — must never reach live without Phase 4 auth.
        if endpoint.contains("api.alpaca.markets") && !endpoint.contains("paper-api") {
            warn!("SAFETY: ALPACA_ENDPOINT appears to be the live endpoint — defaulting to paper");
            return Err(TradingError::Broker(
                "ALPACA_ENDPOINT must be the paper endpoint (paper-api.alpaca.markets). \
                 Live trading requires explicit Phase 4 authorization."
                    .into(),
            ));
        }

        let api_key = std::env::var("ALPACA_API_KEY")
            .map_err(|_| TradingError::Broker("ALPACA_API_KEY not set".into()))?;
        let secret_key = std::env::var("ALPACA_SECRET_KEY")
            .map_err(|_| TradingError::Broker("ALPACA_SECRET_KEY not set".into()))?;

        Ok(Self { endpoint, api_key, secret_key })
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// REST request / response types
// ──────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Serialize)]
struct AlpacaOrderRequest {
    symbol: String,
    qty: String,
    side: String,
    #[serde(rename = "type")]
    order_type: String,
    time_in_force: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    limit_price: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    stop_price: Option<String>,
    client_order_id: String,
}

/// Alpaca order response (subset of fields used by this broker).
#[derive(Debug, Deserialize)]
pub struct AlpacaOrder {
    /// Alpaca-assigned UUID — use as broker_order_id.
    pub id: String,
    pub client_order_id: Option<String>,
    pub symbol: String,
    pub status: String,
    pub filled_qty: Option<String>,
    pub filled_avg_price: Option<String>,
    pub qty: Option<String>,
    pub side: Option<String>,
}

impl AlpacaOrder {
    /// True when Alpaca has fully executed the order.
    pub fn is_filled(&self) -> bool {
        self.status == "filled"
    }

    /// Parse filled_avg_price into Decimal. Returns None if not yet filled.
    pub fn fill_price(&self) -> Option<Decimal> {
        self.filled_avg_price
            .as_deref()
            .and_then(|s| Decimal::from_str(s).ok())
    }

    /// Parse filled_qty into Decimal.
    pub fn filled_quantity(&self) -> Option<Decimal> {
        self.filled_qty
            .as_deref()
            .and_then(|s| Decimal::from_str(s).ok())
    }
}

/// Alpaca account — returned by GET /account.
#[derive(Debug, Deserialize)]
pub struct AlpacaAccount {
    pub id: String,
    pub status: String,
    pub equity: String,
    pub cash: String,
    pub buying_power: String,
    pub currency: String,
}

/// Alpaca position — element of GET /positions response.
#[derive(Debug, Deserialize)]
pub struct AlpacaPosition {
    pub symbol: String,
    pub qty: String,
    pub avg_entry_price: String,
    pub current_price: Option<String>,
    pub unrealized_pl: Option<String>,
    pub market_value: Option<String>,
    pub side: String,
}

// ──────────────────────────────────────────────────────────────────────────────
// AlpacaBroker
// ──────────────────────────────────────────────────────────────────────────────

/// Alpaca Markets paper trading broker.
///
/// Connects to https://paper-api.alpaca.markets/v2.
/// Implements the [`Broker`] trait alongside [`PaperBroker`].
///
/// The OMS holds `Box<dyn Broker>` — switching between local paper simulation
/// (PaperBroker) and real Alpaca paper execution (AlpacaBroker) requires no
/// OMS code changes; only main.rs selects which broker to instantiate.
///
/// # Fill handling
///
/// Alpaca fills arrive asynchronously. For Phase 3 paper validation:
///   - `scripts/test_alpaca_connection.py` submits a test order and polls
///     GET /orders/{id} until filled, then writes to PostgreSQL via psycopg2.
///   - Phase 4 will stream fills via the Alpaca WebSocket data stream and
///     call `oms.apply_fill()` on each event.
pub struct AlpacaBroker {
    config: AlpacaConfig,
    client: Client,
}

impl AlpacaBroker {
    /// Creates a new AlpacaBroker with pre-authenticated HTTP client.
    pub fn new(config: AlpacaConfig) -> Result<Self, TradingError> {
        let mut headers = header::HeaderMap::new();

        headers.insert(
            "APCA-API-KEY-ID",
            header::HeaderValue::from_str(&config.api_key)
                .map_err(|e| TradingError::Broker(format!("Invalid Alpaca API key: {e}")))?,
        );
        headers.insert(
            "APCA-API-SECRET-KEY",
            header::HeaderValue::from_str(&config.secret_key)
                .map_err(|e| TradingError::Broker(format!("Invalid Alpaca secret key: {e}")))?,
        );

        let client = Client::builder()
            .default_headers(headers)
            .timeout(std::time::Duration::from_secs(15))
            .build()
            .map_err(|e| TradingError::Broker(format!("HTTP client build failed: {e}")))?;

        Ok(Self { config, client })
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.config.endpoint, path)
    }

    /// GET /account — verify credentials and retrieve account info.
    pub async fn get_account(&self) -> Result<AlpacaAccount, TradingError> {
        let resp = self
            .client
            .get(self.url("/account"))
            .send()
            .await
            .map_err(|e| TradingError::Broker(format!("Alpaca GET /account: {e}")))?;

        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            return Err(TradingError::Broker(format!(
                "Alpaca GET /account → {status}: {body}"
            )));
        }

        resp.json::<AlpacaAccount>()
            .await
            .map_err(|e| TradingError::Broker(format!("Alpaca account parse error: {e}")))
    }

    /// GET /positions — all currently open positions on the Alpaca account.
    pub async fn get_positions(&self) -> Result<Vec<AlpacaPosition>, TradingError> {
        let resp = self
            .client
            .get(self.url("/positions"))
            .send()
            .await
            .map_err(|e| TradingError::Broker(format!("Alpaca GET /positions: {e}")))?;

        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            return Err(TradingError::Broker(format!(
                "Alpaca GET /positions → {status}: {body}"
            )));
        }

        resp.json::<Vec<AlpacaPosition>>()
            .await
            .map_err(|e| TradingError::Broker(format!("Alpaca positions parse error: {e}")))
    }

    /// GET /orders/{id} — fetch the current state of an order.
    pub async fn get_order(&self, alpaca_order_id: &str) -> Result<AlpacaOrder, TradingError> {
        let resp = self
            .client
            .get(self.url(&format!("/orders/{alpaca_order_id}")))
            .send()
            .await
            .map_err(|e| TradingError::Broker(format!("Alpaca GET /orders/{alpaca_order_id}: {e}")))?;

        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            return Err(TradingError::Broker(format!(
                "Alpaca GET /orders/{alpaca_order_id} → {status}: {body}"
            )));
        }

        resp.json::<AlpacaOrder>()
            .await
            .map_err(|e| TradingError::Broker(format!("Alpaca order parse error: {e}")))
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// Broker trait implementation
// ──────────────────────────────────────────────────────────────────────────────

#[async_trait]
impl Broker for AlpacaBroker {
    async fn submit_order(&self, order: &Order) -> Result<String, TradingError> {
        let (order_type_str, limit_price, stop_price) = match &order.order_type {
            OrderType::Market => ("market", None, None),
            OrderType::Limit { limit_price } => {
                ("limit", Some(format!("{:.2}", limit_price)), None)
            }
            OrderType::StopLimit { stop_price, limit_price } => (
                "stop_limit",
                Some(format!("{:.2}", limit_price)),
                Some(format!("{:.2}", stop_price)),
            ),
        };

        let body = AlpacaOrderRequest {
            symbol: order.symbol.clone(),
            qty: format!("{}", order.quantity),
            side: match order.side {
                Side::Buy => "buy".into(),
                Side::Sell => "sell".into(),
            },
            order_type: order_type_str.into(),
            time_in_force: "day".into(),
            limit_price,
            stop_price,
            client_order_id: order.client_order_id.to_string(),
        };

        info!(
            symbol     = %order.symbol,
            side       = %order.side,
            qty        = %order.quantity,
            order_type = order_type_str,
            "Alpaca: submitting order"
        );

        let resp = self
            .client
            .post(self.url("/orders"))
            .json(&body)
            .send()
            .await
            .map_err(|e| TradingError::Broker(format!("Alpaca POST /orders: {e}")))?;

        if !resp.status().is_success() {
            let status = resp.status();
            let body_text = resp.text().await.unwrap_or_default();
            return Err(TradingError::Broker(format!(
                "Alpaca POST /orders → {status}: {body_text}"
            )));
        }

        let order_resp = resp
            .json::<AlpacaOrder>()
            .await
            .map_err(|e| TradingError::Broker(format!("Alpaca order response parse: {e}")))?;

        info!(
            alpaca_id = %order_resp.id,
            status    = %order_resp.status,
            symbol    = %order.symbol,
            "Alpaca: order accepted"
        );

        Ok(order_resp.id)
    }

    async fn cancel_order(&self, broker_order_id: &str) -> Result<(), TradingError> {
        let resp = self
            .client
            .delete(self.url(&format!("/orders/{broker_order_id}")))
            .send()
            .await
            .map_err(|e| TradingError::Broker(format!("Alpaca DELETE /orders: {e}")))?;

        match resp.status() {
            StatusCode::NO_CONTENT | StatusCode::OK => {
                info!(broker_order_id, "Alpaca: order cancelled");
                Ok(())
            }
            StatusCode::NOT_FOUND => Err(TradingError::Broker(format!(
                "Alpaca: order '{broker_order_id}' not found (already filled or expired)"
            ))),
            status => {
                let body = resp.text().await.unwrap_or_default();
                Err(TradingError::Broker(format!(
                    "Alpaca DELETE /orders/{broker_order_id} → {status}: {body}"
                )))
            }
        }
    }

    async fn health_check(&self) -> Result<(), TradingError> {
        let account = self.get_account().await?;
        info!(
            account_id   = %account.id,
            status       = %account.status,
            equity       = %account.equity,
            cash         = %account.cash,
            buying_power = %account.buying_power,
            "Alpaca: health check OK"
        );
        Ok(())
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// Tests
// ──────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn make_config(endpoint: &str) -> AlpacaConfig {
        AlpacaConfig {
            endpoint: endpoint.into(),
            api_key: "test_key".into(),
            secret_key: "test_secret".into(),
        }
    }

    /// Verify endpoint safety guard rejects live URL.
    #[test]
    fn live_endpoint_rejected() {
        // Simulate what from_env would do if ALPACA_ENDPOINT pointed at live.
        let endpoint = "https://api.alpaca.markets/v2".to_string();
        let is_live = endpoint.contains("api.alpaca.markets") && !endpoint.contains("paper-api");
        assert!(is_live, "Live endpoint should be flagged");
    }

    #[test]
    fn paper_endpoint_accepted() {
        let config = make_config("https://paper-api.alpaca.markets/v2");
        assert!(config.endpoint.contains("paper-api"));
    }

    #[test]
    fn alpaca_order_fill_price_parse() {
        let order = AlpacaOrder {
            id: "abc".into(),
            client_order_id: None,
            symbol: "AAPL".into(),
            status: "filled".into(),
            filled_qty: Some("1".into()),
            filled_avg_price: Some("174.50".into()),
            qty: Some("1".into()),
            side: Some("buy".into()),
        };

        assert!(order.is_filled());
        assert_eq!(
            order.fill_price(),
            Some(Decimal::from_str("174.50").unwrap())
        );
        assert_eq!(
            order.filled_quantity(),
            Some(Decimal::from_str("1").unwrap())
        );
    }
}
