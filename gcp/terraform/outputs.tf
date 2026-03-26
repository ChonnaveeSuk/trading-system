# trading-system/gcp/terraform/outputs.tf

output "service_account_email" {
  description = "Service account email to use in application config"
  value       = google_service_account.quantai.email
}

output "pubsub_topics" {
  description = "Pub/Sub topic names for Rust configuration"
  value = {
    fills       = google_pubsub_topic.fills.id
    ticks       = google_pubsub_topic.ticks.id
    signals     = google_pubsub_topic.signals.id
    risk_events = google_pubsub_topic.risk_events.id
  }
}

output "bigquery_dataset" {
  description = "BigQuery dataset ID"
  value       = google_bigquery_dataset.quantai.dataset_id
}

output "gcs_backup_bucket" {
  description = "GCS backup bucket name"
  value       = google_storage_bucket.backups.name
}

output "startup_verification_commands" {
  description = "Commands to run at each session startup"
  value = <<-EOT
    # Verify paper mode (must print: paper)
    gcloud secrets versions access latest --secret="trading-mode" --project=${var.project_id}

    # Verify IBKR port (must print: 7497)
    gcloud secrets versions access latest --secret="ibkr-paper-port" --project=${var.project_id}
  EOT
}
