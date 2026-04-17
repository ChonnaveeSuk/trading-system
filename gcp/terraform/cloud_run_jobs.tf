# trading-system/gcp/terraform/cloud_run_jobs.tf
#
# Cloud Run Jobs + Cloud Scheduler for the QuantAI daily trading loop.
# Replaces WSL cron as the authoritative scheduler — runs even when Windows is off.
#
# Resources:
#   - Artifact Registry repository (Docker images)
#   - Workload Identity Pool + Provider (GitHub Actions keyless auth)
#   - Cloud Run Job: quantai-daily-runner  (Mon–Fri 22:00 UTC)
#   - Cloud Run Job: quantai-backup        (daily 02:00 UTC)
#   - Cloud Scheduler: triggers for both jobs
#   - IAM: SA bindings for Cloud Run + Cloud Scheduler
#   - Secret Manager: database-url secret (populated by cloud_sql.tf)
#
# Cloud SQL connectivity:
#   Both jobs mount the Cloud SQL Auth Proxy socket at /cloudsql/.
#   DATABASE_URL (from Secret Manager) uses the Unix socket format:
#     postgresql://quantai:PASSWORD@/quantai?host=/cloudsql/PROJECT:REGION:INSTANCE
#
# Apply:
#   cd gcp/terraform && terraform apply -var-file=paper.tfvars
#
# Manual trigger:
#   gcloud run jobs execute quantai-daily-runner --region asia-southeast1
#   gcloud run jobs execute quantai-backup        --region asia-southeast1

# ── Enable additional APIs ─────────────────────────────────────────────────────
locals {
  cloud_run_apis = [
    "artifactregistry.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
    "cloudscheduler.googleapis.com",
  ]
}

resource "google_project_service" "cloud_run_apis" {
  for_each = toset(local.cloud_run_apis)
  service  = each.value

  disable_on_destroy = false
}

# ── Artifact Registry ─────────────────────────────────────────────────────────
resource "google_artifact_registry_repository" "quantai" {
  location      = var.region
  repository_id = "quantai"
  description   = "Docker images for QuantAI Cloud Run Jobs"
  format        = "DOCKER"

  depends_on = [google_project_service.cloud_run_apis]
}

# Allow the quantai SA to push/pull images
resource "google_artifact_registry_repository_iam_member" "sa_ar_writer" {
  location   = google_artifact_registry_repository.quantai.location
  repository = google_artifact_registry_repository.quantai.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.quantai.email}"
}

# ── Secret: DATABASE_URL ──────────────────────────────────────────────────────
# Stores the PostgreSQL connection string for Cloud Run Jobs.
# Set the value after terraform apply:
#   echo -n "postgres://quantai:PASSWORD@HOST:5432/quantai" \
#     | gcloud secrets versions add database-url --data-file=- --project=quantai-trading-paper
#
# For Cloud SQL (recommended):
#   echo -n "postgres://quantai:PASSWORD@/quantai?host=/cloudsql/PROJECT:REGION:INSTANCE" \
#     | gcloud secrets versions add database-url --data-file=- --project=quantai-trading-paper

resource "google_secret_manager_secret" "database_url" {
  secret_id = "database-url"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "sa_database_url" {
  secret_id = google_secret_manager_secret.database_url.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.quantai.email}"
}

# Also need access to alpaca secrets and trading-mode for the runner job
resource "google_secret_manager_secret_iam_member" "sa_alpaca_key" {
  secret_id = "alpaca-api-key"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.quantai.email}"

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "sa_alpaca_secret" {
  secret_id = "alpaca-secret-key"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.quantai.email}"

  depends_on = [google_project_service.apis]
}

# ── Telegram alert secrets ────────────────────────────────────────────────────

resource "google_secret_manager_secret" "telegram_bot_token" {
  secret_id = "telegram-bot-token"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "telegram_chat_id" {
  secret_id = "telegram-chat-id"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "sa_telegram_bot_token" {
  secret_id = google_secret_manager_secret.telegram_bot_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.quantai.email}"
}

resource "google_secret_manager_secret_iam_member" "sa_telegram_chat_id" {
  secret_id = google_secret_manager_secret.telegram_chat_id.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.quantai.email}"
}

# ── IAM: Cloud Run Jobs SA permissions ────────────────────────────────────────
# Cloud Run Jobs run as the existing quantai SA.
# Grant it the Cloud Run Job Invoker role on itself so Scheduler can trigger it.

resource "google_project_iam_member" "sa_run_jobs_runner" {
  project = var.project_id
  role    = "roles/run.jobsExecutorWithOverrides"
  member  = "serviceAccount:${google_service_account.quantai.email}"
}

resource "google_project_iam_member" "sa_storage_viewer" {
  project = var.project_id
  role    = "roles/storage.objectViewer"
  member  = "serviceAccount:${google_service_account.quantai.email}"
}

# ── Cloud Run Job: quantai-daily-runner ───────────────────────────────────────
resource "google_cloud_run_v2_job" "daily_runner" {
  name     = "quantai-daily-runner"
  location = var.region

  template {
    task_count = 1

    template {
      service_account = google_service_account.quantai.email
      timeout         = "3600s" # 1 hour max

      containers {
        image = var.runner_image

        # Default entry point runs run_daily.sh (set in Dockerfile CMD)

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        # Static env vars
        env {
          name  = "TRADING_MODE"
          value = "paper"
        }
        env {
          name  = "GCP_PROJECT_ID"
          value = var.project_id
        }
        env {
          name  = "GCS_BACKUP_BUCKET"
          value = "quantai-backups-${var.project_id}"
        }
        env {
          # Cloud Run must use Alpaca — Yahoo Finance blocks GCP IPs
          name  = "ALPACA_FETCHER"
          value = "1"
        }
        env {
          # Direct Alpaca REST for order submission — Rust OMS cannot run on Cloud Run
          # (no Redis available; Rust main.rs also uses PaperBroker not AlpacaBroker)
          name  = "ALPACA_DIRECT"
          value = "1"
        }
        env {
          # Start Cloud SQL before job, stop after — saves ~$7.55/month (~70% of SQL cost)
          name  = "MANAGE_CLOUD_SQL"
          value = "1"
        }

        # DATABASE_URL from Secret Manager (Cloud SQL Unix-socket format)
        env {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.database_url.secret_id
              version = "latest"
            }
          }
        }

        # Telegram alert credentials from Secret Manager
        env {
          name = "TELEGRAM_BOT_TOKEN"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.telegram_bot_token.secret_id
              version = "latest"
            }
          }
        }
        env {
          name = "TELEGRAM_CHAT_ID"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.telegram_chat_id.secret_id
              version = "latest"
            }
          }
        }

        # Mount Cloud SQL Auth Proxy socket
        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
      }

      # Cloud SQL Auth Proxy socket volume
      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = ["${var.project_id}:${var.region}:${google_sql_database_instance.postgres.name}"]
        }
      }

      max_retries = 1
    }
  }

  depends_on = [
    google_artifact_registry_repository.quantai,
    google_secret_manager_secret.database_url,
    google_secret_manager_secret.telegram_bot_token,
    google_secret_manager_secret.telegram_chat_id,
    google_sql_database_instance.postgres,
    google_project_service.cloud_run_apis,
    google_project_iam_member.sa_cloudsql_client,
  ]

  lifecycle {
    ignore_changes = [
      # Allow GitHub Actions to update the image without Terraform drift
      template[0].template[0].containers[0].image,
    ]
  }
}

# ── Cloud Run Job: quantai-backup ─────────────────────────────────────────────
resource "google_cloud_run_v2_job" "backup" {
  name     = "quantai-backup"
  location = var.region

  template {
    task_count = 1

    template {
      service_account = google_service_account.quantai.email
      timeout         = "1800s" # 30 minutes

      containers {
        image   = var.runner_image
        command = ["bash", "/app/scripts/backup_postgres.sh"]

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        env {
          name  = "GCP_PROJECT_ID"
          value = var.project_id
        }
        env {
          name  = "GCS_BACKUP_BUCKET"
          value = "quantai-backups-${var.project_id}"
        }
        env {
          # Start Cloud SQL before backup, stop after — saves ~$7.55/month
          name  = "MANAGE_CLOUD_SQL"
          value = "1"
        }

        env {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.database_url.secret_id
              version = "latest"
            }
          }
        }

        # Mount Cloud SQL Auth Proxy socket
        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
      }

      # Cloud SQL Auth Proxy socket volume
      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = ["${var.project_id}:${var.region}:${google_sql_database_instance.postgres.name}"]
        }
      }

      max_retries = 2
    }
  }

  depends_on = [
    google_artifact_registry_repository.quantai,
    google_secret_manager_secret.database_url,
    google_sql_database_instance.postgres,
    google_project_service.cloud_run_apis,
    google_project_iam_member.sa_cloudsql_client,
  ]

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

# ── Cloud Scheduler ───────────────────────────────────────────────────────────
# Scheduler needs a SA with permission to trigger Cloud Run Jobs.
# We use the existing quantai SA which already has run.jobsExecutorWithOverrides.

resource "google_cloud_scheduler_job" "daily_runner" {
  name      = "quantai-daily-schedule"
  region    = var.region
  schedule  = var.daily_runner_schedule
  time_zone = var.cloud_scheduler_timezone

  description = "Triggers quantai-daily-runner Mon–Fri at 22:00 UTC (after US market close)"

  http_target {
    http_method = "POST"
    uri = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.daily_runner.name}:run"

    oauth_token {
      service_account_email = google_service_account.quantai.email
    }
  }

  retry_config {
    retry_count          = 1
    max_retry_duration   = "0s"
    min_backoff_duration = "5s"
    max_backoff_duration = "3600s"
    max_doublings        = 5
  }

  depends_on = [
    google_cloud_run_v2_job.daily_runner,
    google_project_iam_member.sa_run_jobs_runner,
    google_project_service.cloud_run_apis,
  ]
}

resource "google_cloud_scheduler_job" "backup" {
  name      = "quantai-backup-schedule"
  region    = var.region
  schedule  = var.backup_schedule
  time_zone = var.cloud_scheduler_timezone

  description = "Triggers quantai-backup daily at 02:00 UTC"

  http_target {
    http_method = "POST"
    uri = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.backup.name}:run"

    oauth_token {
      service_account_email = google_service_account.quantai.email
    }
  }

  retry_config {
    retry_count          = 2
    max_retry_duration   = "0s"
    min_backoff_duration = "5s"
    max_backoff_duration = "3600s"
    max_doublings        = 5
  }

  depends_on = [
    google_cloud_run_v2_job.backup,
    google_project_iam_member.sa_run_jobs_runner,
    google_project_service.cloud_run_apis,
  ]
}

# ── Workload Identity Federation (GitHub Actions keyless auth) ─────────────────
# Allows GitHub Actions to push images and update Cloud Run Jobs
# without storing a service account JSON key.

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github-pool"
  display_name              = "GitHub Actions Pool"
  description               = "WIF pool for GitHub Actions CI/CD"

  depends_on = [google_project_service.cloud_run_apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-provider"
  display_name                       = "GitHub OIDC Provider"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.actor"      = "assertion.actor"
    "attribute.repository" = "assertion.repository"
  }

  attribute_condition = "assertion.repository == '${var.github_repo}'"
}

# Allow GitHub Actions (from the specific repo) to impersonate the quantai SA
resource "google_service_account_iam_member" "github_wif_sa_binding" {
  service_account_id = google_service_account.quantai.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repo}"
}

# Allow quantai SA to push to Artifact Registry (needed by GitHub Actions acting as quantai SA)
resource "google_project_iam_member" "sa_ar_admin" {
  project = var.project_id
  role    = "roles/artifactregistry.repoAdmin"
  member  = "serviceAccount:${google_service_account.quantai.email}"
}

# Allow quantai SA to update Cloud Run Jobs
resource "google_project_iam_member" "sa_run_developer" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.quantai.email}"
}

# Allow Cloud Run Jobs to start/stop Cloud SQL (for cost optimization)
# Needed by MANAGE_CLOUD_SQL=1 — scripts call gcloud sql instances patch
resource "google_project_iam_member" "sa_cloudsql_admin" {
  project = var.project_id
  role    = "roles/cloudsql.admin"
  member  = "serviceAccount:${google_service_account.quantai.email}"
}
