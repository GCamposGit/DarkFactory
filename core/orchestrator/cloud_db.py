"""Cloud database connectivity probe and configuration for HF-03.

Governed by HF-03-02 / ADR-HF-001.
Ensures zero credential leakage, validates unprivileged user boundaries,
and reports structured probe status for the remote PostgreSQL target.
"""

from __future__ import annotations

import os
import re
import time
from urllib.parse import urlparse
from pydantic import BaseModel, ConfigDict, Field


_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"://([^:]+):([^@]+)@"),
    re.compile(r"password=([^\s;]+)", re.I),
)


def sanitize_database_url(url: str | None) -> str:
    """Mask credentials in a database URL for safe diagnostics."""
    if not url:
        return ""
    sanitized = url
    for pat in _SECRET_PATTERNS:
        sanitized = pat.sub(r"://\1:***@", sanitized)
    return sanitized


class DatabaseProbeResult(BaseModel):
    """Structured result of probing the cloud PostgreSQL database."""

    model_config = ConfigDict(frozen=True)

    status: str = Field(description="'ready', 'blocked', or 'error'")
    database_name: str | None = None
    current_user: str | None = None
    server_version: str | None = None
    is_unprivileged: bool = False
    error_message: str | None = None
    latency_ms: float | None = None


def probe_cloud_database(
    database_url: str | None = None,
    timeout_seconds: float = 5.0,
) -> DatabaseProbeResult:
    """Probe PostgreSQL connectivity and enforce governance requirements.

    Requirements:
    - If URL is missing, returns status='blocked' cleanly.
    - If user is 'postgres', fails unprivileged validation (superuser forbidden).
    - Under no circumstances leaks passwords in error_message.
    """
    raw_url = database_url or os.environ.get("DARKFAC_HF02_DATABASE_URL")
    if not raw_url or not raw_url.strip():
        return DatabaseProbeResult(
            status="blocked",
            error_message="DARKFAC_HF02_DATABASE_URL not configured (waiting_access)",
        )

    # Validate URL structure and unprivileged user before attempting network I/O
    try:
        parsed = urlparse(raw_url)
        username = parsed.username or ""
        db_name = parsed.path.lstrip("/")
    except Exception as exc:
        return DatabaseProbeResult(
            status="error",
            error_message=f"Invalid database URL format: {exc}",
        )

    if username.casefold() in {"postgres", "root", "admin", "superuser"}:
        return DatabaseProbeResult(
            status="error",
            database_name=db_name,
            current_user=username,
            is_unprivileged=False,
            error_message=f"Superuser '{username}' is forbidden by HF-03 policy; use unprivileged 'darkfac_worker'",
        )

    # Attempt connection via psycopg if installed
    start_time = time.monotonic()
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError:
        return DatabaseProbeResult(
            status="blocked",
            database_name=db_name,
            current_user=username,
            is_unprivileged=True,
            error_message="psycopg driver is not installed in the active environment",
        )

    try:
        with psycopg.connect(raw_url, connect_timeout=int(timeout_seconds)) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), current_user, version();")
                row = cur.fetchone()
                latency = (time.monotonic() - start_time) * 1000.0
                if row:
                    actual_db, actual_user, version_str = row
                    is_unprivileged = actual_user.casefold() not in {"postgres", "root"}
                    return DatabaseProbeResult(
                        status="ready" if is_unprivileged else "error",
                        database_name=actual_db,
                        current_user=actual_user,
                        server_version=version_str.split()[1] if version_str else None,
                        is_unprivileged=is_unprivileged,
                        latency_ms=latency,
                        error_message=None if is_unprivileged else "Connected user resolved to privileged superuser",
                    )
                return DatabaseProbeResult(
                    status="error",
                    database_name=db_name,
                    current_user=username,
                    error_message="Database probe query returned zero rows",
                )
    except Exception as exc:
        # Sanitize exception message to ensure password is never exposed
        raw_msg = str(exc)
        sanitized_msg = sanitize_database_url(raw_msg)
        if parsed.password and parsed.password in sanitized_msg:
            sanitized_msg = sanitized_msg.replace(parsed.password, "***")
        return DatabaseProbeResult(
            status="error",
            database_name=db_name,
            current_user=username,
            is_unprivileged=True,
            error_message=sanitized_msg,
        )
