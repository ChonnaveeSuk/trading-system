# trading-system/gcp/terraform/variables.tf

variable "project_id" {
  description = "GCP project ID"
  type        = string
  default     = "quantai-trading-paper"
}

variable "region" {
  description = "Default GCP region. Singapore is closest to Thailand."
  type        = string
  default     = "asia-southeast1"
}

variable "environment" {
  description = "Deployment environment. Must be 'paper' until Phase 4 authorization."
  type        = string
  default     = "paper"

  validation {
    condition     = contains(["paper", "live"], var.environment)
    error_message = "environment must be 'paper' or 'live'."
  }
}

variable "bigquery_dataset_id" {
  description = "BigQuery dataset name for all QuantAI tables."
  type        = string
  default     = "quantai_trading"
}

variable "gcs_backup_bucket" {
  description = "GCS bucket for PostgreSQL daily backups + ML model artifacts."
  type        = string
  # Set per-project: e.g., "quantai-backups-<project_id>"
}

variable "alert_email" {
  description = "Email address for HALT alerts (drawdown, daily loss)."
  type        = string
}

# ── Cloud Run Jobs ─────────────────────────────────────────────────────────────

variable "runner_image" {
  description = "Full Artifact Registry image path for the daily runner job."
  type        = string
  # Set after first docker push, e.g.:
  # asia-southeast1-docker.pkg.dev/quantai-trading-paper/quantai/runner:latest
  default     = "asia-southeast1-docker.pkg.dev/quantai-trading-paper/quantai/runner:latest"
}

variable "cloud_scheduler_timezone" {
  description = "IANA timezone for Cloud Scheduler jobs."
  type        = string
  default     = "UTC"
}

variable "daily_runner_schedule" {
  description = "Cron expression for the daily strategy run (UTC)."
  type        = string
  default     = "0 22 * * 1-5" # Mon–Fri 22:00 UTC (05:00 Thai, after US market close)
}

variable "backup_schedule" {
  description = "Cron expression for the PostgreSQL backup job (UTC)."
  type        = string
  default     = "0 2 * * *" # Daily 02:00 UTC
}

variable "github_repo" {
  description = "GitHub repo for Workload Identity Federation (org/repo format)."
  type        = string
  default     = "QuantAI/trading-system"
}
