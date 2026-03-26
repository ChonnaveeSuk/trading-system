# trading-system/strategy/src/gcp/__init__.py
"""
GCP client layer for the Python strategy service.

Rules:
  - GCP clients initialized ONCE at startup, not per-request
  - All credentials via google.auth.default() — never hardcode
  - BigQuery: use NUMERIC type for prices, FLOAT64 for ratios/scores
  - Convert Decimal → float ONLY at the BigQuery write boundary
  - Secret Manager: use @lru_cache to avoid repeated API calls
"""

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)


@lru_cache(maxsize=None)
def get_secret(secret_id: str, project_id: str) -> str:
    """Fetch a secret from GCP Secret Manager.

    Results are cached in-process for the lifetime of the service.
    Call at startup, not in hot paths.

    Args:
        secret_id:  Secret name (e.g. "trading-mode")
        project_id: GCP project ID

    Returns:
        Secret value as a stripped string.

    Raises:
        google.api_core.exceptions.NotFound: If secret doesn't exist.
    """
    from google.cloud import secretmanager  # type: ignore

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    value = response.payload.data.decode("UTF-8").strip()
    logger.info("Loaded secret: %s", secret_id)
    return value


def verify_paper_mode(project_id: str) -> None:
    """Safety check: abort if not in paper trading mode.

    Call this at every service startup before doing anything else.

    Raises:
        SystemExit: If trading-mode != 'paper'
    """
    mode = get_secret("trading-mode", project_id)
    if mode.lower() != "paper":
        logger.critical(
            "SAFETY ABORT: trading-mode='%s', expected 'paper'. "
            "Never run in live mode without explicit authorization.",
            mode,
        )
        raise SystemExit(1)
    logger.info("Trading mode verified: %s", mode)


# TODO Phase 1: class BigQueryClient (stream trades, ohlcv, signals)
# TODO Phase 1: class PubSubClient (subscribe to fills topic)
