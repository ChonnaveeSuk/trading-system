#!/usr/bin/env python3
# trading-system/scripts/log_system_health.py
#
# Collects operational health metrics and writes them to the system_metrics
# PostgreSQL table. Designed to run:
#   - At the start and end of run_daily.sh  (cron Mon–Fri 22:00 UTC)
#   - Manually for ad-hoc health checks
#
# Metrics collected:
#   pg_connections      - active PostgreSQL connections to the quantai DB
#   redis_hit_rate      - Redis keyspace hit rate %
#   redis_key_count     - total keys in Redis
#   alpaca_latency_ms   - GET /account round-trip time in milliseconds
#   docker_health       - 1=healthy/running, 0=unhealthy/stopped (per container)
#   cron_last_run       - Unix timestamp written when run_daily.sh finishes
#
# Usage:
#   python3 scripts/log_system_health.py             # all metrics
#   python3 scripts/log_system_health.py --cron-done # just write cron_last_run marker
#   python3 scripts/log_system_health.py --skip-alpaca  # skip Alpaca ping (offline)

import argparse
import os
import subprocess
import sys
import time
from typing import Optional

import psycopg2

# ── Config ────────────────────────────────────────────────────────────────────

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://quantai:quantai_dev_2026@localhost:5432/quantai",
)

DOCKER_CONTAINERS = [
    "quantai-postgres",
    "quantai-redis",
    "quantai-grafana",
]

ALPACA_ENDPOINT_DEFAULT = "https://paper-api.alpaca.markets/v2"


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DATABASE_URL)


def insert_metric(
    conn,
    metric_name: str,
    metric_value: float,
    labels: Optional[dict] = None,
) -> None:
    import json
    labels_json = json.dumps(labels or {})
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO system_metrics (metric_name, metric_value, labels)
            VALUES (%s, %s, %s::jsonb)
            """,
            (metric_name, metric_value, labels_json),
        )
    conn.commit()


# ── Metric collectors ─────────────────────────────────────────────────────────

def collect_pg_connections(conn) -> None:
    """Active backend connections to the quantai database."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM pg_stat_activity WHERE datname = 'quantai' AND state = 'active'"
            )
            row = cur.fetchone()
        count = float(row[0]) if row else 0.0
        insert_metric(conn, "pg_connections", count)
        print(f"  pg_connections: {count:.0f}")
    except Exception as exc:
        print(f"  pg_connections: ERROR — {exc}", file=sys.stderr)


def collect_redis_stats(conn) -> None:
    """Redis hit rate % and total key count via redis-cli INFO."""
    try:
        result = subprocess.run(
            ["redis-cli", "-h", "localhost", "-p", "6379", "INFO", "stats"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        hits = 0
        misses = 0
        for line in result.stdout.splitlines():
            if line.startswith("keyspace_hits:"):
                hits = int(line.split(":")[1].strip())
            elif line.startswith("keyspace_misses:"):
                misses = int(line.split(":")[1].strip())

        total = hits + misses
        hit_rate = (hits / total * 100.0) if total > 0 else 0.0
        insert_metric(conn, "redis_hit_rate", hit_rate)
        print(f"  redis_hit_rate: {hit_rate:.1f}%")

        # Key count
        dbsize = subprocess.run(
            ["redis-cli", "-h", "localhost", "-p", "6379", "DBSIZE"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        key_count = float(dbsize.stdout.strip()) if dbsize.stdout.strip().isdigit() else 0.0
        insert_metric(conn, "redis_key_count", key_count)
        print(f"  redis_key_count: {key_count:.0f}")

    except FileNotFoundError:
        print("  redis-cli not found — skipping Redis stats", file=sys.stderr)
    except Exception as exc:
        print(f"  redis_stats: ERROR — {exc}", file=sys.stderr)


def collect_alpaca_latency(conn) -> None:
    """Round-trip latency to Alpaca paper API GET /account."""
    try:
        import urllib.request

        # Try loading credentials from GCP Secret Manager first, then env vars
        api_key = _load_secret("alpaca-api-key") or os.environ.get("ALPACA_API_KEY", "")
        secret_key = _load_secret("alpaca-secret-key") or os.environ.get("ALPACA_SECRET_KEY", "")
        endpoint = (
            _load_secret("alpaca-endpoint")
            or os.environ.get("ALPACA_ENDPOINT", ALPACA_ENDPOINT_DEFAULT)
        )

        if not api_key or not secret_key:
            print("  alpaca_latency: skipped (no credentials)", file=sys.stderr)
            return

        url = endpoint.rstrip("/") + "/account"
        req = urllib.request.Request(url)
        req.add_header("APCA-API-KEY-ID", api_key)
        req.add_header("APCA-API-SECRET-KEY", secret_key)

        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=10):
            pass
        latency_ms = (time.monotonic() - t0) * 1000.0

        insert_metric(conn, "alpaca_latency_ms", latency_ms)
        print(f"  alpaca_latency_ms: {latency_ms:.1f}")

    except Exception as exc:
        print(f"  alpaca_latency: ERROR — {exc}", file=sys.stderr)


def collect_docker_health(conn) -> None:
    """Container health status for each known container (1=healthy/running, 0=stopped)."""
    for container in DOCKER_CONTAINERS:
        try:
            result = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Status}} {{.State.Health.Status}}",
                    container,
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = result.stdout.strip()
            # output: "running healthy" / "running starting" / "exited " / ""
            parts = output.split()
            state = parts[0] if parts else "unknown"
            health = parts[1] if len(parts) > 1 else ""

            # 1 = container is running and health check passed (or no health check defined)
            is_healthy = 1.0 if state == "running" and health in ("healthy", "") else 0.0
            insert_metric(conn, "docker_health", is_healthy, {"container": container})
            print(f"  docker_health[{container}]: {'OK' if is_healthy else 'DOWN'} ({state}/{health})")

        except FileNotFoundError:
            print("  docker not found — skipping container health", file=sys.stderr)
            return
        except Exception as exc:
            print(f"  docker_health[{container}]: ERROR — {exc}", file=sys.stderr)


def write_cron_marker(conn) -> None:
    """Writes a timestamp marker indicating run_daily.sh just completed."""
    insert_metric(conn, "cron_last_run", time.time())
    print(f"  cron_last_run: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")


# ── Secret Manager helper ─────────────────────────────────────────────────────

def _load_secret(name: str) -> Optional[str]:
    """Load a secret from GCP Secret Manager via gcloud CLI. Returns None on failure."""
    project = os.environ.get("GCP_PROJECT_ID", "quantai-trading-paper")
    try:
        result = subprocess.run(
            [
                "gcloud",
                "secrets",
                "versions",
                "access",
                "latest",
                f"--secret={name}",
                f"--project={project}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Log system health metrics to PostgreSQL")
    parser.add_argument(
        "--cron-done",
        action="store_true",
        help="Only write cron_last_run marker (fast — called at end of run_daily.sh)",
    )
    parser.add_argument(
        "--skip-alpaca",
        action="store_true",
        help="Skip Alpaca API latency check (use when running outside market hours offline)",
    )
    args = parser.parse_args()

    print(f"[log_system_health] Connecting to PostgreSQL…")
    try:
        conn = get_conn()
    except Exception as exc:
        print(f"[log_system_health] ERROR: Cannot connect to PostgreSQL — {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        if args.cron_done:
            write_cron_marker(conn)
        else:
            print("[log_system_health] Collecting metrics…")
            collect_pg_connections(conn)
            collect_redis_stats(conn)
            collect_docker_health(conn)
            if not args.skip_alpaca:
                collect_alpaca_latency(conn)
            write_cron_marker(conn)
            print("[log_system_health] Done.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
