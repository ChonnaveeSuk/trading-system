"""Centralized database URL accessor.

Fails loudly when DATABASE_URL is unset rather than silently falling back
to a hardcoded credential. Set DATABASE_URL in your shell or .env, e.g.:

    export DATABASE_URL=postgres://quantai:$POSTGRES_PASSWORD@localhost:5432/quantai
"""

from __future__ import annotations

import os


def database_url() -> str:
    """Return DATABASE_URL from the environment or raise RuntimeError."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Export it from .env, e.g.\n"
            "  export DATABASE_URL=postgres://quantai:$POSTGRES_PASSWORD@localhost:5432/quantai"
        )
    return url
