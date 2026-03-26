// trading-system/core/src/gcp/mod.rs
//
// GCP integration from the Rust execution engine.
// ALL GCP calls are fire-and-forget: they NEVER block the order execution path.
//
// Phase 1 will implement:
//   - pubsub.rs → Async Pub/Sub publisher (fills + ticks → BigQuery pipeline)
//
// Architecture rule: Pub/Sub publish runs in a SEPARATE tokio task.
// The hot path (order → risk → broker) NEVER awaits GCP.
//
//   tokio::spawn(async move {
//       if let Err(e) = pubsub.publish_fill(&fill).await {
//           tracing::warn!("Pub/Sub publish failed (non-fatal): {e}");
//           // GCP failure NEVER halts trading
//       }
//   });

pub mod pubsub;

/// GCP project configuration.
#[derive(Debug, Clone)]
pub struct GcpConfig {
    pub project_id: String,
    pub fills_topic: String,
    pub ticks_topic: String,
    pub signals_topic: String,
    pub region: String, // default: asia-southeast1
}

impl GcpConfig {
    pub fn from_env() -> Result<Self, crate::error::TradingError> {
        // TODO Phase 1: load from GCP Secret Manager instead of env
        let project_id = std::env::var("GCP_PROJECT_ID").map_err(|_| {
            crate::error::TradingError::Config("GCP_PROJECT_ID not set".into())
        })?;
        Ok(Self {
            fills_topic: format!("projects/{project_id}/topics/quantai-fills"),
            ticks_topic: format!("projects/{project_id}/topics/quantai-ticks"),
            signals_topic: format!("projects/{project_id}/topics/quantai-signals"),
            region: "asia-southeast1".into(),
            project_id,
        })
    }
}
