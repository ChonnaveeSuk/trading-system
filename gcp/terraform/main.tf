# trading-system/gcp/terraform/main.tf
#
# All GCP resources for QuantAI Trading System.
# Region: asia-southeast1 (Singapore — closest to Thailand).
#
# Cost estimate (paper trading phase): ~$0–1/month
#   BigQuery: 10GB storage free, 1TB query free/month
#   Pub/Sub:  10GB free/month
#   Secret Manager: 6 secrets free, 10K accesses free/month
#   Cloud Storage: 5GB free
#   Cloud Run: 2M requests free/month
#
# Apply:  terraform init && terraform apply -var-file=paper.tfvars
# Destroy: terraform destroy -var-file=paper.tfvars

terraform {
  required_version = ">= 1.7"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }

  # TODO: configure GCS backend for remote state before going live
  # backend "gcs" {
  #   bucket = "quantai-terraform-state"
  #   prefix = "trading-system"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# ── Enable required APIs ───────────────────────────────────────────────────────
locals {
  required_apis = [
    "bigquery.googleapis.com",
    "pubsub.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
    "run.googleapis.com",
    "aiplatform.googleapis.com",
    "cloudscheduler.googleapis.com",
    "monitoring.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each = toset(local.required_apis)
  service  = each.value

  disable_on_destroy = false
}

# ── Service Account ────────────────────────────────────────────────────────────
resource "google_service_account" "quantai" {
  account_id   = "quantai-trading"
  display_name = "QuantAI Trading System"
  description  = "Service account for QuantAI execution engine and strategy layer"
}

# ── Secret Manager ─────────────────────────────────────────────────────────────
# Secrets are created here (empty). Values are set manually via gcloud CLI.
# NEVER store actual secret values in Terraform state.

resource "google_secret_manager_secret" "trading_mode" {
  secret_id = "trading-mode"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "ibkr_paper_port" {
  secret_id = "ibkr-paper-port"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "postgres_password" {
  secret_id = "quantai-postgres-password"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "ibkr_account_id" {
  secret_id = "ibkr-account-id"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "ibkr_paper_host" {
  secret_id = "ibkr-paper-host"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# Grant service account access to read secrets
resource "google_secret_manager_secret_iam_member" "sa_secret_access" {
  for_each = toset([
    google_secret_manager_secret.trading_mode.id,
    google_secret_manager_secret.ibkr_paper_port.id,
    google_secret_manager_secret.postgres_password.id,
    google_secret_manager_secret.ibkr_account_id.id,
    google_secret_manager_secret.ibkr_paper_host.id,
  ])
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.quantai.email}"
}

# ── Pub/Sub Topics ────────────────────────────────────────────────────────────
# Rust execution engine publishes here (fire-and-forget, never in hot path).

resource "google_pubsub_topic" "fills" {
  name = "quantai-fills"
  message_retention_duration = "86400s" # 24h retention
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic" "ticks" {
  name = "quantai-ticks"
  message_retention_duration = "3600s" # 1h — high volume, short retention
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic" "signals" {
  name = "quantai-signals"
  message_retention_duration = "86400s"
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic" "risk_events" {
  name = "quantai-risk-events"
  message_retention_duration = "604800s" # 7 days — audit trail
  depends_on = [google_project_service.apis]
}

# Dead-letter topic for failed messages
resource "google_pubsub_topic" "dead_letter" {
  name = "quantai-dead-letter"
  message_retention_duration = "604800s"
  depends_on = [google_project_service.apis]
}

# ── BigQuery Dataset ──────────────────────────────────────────────────────────
resource "google_bigquery_dataset" "quantai" {
  dataset_id    = var.bigquery_dataset_id
  friendly_name = "QuantAI Trading System"
  description   = "Historical trade archive, OHLCV, signals, and risk events"
  location      = var.region

  default_table_expiration_ms    = null # No auto-expiration for audit tables
  default_partition_expiration_ms = null

  depends_on = [google_project_service.apis]
}

# ── BigQuery Tables (schemas loaded from JSON files) ─────────────────────────
resource "google_bigquery_table" "trades" {
  dataset_id          = google_bigquery_dataset.quantai.dataset_id
  table_id            = "trades"
  deletion_protection = true

  schema = file("${path.module}/../bigquery/schema/trades.json")

  time_partitioning {
    type  = "DAY"
    field = "timestamp"
  }

  clustering = ["symbol", "strategy_id"]
}

resource "google_bigquery_table" "ohlcv" {
  dataset_id          = google_bigquery_dataset.quantai.dataset_id
  table_id            = "ohlcv"
  deletion_protection = true

  schema = file("${path.module}/../bigquery/schema/ohlcv.json")

  time_partitioning {
    type  = "DAY"
    field = "timestamp"
  }

  clustering = ["symbol"]
}

resource "google_bigquery_table" "signals" {
  dataset_id          = google_bigquery_dataset.quantai.dataset_id
  table_id            = "signals"
  deletion_protection = true

  schema = file("${path.module}/../bigquery/schema/signals.json")

  time_partitioning {
    type  = "DAY"
    field = "created_at"
  }

  clustering = ["symbol", "strategy_id"]
}

# ── Cloud Storage ─────────────────────────────────────────────────────────────
resource "google_storage_bucket" "backups" {
  name          = var.gcs_backup_bucket
  location      = var.region
  force_destroy = false # Safety: never auto-delete backups

  lifecycle_rule {
    action { type = "Delete" }
    condition { age = 90 } # Keep PostgreSQL backups for 90 days
  }

  lifecycle_rule {
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
    condition { age = 30 } # Move to NEARLINE after 30 days (cost saving)
  }

  versioning { enabled = true }

  depends_on = [google_project_service.apis]
}

# ── IAM bindings ──────────────────────────────────────────────────────────────
resource "google_bigquery_dataset_iam_member" "sa_bq_editor" {
  dataset_id = google_bigquery_dataset.quantai.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.quantai.email}"
}

resource "google_pubsub_topic_iam_member" "sa_pubsub_publisher" {
  for_each = toset([
    google_pubsub_topic.fills.id,
    google_pubsub_topic.ticks.id,
    google_pubsub_topic.signals.id,
    google_pubsub_topic.risk_events.id,
  ])
  topic  = each.value
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.quantai.email}"
}

resource "google_storage_bucket_iam_member" "sa_gcs_writer" {
  bucket = google_storage_bucket.backups.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.quantai.email}"
}

# ── Pub/Sub → BigQuery Subscriptions ─────────────────────────────────────────
# Native Pub/Sub BigQuery subscriptions stream messages directly into BigQuery
# tables without Dataflow or Cloud Functions. ADR-002: GCP always downstream.
#
# Message format: JSON matching the BigQuery table schema.
# The Rust publisher sends fill.to_bq_json() which matches trades.json schema.

# Grant the Pub/Sub service agent permission to write to BigQuery
data "google_project" "project" {}

resource "google_project_iam_member" "pubsub_bq_editor" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_project_iam_member" "pubsub_bq_viewer" {
  project = var.project_id
  role    = "roles/bigquery.metadataViewer"
  member  = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# Fills → BigQuery trades table (primary audit trail)
resource "google_pubsub_subscription" "fills_to_bq" {
  name  = "quantai-fills-to-bigquery"
  topic = google_pubsub_topic.fills.name

  bigquery_config {
    table            = "${var.project_id}.${google_bigquery_dataset.quantai.dataset_id}.${google_bigquery_table.trades.table_id}"
    use_table_schema = true   # Map JSON fields to BQ schema automatically
    write_metadata   = false
  }

  # Dead-letter: route failed messages to DLQ topic after 5 attempts
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = 5
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }

  depends_on = [
    google_project_iam_member.pubsub_bq_editor,
    google_project_iam_member.pubsub_bq_viewer,
    google_bigquery_table.trades,
  ]
}

# ── Pub/Sub Subscriptions for pull-based consumers (strategy layer) ───────────

# Signals subscription — Python strategy layer subscribes here to receive
# processed signals for backtesting validation (Phase 2)
resource "google_pubsub_subscription" "signals_pull" {
  name  = "quantai-signals-strategy"
  topic = google_pubsub_topic.signals.name

  ack_deadline_seconds = 60

  retry_policy {
    minimum_backoff = "5s"
    maximum_backoff = "60s"
  }
}

# Risk events subscription — monitoring / alerting pipeline (Phase 3)
resource "google_pubsub_subscription" "risk_events_pull" {
  name  = "quantai-risk-events-monitor"
  topic = google_pubsub_topic.risk_events.name

  ack_deadline_seconds = 60

  retry_policy {
    minimum_backoff = "5s"
    maximum_backoff = "60s"
  }
}

# ── Monitoring: Alert on HALT risk events ────────────────────────────────────
# Phase 3: wire this to PagerDuty / email. Placeholder notification channel.
resource "google_monitoring_alert_policy" "halt_alert" {
  display_name = "QuantAI Trading Halt"
  combiner     = "OR"

  conditions {
    display_name = "Risk halt event published"
    condition_threshold {
      filter          = "resource.type=\"pubsub_topic\" AND resource.labels.topic_id=\"quantai-risk-events\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_COUNT"
      }
    }
  }

  notification_channels = [] # Phase 3: add email/PagerDuty channel IDs here
  depends_on            = [google_project_service.apis]
}
