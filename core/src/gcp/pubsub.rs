// trading-system/core/src/gcp/pubsub.rs
//
// Async GCP Pub/Sub publisher — fire-and-forget pattern.
//
// ADR-002: GCP is ALWAYS downstream. Pub/Sub publish runs in a separate
// tokio task and NEVER blocks the order execution hot path. GCP failure
// NEVER halts trading.
//
// Usage:
//   let client = PubSubClient::new(config, reqwest::Client::new()).await?;
//   // In fill handler:
//   let client = client.clone();
//   let fill_copy = fill.clone();
//   tokio::spawn(async move {
//       if let Err(e) = client.publish_fill(&fill_copy).await {
//           tracing::warn!("Pub/Sub publish failed (non-fatal): {e}");
//       }
//   });
//
// Authentication strategy (in priority order):
//   1. GCE/Cloud Run metadata server (production)
//   2. GCP_ACCESS_TOKEN env var (CI / local testing)
//   Token caching: 5-minute buffer before expiry.

use std::{
    sync::Arc,
    time::{Duration, Instant},
};

use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json;
use tokio::sync::Mutex;
use tracing::{debug, warn};

use crate::{
    error::TradingError,
    types::{Fill, Tick},
};

use super::GcpConfig;

// ──────────────────────────────────────────────────────────────────────────────
// BigQuery record types
//
// These structs serialize to JSON that matches the BigQuery table schemas
// in gcp/bigquery/schema/. The Pub/Sub → BigQuery subscription uses
// use_table_schema=true, so field names must match exactly.
//
// NUMERIC BigQuery columns must be sent as strings (BQ auto-converts).
// FLOAT64 columns (signal_score) are sent as JSON numbers.
// TIMESTAMP columns are RFC3339 strings.
// ──────────────────────────────────────────────────────────────────────────────

/// BigQuery `trades` table record.
///
/// Constructed from a `Fill` + order metadata and published to `quantai-fills`.
/// The Pub/Sub → BigQuery subscription streams this to the `trades` table.
#[derive(Debug, Clone, Serialize)]
pub struct FillBqRecord {
    /// UUID string (matches PostgreSQL fills.fill_id)
    pub fill_id: String,
    pub client_order_id: String,
    pub broker_order_id: Option<String>,
    pub symbol: String,
    pub side: String,
    /// NUMERIC → string for BigQuery
    pub filled_quantity: String,
    pub fill_price: String,
    pub gross_value: String,
    pub commission: String,
    pub strategy_id: Option<String>,
    /// FLOAT64 — f64 is acceptable for signal scores per ADR-001
    pub signal_score: Option<f64>,
    /// RFC3339 timestamp string
    pub timestamp: String,
    /// Always "paper" for Phase 0–3 records
    pub trading_mode: String,
}

impl FillBqRecord {
    /// Construct from a Fill with supplemental fields from the originating order.
    pub fn from_fill(
        fill: &Fill,
        strategy_id: Option<String>,
        signal_score: Option<f64>,
        trading_mode: &str,
    ) -> Self {
        let gross_value = fill.filled_quantity * fill.fill_price;
        Self {
            fill_id: fill.fill_id.to_string(),
            client_order_id: fill.client_order_id.to_string(),
            broker_order_id: fill.broker_order_id.clone(),
            symbol: fill.symbol.clone(),
            side: fill.side.to_string(),
            filled_quantity: fill.filled_quantity.to_string(),
            fill_price: fill.fill_price.to_string(),
            gross_value: gross_value.to_string(),
            commission: fill.commission.to_string(),
            strategy_id,
            signal_score,
            timestamp: fill.timestamp.to_rfc3339(),
            trading_mode: trading_mode.to_string(),
        }
    }
}

/// Risk event record published to `quantai-risk-events` topic.
#[derive(Debug, Clone, Serialize)]
pub struct RiskEventBqRecord {
    pub event_type: String,
    pub severity: String,
    pub symbol: Option<String>,
    pub order_id: Option<String>,
    pub reason: String,
    pub timestamp: String,
}

// ──────────────────────────────────────────────────────────────────────────────
// Pub/Sub REST message types
// ──────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Serialize)]
struct PubSubMessage {
    data: String, // Base64-encoded payload
    #[serde(skip_serializing_if = "std::collections::HashMap::is_empty")]
    attributes: std::collections::HashMap<String, String>,
}

#[derive(Debug, Serialize)]
struct PublishRequest {
    messages: Vec<PubSubMessage>,
}

#[derive(Debug, Deserialize)]
struct PublishResponse {
    #[serde(rename = "messageIds")]
    message_ids: Vec<String>,
}

// ──────────────────────────────────────────────────────────────────────────────
// Token cache
// ──────────────────────────────────────────────────────────────────────────────

#[derive(Debug)]
struct CachedToken {
    token: String,
    expires_at: Instant,
}

impl CachedToken {
    /// Returns true if the token is still valid (with 5-minute buffer).
    fn is_valid(&self) -> bool {
        self.expires_at > Instant::now() + Duration::from_secs(300)
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// PubSubClient
// ──────────────────────────────────────────────────────────────────────────────

/// Async GCP Pub/Sub publisher.
///
/// Clone-safe — the HTTP client and token cache are shared via `Arc`.
#[derive(Clone)]
pub struct PubSubClient {
    config: GcpConfig,
    http: Client,
    token_cache: Arc<Mutex<Option<CachedToken>>>,
}

impl PubSubClient {
    /// Creates a new Pub/Sub client. Call `verify_connectivity()` to
    /// confirm credentials before the trading loop starts.
    pub fn new(config: GcpConfig, http: Client) -> Self {
        Self {
            config,
            http,
            token_cache: Arc::new(Mutex::new(None)),
        }
    }

    // ── Public publish methods ─────────────────────────────────────────────────

    /// Publish a fill event to the `quantai-fills` topic.
    ///
    /// Intended to be called from a spawned task — never on the hot path.
    pub async fn publish_fill(&self, fill: &Fill) -> Result<String, TradingError> {
        let mut attrs = std::collections::HashMap::new();
        attrs.insert("symbol".into(), fill.symbol.clone());
        attrs.insert("side".into(), fill.side.to_string());
        attrs.insert("type".into(), "fill".into());

        self.publish_message(&self.config.fills_topic, fill, attrs).await
    }

    /// Publish a tick to the `quantai-ticks` topic.
    pub async fn publish_tick(&self, tick: &Tick) -> Result<String, TradingError> {
        let mut attrs = std::collections::HashMap::new();
        attrs.insert("symbol".into(), tick.symbol.clone());
        attrs.insert("type".into(), "tick".into());

        self.publish_message(&self.config.ticks_topic, tick, attrs).await
    }

    /// Publish a fill to BigQuery (via the Pub/Sub → BigQuery subscription).
    ///
    /// The message JSON must match the `trades` BigQuery schema exactly.
    /// Call from a `tokio::spawn` task — never on the order execution hot path.
    pub async fn publish_fill_bq(&self, record: &FillBqRecord) -> Result<String, TradingError> {
        let mut attrs = std::collections::HashMap::new();
        attrs.insert("symbol".into(), record.symbol.clone());
        attrs.insert("side".into(), record.side.clone());
        attrs.insert("trading_mode".into(), record.trading_mode.clone());
        attrs.insert("type".into(), "fill".into());

        self.publish_message(&self.config.fills_topic, record, attrs).await
    }

    /// Publish a risk event to the `quantai-risk-events` topic.
    ///
    /// HALT events are published here so the monitoring pipeline can alert.
    /// Call from a `tokio::spawn` — never on the hot path.
    pub async fn publish_risk_event(&self, record: &RiskEventBqRecord) -> Result<String, TradingError> {
        let mut attrs = std::collections::HashMap::new();
        attrs.insert("severity".into(), record.severity.clone());
        attrs.insert("event_type".into(), record.event_type.clone());
        attrs.insert("type".into(), "risk_event".into());

        self.publish_message(&self.config.risk_events_topic, record, attrs).await
    }

    /// Verify that credentials are available and the project is reachable.
    pub async fn verify_connectivity(&self) -> Result<(), TradingError> {
        self.get_access_token().await.map(|_| ())
    }

    // ── Internal ───────────────────────────────────────────────────────────────

    async fn publish_message(
        &self,
        topic: &str,
        payload: &impl serde::Serialize,
        attributes: std::collections::HashMap<String, String>,
    ) -> Result<String, TradingError> {
        let token = self.get_access_token().await?;

        let json_bytes = serde_json::to_vec(payload)?;
        let data = BASE64.encode(&json_bytes);

        let request = PublishRequest {
            messages: vec![PubSubMessage { data, attributes }],
        };

        let url = format!(
            "https://pubsub.googleapis.com/v1/{}:publish",
            topic
        );

        let response = self
            .http
            .post(&url)
            .bearer_auth(&token)
            .json(&request)
            .send()
            .await
            .map_err(|e| TradingError::Gcp(format!("Pub/Sub HTTP error: {e}")))?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            return Err(TradingError::Gcp(format!(
                "Pub/Sub publish failed: HTTP {status}: {body}"
            )));
        }

        let resp: PublishResponse = response
            .json()
            .await
            .map_err(|e| TradingError::Gcp(format!("Pub/Sub parse error: {e}")))?;

        let message_id = resp.message_ids.into_iter().next().unwrap_or_default();
        if message_id.is_empty() {
            warn!(topic = %topic, "Pub/Sub: message accepted but no message_id returned");
        } else {
            debug!(topic = %topic, message_id = %message_id, "Pub/Sub: message published");
        }
        Ok(message_id)
    }

    /// Get a valid access token, refreshing from the best available source.
    ///
    /// Priority order (ADR-002: GCP is always downstream, credentials must be flexible):
    ///   1. GCE / Cloud Run metadata server  (production)
    ///   2. Application Default Credentials  (`gcloud auth application-default login`)
    ///   3. GCP_ACCESS_TOKEN env var          (CI / manual override)
    async fn get_access_token(&self) -> Result<String, TradingError> {
        let mut cache = self.token_cache.lock().await;

        if let Some(ref cached) = *cache {
            if cached.is_valid() {
                return Ok(cached.token.clone());
            }
        }

        // Priority 1: GCE / Cloud Run metadata server
        if let Ok(token) = self.fetch_metadata_token().await {
            *cache = Some(CachedToken {
                token: token.clone(),
                expires_at: Instant::now() + Duration::from_secs(3600),
            });
            return Ok(token);
        }

        // Priority 2: Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS
        // or ~/.config/gcloud/application_default_credentials.json)
        if let Ok((token, expires_in)) = self.fetch_adc_token().await {
            *cache = Some(CachedToken {
                token: token.clone(),
                expires_at: Instant::now() + Duration::from_secs(expires_in),
            });
            return Ok(token);
        }

        // Priority 3: GCP_ACCESS_TOKEN env var (CI / manual override)
        if let Ok(token) = std::env::var("GCP_ACCESS_TOKEN") {
            if !token.is_empty() {
                *cache = Some(CachedToken {
                    token: token.clone(),
                    expires_at: Instant::now() + Duration::from_secs(3600),
                });
                return Ok(token);
            }
        }

        Err(TradingError::Gcp(
            "No GCP credentials found. Run 'gcloud auth application-default login', \
             set GCP_ACCESS_TOKEN, or run on GCE/Cloud Run. \
             See GCP Infrastructure section in CLAUDE.md."
                .into(),
        ))
    }

    /// Fetch an access token from the GCE instance metadata server.
    ///
    /// Works automatically on GCE, Cloud Run, GKE, and Compute Engine.
    async fn fetch_metadata_token(&self) -> Result<String, reqwest::Error> {
        #[derive(Deserialize)]
        struct MetadataToken {
            access_token: String,
        }

        let response = self
            .http
            .get("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token")
            .header("Metadata-Flavor", "Google")
            .timeout(Duration::from_secs(2))
            .send()
            .await?;

        let token: MetadataToken = response.json().await?;
        Ok(token.access_token)
    }

    /// Fetch an access token from Application Default Credentials.
    ///
    /// Reads from `GOOGLE_APPLICATION_CREDENTIALS` if set, otherwise falls back
    /// to `~/.config/gcloud/application_default_credentials.json` (set by
    /// `gcloud auth application-default login`). Only `authorized_user` type is
    /// supported here — service account keys are handled via metadata server.
    async fn fetch_adc_token(&self) -> Result<(String, u64), TradingError> {
        #[derive(Deserialize)]
        struct AdcFile {
            #[serde(rename = "type")]
            cred_type: String,
            client_id: Option<String>,
            client_secret: Option<String>,
            refresh_token: Option<String>,
        }

        #[derive(Deserialize)]
        struct TokenResponse {
            access_token: String,
            expires_in: u64,
        }

        let adc_path = if let Ok(path) = std::env::var("GOOGLE_APPLICATION_CREDENTIALS") {
            std::path::PathBuf::from(path)
        } else {
            let home = std::env::var("HOME").unwrap_or_else(|_| "/root".into());
            std::path::PathBuf::from(home)
                .join(".config/gcloud/application_default_credentials.json")
        };

        let content = tokio::fs::read_to_string(&adc_path).await.map_err(|e| {
            TradingError::Gcp(format!("ADC file not readable at {}: {e}", adc_path.display()))
        })?;

        let creds: AdcFile = serde_json::from_str(&content)
            .map_err(|e| TradingError::Gcp(format!("ADC parse error: {e}")))?;

        if creds.cred_type != "authorized_user" {
            return Err(TradingError::Gcp(format!(
                "ADC type '{}' not supported (expected authorized_user)",
                creds.cred_type
            )));
        }

        let client_id = creds
            .client_id
            .ok_or_else(|| TradingError::Gcp("ADC missing client_id".into()))?;
        let client_secret = creds
            .client_secret
            .ok_or_else(|| TradingError::Gcp("ADC missing client_secret".into()))?;
        let refresh_token = creds
            .refresh_token
            .ok_or_else(|| TradingError::Gcp("ADC missing refresh_token".into()))?;

        let params = [
            ("grant_type", "refresh_token"),
            ("client_id", client_id.as_str()),
            ("client_secret", client_secret.as_str()),
            ("refresh_token", refresh_token.as_str()),
        ];

        let response = self
            .http
            .post("https://oauth2.googleapis.com/token")
            .form(&params)
            .send()
            .await
            .map_err(|e| TradingError::Gcp(format!("ADC token refresh HTTP error: {e}")))?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            return Err(TradingError::Gcp(format!(
                "ADC token refresh failed: HTTP {status}: {body}"
            )));
        }

        let resp: TokenResponse = response
            .json()
            .await
            .map_err(|e| TradingError::Gcp(format!("ADC token parse error: {e}")))?;

        debug!("ADC: access token refreshed (expires_in={}s)", resp.expires_in);
        Ok((resp.access_token, resp.expires_in))
    }
}

// ──────────────────────────────────────────────────────────────────────────────
// Fire-and-forget helper macro
// ──────────────────────────────────────────────────────────────────────────────

/// Spawn a Pub/Sub publish in a background task.
///
/// Per ADR-002, GCP failure must NEVER halt trading. This macro enforces that
/// contract: any publish error is logged as a warning and silently dropped.
///
/// Usage:
/// ```ignore
/// publish_async!(pubsub_client, publish_fill, &fill);
/// publish_async!(pubsub_client, publish_tick, &tick);
/// ```
#[macro_export]
macro_rules! publish_async {
    ($client:expr, $method:ident, $payload:expr) => {{
        let _client = $client.clone();
        let _payload = $payload.clone();
        tokio::spawn(async move {
            if let Err(e) = _client.$method(&_payload).await {
                tracing::warn!("Pub/Sub publish failed (non-fatal): {e}");
            }
        });
    }};
}
