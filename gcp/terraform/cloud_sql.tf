# trading-system/gcp/terraform/cloud_sql.tf
#
# Cloud SQL PostgreSQL 16 for QuantAI Cloud Run Jobs.
# Replaces WSL Docker PostgreSQL as the persistent store for Cloud Run.
# Local Docker PostgreSQL is retained for local development.
#
# Cost estimate (asia-southeast1, paper trading):
#   db-f1-micro instance:  ~$7.67/month (shared-core, 0.6 GB RAM)
#   Storage (10 GB SSD):   ~$1.70/month
#   Backups (7 days):      ~$0.10/month (incremental)
#   Total:                 ~$9–10/month
#
# Connectivity model:
#   Cloud Run Jobs  → Cloud SQL Auth Proxy socket → quantai-postgres
#   (unix socket mounted at /cloudsql/PROJECT:REGION:INSTANCE)
#   No VPC connector required; proxy route is internal to GCP.
#   Public IP is enabled for the proxy to function; authorised_networks
#   is empty so direct TCP from the internet is blocked.
#
# Local dev access:
#   cloud-sql-proxy quantai-trading-paper:asia-southeast1:quantai-postgres --port 5434
#   psql "postgresql://quantai:PASSWORD@127.0.0.1:5434/quantai"
#
# Apply:
#   cd gcp/terraform && terraform init && terraform apply -var-file=paper.tfvars

# ── Enable Cloud SQL Admin API ─────────────────────────────────────────────────
resource "google_project_service" "sqladmin" {
  service            = "sqladmin.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "servicenetworking" {
  service            = "servicenetworking.googleapis.com"
  disable_on_destroy = false
}

# ── Random password for Cloud SQL user ────────────────────────────────────────
resource "random_password" "cloud_sql" {
  length           = 32
  special          = false # avoid URL-encoding issues in connection strings
  override_special = ""
}

# ── Cloud SQL Instance ────────────────────────────────────────────────────────
resource "google_sql_database_instance" "postgres" {
  name             = "quantai-postgres"
  database_version = "POSTGRES_16"
  region           = var.region

  deletion_protection = false # paper trading — safe to delete for cost saving

  settings {
    tier              = "db-f1-micro"
    availability_type = "ZONAL" # HA not needed for paper trading (REGIONAL doubles cost)
    disk_size         = 10      # GB
    disk_type         = "PD_SSD"
    disk_autoresize   = true
    disk_autoresize_limit = 50  # cap at 50 GB; alert before hitting this

    ip_configuration {
      ipv4_enabled = true  # required: Cloud SQL Auth Proxy needs this to connect
      ssl_mode     = "ENCRYPTED_ONLY"
      # authorized_networks is intentionally empty:
      # only the Cloud SQL Auth Proxy can reach the instance — no raw TCP from internet
    }

    backup_configuration {
      enabled    = true
      start_time = "03:00" # UTC — after backup_postgres.sh GCS dump at 02:00

      backup_retention_settings {
        retained_backups = 7
        retention_unit   = "COUNT"
      }

      transaction_log_retention_days = 7
      point_in_time_recovery_enabled = false # not needed for paper trading
    }

    maintenance_window {
      day          = 7  # Sunday
      hour         = 4  # 04:00 UTC
      update_track = "stable"
    }

    database_flags {
      name  = "max_connections"
      value = "50" # f1-micro RAM limit; Cloud Run Jobs use a few connections each
    }

    database_flags {
      name  = "log_min_duration_statement"
      value = "1000" # log queries >1s for debugging
    }
  }

  depends_on = [
    google_project_service.sqladmin,
    google_project_service.servicenetworking,
  ]
}

# ── Database ──────────────────────────────────────────────────────────────────
resource "google_sql_database" "quantai" {
  name     = "quantai"
  instance = google_sql_database_instance.postgres.name
}

# ── User ──────────────────────────────────────────────────────────────────────
resource "google_sql_user" "quantai" {
  name     = "quantai"
  instance = google_sql_database_instance.postgres.name
  password = random_password.cloud_sql.result
}

# ── Secrets ───────────────────────────────────────────────────────────────────
# cloud-sql-password: raw password (used by migration script + local proxy dev)
resource "google_secret_manager_secret" "cloud_sql_password" {
  secret_id = "cloud-sql-quantai-password"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "cloud_sql_password" {
  secret      = google_secret_manager_secret.cloud_sql_password.id
  secret_data = random_password.cloud_sql.result
}

# database-url: full connection string for Cloud Run Jobs (Unix socket format)
# This updates the existing database-url secret that was seeded with a placeholder.
resource "google_secret_manager_secret_version" "database_url_cloud_sql" {
  secret = google_secret_manager_secret.database_url.id

  secret_data = join("", [
    "postgresql://quantai:",
    random_password.cloud_sql.result,
    "@/quantai?host=/cloudsql/",
    var.project_id,
    ":",
    var.region,
    ":",
    google_sql_database_instance.postgres.name,
  ])

  lifecycle {
    # Secret versions are immutable — on re-apply Terraform will see no drift
    # and leave the version untouched unless the password resource is replaced.
    ignore_changes = [secret_data]
  }
}

# Grant SA permission to access the new secrets
resource "google_secret_manager_secret_iam_member" "sa_cloud_sql_password" {
  secret_id = google_secret_manager_secret.cloud_sql_password.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.quantai.email}"
}

# ── IAM: Cloud SQL client role ────────────────────────────────────────────────
# Required so the Cloud SQL Auth Proxy (running inside Cloud Run) can connect.
resource "google_project_iam_member" "sa_cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.quantai.email}"
}
