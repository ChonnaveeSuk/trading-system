project_id          = "quantai-trading-paper"
environment         = "paper"
region              = "asia-southeast1"
bigquery_dataset_id = "quantai_trading"
gcs_backup_bucket   = "quantai-backups-quantai-trading-paper"
alert_email         = "chonnaveesukyao@gmail.com"

# Cloud Run Jobs
runner_image             = "asia-southeast1-docker.pkg.dev/quantai-trading-paper/quantai/runner:latest"
cloud_scheduler_timezone = "UTC"
daily_runner_schedule    = "0 22 * * 1-5"
backup_schedule          = "0 2 * * *"
github_repo              = "QuantAI/trading-system"
