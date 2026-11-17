"""Test suite for HF-03-02: Cloud Database Probe and Sanitization.

Tests:
- Absent URL produces status='blocked' without exception.
- Superuser 'postgres' is rejected with explicit policy error.
- Sanitization masks passwords and tokens under connection errors.
- Unprivileged user format roundtrip.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from core.orchestrator.cloud_db import (
    DatabaseProbeResult,
    probe_cloud_database,
    sanitize_database_url,
)


def test_missing_database_url_produces_clean_blocked_status() -> None:
    result = probe_cloud_database(database_url=None)
    assert result.status == "blocked"
    assert "waiting_access" in result.error_message or "not configured" in result.error_message
    assert result.database_name is None
    assert result.current_user is None


def test_superuser_is_strictly_rejected() -> None:
    fake_superuser_url = "postgresql://postgres:secretpassword@localhost:5432/darkfac_hf02_prod"
    result = probe_cloud_database(database_url=fake_superuser_url)
    assert result.status == "error"
    assert result.is_unprivileged is False
    assert "Superuser 'postgres' is forbidden" in (result.error_message or "")
    assert "secretpassword" not in (result.error_message or "")


def test_sanitize_database_url_masks_credentials() -> None:
    url = "postgresql://darkfac_worker:supersecretpassword123@100.83.176.60:5432/darkfac_hf02_prod"
    sanitized = sanitize_database_url(url)
    assert "supersecretpassword123" not in sanitized
    assert "darkfac_worker:***@100.83.176.60:5432" in sanitized


def test_unprivileged_user_without_psycopg_driver_returns_clean_blocked() -> None:
    fake_worker_url = "postgresql://darkfac_worker:secret123@100.83.176.60:5432/darkfac_hf02_prod"
    result = probe_cloud_database(database_url=fake_worker_url)
    assert result.status in {"blocked", "error"}
    assert "secret123" not in str(result.model_dump())


def test_driver_exception_does_not_echo_dsn_or_sensitive_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_url = (
        "postgresql://writer:secret-pass@private-db:5432/factory"
        "?token=secret-token&sslpassword=secret-ssl&api_key=secret-key"
    )

    def fail_connect(*args: object, **kwargs: object) -> None:
        raise TimeoutError(f"could not connect to {secret_url}")

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=fail_connect))
    result = probe_cloud_database(database_url=secret_url)
    output = str(result.model_dump())

    assert result.status == "error"
    assert result.error_message == "PostgreSQL connection failed (TimeoutError)"
    for sensitive in (secret_url, "secret-pass", "secret-token", "secret-ssl", "secret-key", "private-db"):
        assert sensitive not in output
