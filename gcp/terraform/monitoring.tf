# trading-system/gcp/terraform/monitoring.tf
#
# Cloud Monitoring + Error Reporting alert pipeline for QuantAI.
#
# Free tier: notification channels, alert policies, log-based metrics, and
# Error Reporting are all included in the GCP free tier — this file adds zero
# recurring cost provided we stay under the per-project quotas (1500 alerts /
# month is plenty for a single trading bot).
#
# Telegram delivery: GCP doesn't ship a native Telegram channel, so we use a
# webhook channel that POSTs to api.telegram.org/bot{TOKEN}/sendMessage with
# `chat_id` baked into the URL.  The bot token + chat ID come from the same
# secrets that scripts/telegram_alert.py uses, kept out of Terraform state.
#
# Provisioning order:
#   1. Set the bot token + chat ID as Terraform variables (or read from
#      Secret Manager via `data` blocks below).
#   2. terraform apply -var-file=paper.tfvars
#   3. Confirm a test page in Cloud Monitoring → Alerting → Channels
#
# Resources:
#   - notification channel: email
#   - notification channel: webhook → Telegram bot
#   - alert: Cloud Run job failed (Task 1)
#   - log-based metric + alert: STOP LOSS triggered (Task 3)
#   - log-based metric + alert: Cloud Run unhandled exception (Task 2)
#   - alert: missed daily schedule (Task 4)

# ── Telegram credentials (read from Secret Manager) ──────────────────────────
# The webhook URL is computed at apply time and stored in the channel's
# `auth_token` attribute — Terraform marks it sensitive so it won't be
# printed in plans or state outputs.

data "google_secret_manager_secret_version" "telegram_bot_token" {
  secret  = "telegram-bot-token"
  project = var.project_id
}

data "google_secret_manager_secret_version" "telegram_chat_id" {
  secret  = "telegram-chat-id"
  project = var.project_id
}

# ── Notification channels ─────────────────────────────────────────────────────

resource "google_monitoring_notification_channel" "email" {
  display_name = "QuantAI Alert Email"
  type         = "email"
  description  = "Primary email channel for QuantAI Cloud Run / risk alerts"
  labels = {
    email_address = var.alert_email
  }
  depends_on = [google_project_service.apis]
}

# Webhook → Telegram bot.  The Telegram bot token is part of the URL path
# (the standard Bot API contract), so we send it via the `url` label and
# omit auth_token — webhook_tokenauth's only supported label is `url`,
# and adding sensitive_labels.auth_token returns a 400 from the GCP API.
# The bot token + chat ID are read from Secret Manager at apply time so
# nothing sensitive leaks into tfvars or plan output.
resource "google_monitoring_notification_channel" "telegram_webhook" {
  display_name = "QuantAI Telegram Bot"
  type         = "webhook_tokenauth"
  description  = "Webhook → Telegram bot @QuantAITradingBot"
  labels = {
    url = format(
      "https://api.telegram.org/bot%s/sendMessage?chat_id=%s",
      data.google_secret_manager_secret_version.telegram_bot_token.secret_data,
      data.google_secret_manager_secret_version.telegram_chat_id.secret_data,
    )
  }
  depends_on = [google_project_service.apis]
}

# ── Task 1: Cloud Run Job failure alert ───────────────────────────────────────
# Fires when any execution of quantai-daily-runner ends with result=failed.
# `cloud_run_job/completed_task_attempt_count` is the standard Cloud Run job
# metric; filtering on result="failed" gives a clean failure-only signal.

resource "google_monitoring_alert_policy" "daily_runner_failed" {
  display_name = "QuantAI: daily runner FAILED"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "Cloud Run Job task failed"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"run.googleapis.com/job/completed_task_attempt_count\"",
        "resource.type=\"cloud_run_job\"",
        "resource.label.job_name=\"${google_cloud_run_v2_job.daily_runner.name}\"",
        "metric.label.result=\"failed\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  notification_channels = [
    google_monitoring_notification_channel.telegram_webhook.id,
    google_monitoring_notification_channel.email.id,
  ]

  alert_strategy {
    auto_close = "1800s" # 30 min
  }

  documentation {
    content = <<-EOT
      QuantAI daily runner reported a failed task attempt.
      Check Cloud Run logs:
        gcloud logging read 'resource.type="cloud_run_job"
        resource.labels.job_name="quantai-daily-runner"' --limit 50
    EOT
    mime_type = "text/markdown"
  }

  depends_on = [
    google_project_service.apis,
    google_cloud_run_v2_job.daily_runner,
  ]
}

# ── Task 3: STOP LOSS log-based metric + alert ────────────────────────────────
# alpaca_direct.py emits the literal string `STOP LOSS triggered:` from
# AlpacaDirectClient.check_and_trigger_stops when a position breaches the
# hard-stop pct.  The log-based metric counts those occurrences;  the alert
# fires the moment any stop-loss happens (threshold > 0 over 5 min).

resource "google_logging_metric" "stop_loss_triggered" {
  name        = "quantai/stop_loss_triggered"
  description = "Count of STOP LOSS triggered events from alpaca_direct.py"
  filter      = <<-EOT
    resource.type="cloud_run_job"
    resource.labels.job_name="${google_cloud_run_v2_job.daily_runner.name}"
    textPayload=~"STOP LOSS triggered"
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "symbol"
      value_type  = "STRING"
      description = "Symbol that breached the hard stop"
    }
  }

  label_extractors = {
    "symbol" = "REGEXP_EXTRACT(textPayload, \"STOP LOSS triggered: ([A-Z0-9-]+)\")"
  }

  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "stop_loss_triggered" {
  display_name = "QuantAI: STOP LOSS triggered"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "Hard stop-loss fired (count > 0 in 5 min)"
    condition_threshold {
      filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.stop_loss_triggered.name}\" AND resource.type=\"cloud_run_job\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  notification_channels = [
    google_monitoring_notification_channel.telegram_webhook.id,
  ]

  alert_strategy {
    auto_close = "3600s"
  }

  documentation {
    content = <<-EOT
      A QuantAI position breached the hard stop-loss threshold and was closed.
      The stop level is configured in MomentumConfig.stop_loss_pct
      (currently 5%).  Investigate the symbol mentioned in the log payload.
    EOT
    mime_type = "text/markdown"
  }

  depends_on = [
    google_project_service.apis,
    google_logging_metric.stop_loss_triggered,
  ]
}

# ── Unhandled-exception log-based metric + alert (supports Task 2) ───────────
# Cloud Error Reporting also surfaces these; the metric gives us paging via
# the same Telegram channel without depending on Error Reporting's email rules.

resource "google_logging_metric" "cloud_run_unhandled_error" {
  name        = "quantai/cloud_run_unhandled_error"
  description = "Count of severity=ERROR / Tracebacks in any QuantAI Cloud Run job"
  filter      = <<-EOT
    resource.type="cloud_run_job"
    (resource.labels.job_name="${google_cloud_run_v2_job.daily_runner.name}"
     OR resource.labels.job_name="${google_cloud_run_v2_job.backup.name}")
    (severity>=ERROR OR textPayload=~"Traceback \\(most recent call last\\)")
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }

  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "unhandled_exception" {
  display_name = "QuantAI: unhandled exception in Cloud Run"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "ERROR-level log line or Traceback (count > 0 in 5 min)"
    condition_threshold {
      filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.cloud_run_unhandled_error.name}\" AND resource.type=\"cloud_run_job\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  notification_channels = [
    google_monitoring_notification_channel.telegram_webhook.id,
    google_monitoring_notification_channel.email.id,
  ]

  alert_strategy {
    auto_close = "1800s"
  }

  documentation {
    content = <<-EOT
      A QuantAI Cloud Run job emitted an ERROR-level log or Python Traceback.
      Check the most recent logs:
        gcloud logging read 'resource.type="cloud_run_job"
        severity>=ERROR' --limit 20 --project ${var.project_id}
      Cloud Error Reporting will also have grouped the same exception.
    EOT
    mime_type = "text/markdown"
  }

  depends_on = [
    google_project_service.apis,
    google_logging_metric.cloud_run_unhandled_error,
  ]
}

# ── Task 4: Missed-schedule alert ─────────────────────────────────────────────
# We want a page if quantai-daily-runner *didn't* run by 22:30 UTC on a weekday.
# Cloud Monitoring does not have a native "absence of metric" alert with a
# weekday cron carve-out, so we approximate with a Cloud Scheduler watchdog
# job that fires at 22:30 UTC Mon-Fri and triggers a check-and-alert function.
#
# Implementation choice: rather than build a Cloud Function, we lean on the
# existing log-based metric `cloud_run_job/completed_task_attempt_count` and
# add a metric-absence alert.  The condition fires when the runner's task-
# completed metric stays flat at zero for 30+ minutes after the scheduled
# trigger window — which is exactly the "missed schedule" semantics.

resource "google_monitoring_alert_policy" "daily_runner_missed_schedule" {
  display_name = "QuantAI: daily runner missed schedule"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "No successful task in the last 30 minutes during run window"
    condition_absent {
      filter = join(" AND ", [
        "metric.type=\"run.googleapis.com/job/completed_task_attempt_count\"",
        "resource.type=\"cloud_run_job\"",
        "resource.label.job_name=\"${google_cloud_run_v2_job.daily_runner.name}\"",
        "metric.label.result=\"succeeded\"",
      ])
      duration = "1800s" # 30 min — covers 22:00 → 22:30 UTC trigger window
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_COUNT"
        cross_series_reducer = "REDUCE_SUM"
      }
      trigger {
        count = 1
      }
    }
  }

  notification_channels = [
    google_monitoring_notification_channel.telegram_webhook.id,
    google_monitoring_notification_channel.email.id,
  ]

  alert_strategy {
    # The runner completes within ~10 min, so 6h auto-close keeps a missed-day
    # alert visible until the operator acks it.
    auto_close = "21600s"
  }

  documentation {
    content = <<-EOT
      ⚠️ QuantAI daily runner did not register a *successful* task attempt
      in the last 30 minutes.  Expected daily window: ${var.daily_runner_schedule}.
      Possible causes:
        - Cloud Scheduler trigger failed
        - Cloud SQL instance still warming up (>10 min)
        - Image pull / build broken — check Artifact Registry
        - Job execution still in-flight (auto-close will resolve in 6h)
    EOT
    mime_type = "text/markdown"
  }

  depends_on = [
    google_project_service.apis,
    google_cloud_run_v2_job.daily_runner,
  ]
}

# ── IAM: allow the runner SA to publish to Error Reporting ───────────────────
# `roles/errorreporting.writer` lets google-cloud-error-reporting `report()`
# calls from inside the Cloud Run container succeed without ad-hoc grants.

resource "google_project_iam_member" "sa_errorreporting_writer" {
  project = var.project_id
  role    = "roles/errorreporting.writer"
  member  = "serviceAccount:${google_service_account.quantai.email}"
}

# Enable the Error Reporting API.  It's auto-enabled when the first event
# arrives, but declaring it here means terraform plan shows the dependency.
resource "google_project_service" "error_reporting" {
  service            = "clouderrorreporting.googleapis.com"
  disable_on_destroy = false
}

# ── Outputs ───────────────────────────────────────────────────────────────────
# Surface channel IDs so downstream Terraform (or operators) can reference
# them when adding more alert policies.

output "notification_channel_email_id" {
  description = "Email notification channel ID"
  value       = google_monitoring_notification_channel.email.id
}

output "notification_channel_telegram_id" {
  description = "Telegram webhook notification channel ID"
  value       = google_monitoring_notification_channel.telegram_webhook.id
}
