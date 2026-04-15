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

output "cloud_sql" {
  description = "Cloud SQL instance details"
  value = {
    instance_name       = google_sql_database_instance.postgres.name
    connection_name     = google_sql_database_instance.postgres.connection_name
    public_ip           = google_sql_database_instance.postgres.public_ip_address
    database            = google_sql_database.quantai.name
    user                = google_sql_user.quantai.name
    socket_path         = "/cloudsql/${google_sql_database_instance.postgres.connection_name}"
    database_url_format = "postgresql://quantai:PASSWORD@/quantai?host=/cloudsql/${google_sql_database_instance.postgres.connection_name}"
  }
}

output "cloud_sql_local_proxy_commands" {
  description = "Commands to connect to Cloud SQL from WSL for local dev or migration"
  value       = <<-EOT
    # Get the Cloud SQL password
    gcloud secrets versions access latest --secret=cloud-sql-quantai-password --project=${var.project_id}

    # Start proxy (port 5434 — avoids collision with local Docker on 5432)
    /tmp/cloud-sql-proxy ${var.project_id}:${var.region}:quantai-postgres --port 5434 &

    # Connect
    psql "postgresql://quantai:PASSWORD@127.0.0.1:5434/quantai"

    # Run migration from local Docker → Cloud SQL
    bash scripts/migrate_to_cloud_sql.sh
  EOT
}

output "artifact_registry_url" {
  description = "Artifact Registry URL for Docker images"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.quantai.repository_id}"
}

output "cloud_run_jobs" {
  description = "Cloud Run Job names"
  value = {
    daily_runner = google_cloud_run_v2_job.daily_runner.name
    backup       = google_cloud_run_v2_job.backup.name
  }
}

output "workload_identity_provider" {
  description = "WIF provider resource name — set as GCP_WIF_PROVIDER in GitHub Actions secrets"
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "cloud_run_manual_trigger_commands" {
  description = "Commands to manually trigger Cloud Run Jobs"
  value       = <<-EOT
    # Trigger daily runner immediately
    gcloud run jobs execute quantai-daily-runner --region ${var.region} --project ${var.project_id}

    # Trigger backup immediately
    gcloud run jobs execute quantai-backup --region ${var.region} --project ${var.project_id}

    # Tail logs (last execution)
    gcloud run jobs executions list --job quantai-daily-runner --region ${var.region} --limit 1
    gcloud logging read 'resource.type="cloud_run_job" resource.labels.job_name="quantai-daily-runner"' --limit 50 --format="value(textPayload)"
  EOT
}

output "populate_database_url_command" {
  description = "Set DATABASE_URL secret after provisioning your PostgreSQL"
  sensitive   = true
  value       = <<-EOT
    # Option A — Cloud SQL (recommended):
    #   1. Create Cloud SQL instance in GCP Console or via Terraform
    #   2. Note INSTANCE_CONNECTION_NAME (PROJECT:REGION:INSTANCE)
    echo -n "postgres://quantai:PASSWORD@/quantai?host=/cloudsql/PROJECT:REGION:INSTANCE" \
      | gcloud secrets versions add database-url --data-file=- --project=${var.project_id}

    # Option B — Direct IP (e.g., Compute Engine VM running PostgreSQL):
    echo -n "postgres://quantai:PASSWORD@EXTERNAL_IP:5432/quantai" \
      | gcloud secrets versions add database-url --data-file=- --project=${var.project_id}
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
