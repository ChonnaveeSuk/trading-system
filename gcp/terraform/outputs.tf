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

output "fills_bq_subscription" {
  description = "Pub/Sub subscription streaming fills to BigQuery"
  value       = google_pubsub_subscription.fills_to_bq.id
}

output "populate_secrets_commands" {
  description = "Run these once after terraform apply to set secret values"
  sensitive   = true
  value = <<-EOT
    PROJECT=${var.project_id}

    echo -n "paper"      | gcloud secrets versions add trading-mode         --data-file=- --project=$PROJECT
    echo -n "7497"       | gcloud secrets versions add ibkr-paper-port      --data-file=- --project=$PROJECT
    echo -n "127.0.0.1"  | gcloud secrets versions add ibkr-paper-host      --data-file=- --project=$PROJECT
    echo -n "PLACEHOLDER" | gcloud secrets versions add ibkr-account-id     --data-file=- --project=$PROJECT

    # Generate and store a strong postgres password:
    PG_PASS=$(python3 -c "import secrets,string; print(secrets.token_urlsafe(32))")
    echo -n "$PG_PASS" | gcloud secrets versions add quantai-postgres-password --data-file=- --project=$PROJECT
    echo "Postgres password stored. Update .env POSTGRES_PASSWORD=$PG_PASS"
  EOT
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
