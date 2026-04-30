#!/usr/bin/env python3
# trading-system/scripts/error_report.py
#
# Tiny CLI wrapper around google-cloud-error-reporting that lets shell scripts
# (run_daily.sh, backup_postgres.sh) report a structured failure to Cloud
# Error Reporting in one line.
#
# Usage from bash:
#
#   python3 scripts/error_report.py \
#       --step "Step 2/4: run_strategy" \
#       --message "exit code $?" \
#       --traceback-file /tmp/quantai_step2.log
#
# The traceback file is optional; when supplied, its contents are appended to
# the error payload so Error Reporting can group similar exceptions.  The
# script is best-effort: any failure (missing credentials, no GCP access, no
# package installed) is logged to stderr and exits 0 — we never want our
# own error reporter to break the daily run.

from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("error_report")


def _read_traceback_file(path: Optional[str]) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
        # Cap at 8 KB to stay under Error Reporting's payload limit.
        return data[-8192:]
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning("Could not read traceback file %s: %s", path, exc)
        return ""


def _report(step: str, message: str, traceback_text: str, project_id: str) -> bool:
    """Send a structured payload to Cloud Error Reporting.  Returns True on success."""
    try:
        from google.cloud import error_reporting
    except ImportError:
        logger.warning(
            "google-cloud-error-reporting not installed — skipping cloud report. "
            "Run: pip install google-cloud-error-reporting"
        )
        return False

    try:
        client = error_reporting.Client(
            project=project_id,
            service="quantai-daily-runner",
        )
    except Exception as exc:
        logger.warning("Could not init Error Reporting client: %s", exc)
        return False

    iso_now = datetime.now(timezone.utc).isoformat()
    body = (
        f"QuantAI step failure\n"
        f"  step:      {step}\n"
        f"  message:   {message}\n"
        f"  timestamp: {iso_now}\n"
    )
    if traceback_text:
        body += f"\n--- traceback ---\n{traceback_text}\n"

    try:
        # report() is non-exceptional — it groups by hash of the message body.
        client.report(body)
        logger.info("Error Reporting: posted '%s' (%s)", step, message)
        return True
    except Exception as exc:
        logger.warning("Error Reporting: client.report() raised: %s", exc)
        # As a last resort, dump a fake traceback so the message is grouped.
        try:
            client.report_exception()
        except Exception:
            pass
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Report a step failure to Cloud Error Reporting")
    parser.add_argument("--step", required=True, help="Logical step name (e.g. 'Step 2/4: run_strategy')")
    parser.add_argument("--message", required=True, help="Short failure summary (1 line)")
    parser.add_argument("--traceback-file", default=None,
                        help="Optional path to a file with full stderr / traceback")
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", "quantai-trading-paper"))
    args = parser.parse_args()

    tb = _read_traceback_file(args.traceback_file)
    ok = _report(args.step, args.message, tb, args.project)

    # Always exit 0 — the caller has already failed; we don't want to compound.
    if not ok:
        # Dump to stderr so it still shows in Cloud Run logs (where the
        # log-based unhandled-exception alert can pick it up).
        print(f"[error_report] FAILED to publish: {args.step} — {args.message}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # pragma: no cover
        traceback.print_exc()
        sys.exit(0)
