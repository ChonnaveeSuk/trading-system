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
