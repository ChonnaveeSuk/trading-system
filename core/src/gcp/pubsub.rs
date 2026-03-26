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
use tracing::debug;

use crate::{
    error::TradingError,
    types::{Fill, Tick},
};

use super::GcpConfig;

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
        debug!(topic = %topic, message_id = %message_id, "Pub/Sub: message published");
        Ok(message_id)
    }

    /// Get a valid access token, refreshing from the best available source.
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

        // Priority 2: GCP_ACCESS_TOKEN env var (local dev / CI)
        if let Ok(token) = std::env::var("GCP_ACCESS_TOKEN") {
            if !token.is_empty() {
                *cache = Some(CachedToken {
                    token: token.clone(),
                    // Assume the manually-set token expires in 1 hour
                    expires_at: Instant::now() + Duration::from_secs(3600),
                });
                return Ok(token);
            }
        }

        Err(TradingError::Gcp(
            "No GCP credentials found. Set GCP_ACCESS_TOKEN or run on GCE/Cloud Run. \
             See First Run section in CLAUDE.md."
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
