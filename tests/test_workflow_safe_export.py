"""Tests for CR-08: safe export and diagnostic sanitization for manifests."""

from __future__ import annotations

from datetime import UTC, datetime
import pytest
from pydantic import ValidationError

from core.workflow.contracts import (
    EnvironmentEndpoint,
    EnvironmentKind,
    EnvironmentManifest,
    SanitizedIdentity,
    SecretReference,
)
from core.workflow.safe_export import (
    format_safe_validation_error,
    safe_export_manifest,
    safe_export_model,
    safe_validation_diagnostics,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def make_valid_manifest() -> EnvironmentManifest:
    return EnvironmentManifest(
        environment_ref="env-target-local",
        ticket_id="HF-04-01",
        kind=EnvironmentKind.TARGET_ENVIRONMENT,
        tools=[{"name": "python", "version": "3.12", "source": "runtime"}],
        system="Linux",
        architecture="x86_64",
        services=["local workflow runner", "redis"],
        accounts=["operator"],
        secret_refs=[
            SecretReference(
                ref_id="sec-1",
                provider="vault",
                locator="secret/data/hf04",
                variable_name="HF04_TOKEN",
            )
        ],
        permission_scopes=["filesystem:workspace"],
        required_env_vars=["HF04_TOKEN"],
        endpoints=[
            EnvironmentEndpoint(
                endpoint_id="ep-health",
                url="https://example.invalid:8443/health?check=ready#live",
                protocol="https",
            )
        ],
        ports=[{"port": 8443, "direction": "outbound", "purpose": "health"}],
        connection_origins=["local"],
        network_policy="strict",
        worker_identity=SanitizedIdentity(subject="worker-1", role="implementer", host="local"),
        installation=["make install"],
        probes=["pytest"],
        rollback=["git clean"],
        cleanup=["rm -rf tmp"],
        target_differences=["None"],
        observed_at=NOW,
    )


def test_endpoint_rejects_synthetic_token_in_query() -> None:
    synthetic_token = "synthetic_token_secret_xyz123"
    with pytest.raises(ValidationError) as exc_info:
        EnvironmentEndpoint(
            endpoint_id="bad-ep",
            url=f"https://example.invalid/health?token={synthetic_token}",
        )

    # Check safe diagnostics
    diagnostics = safe_validation_diagnostics(exc_info.value)
    assert len(diagnostics) > 0
    for diag in diagnostics:
        assert "input" not in diag
        assert "input_value" not in diag
        assert synthetic_token not in diag["message"]
        assert diag["code"] == "value_error"

    # Check formatted error
    formatted = format_safe_validation_error(exc_info.value)
    assert synthetic_token not in formatted
    assert "[VALIDATION_ERROR]" in formatted


def test_endpoint_rejects_synthetic_userinfo() -> None:
    synthetic_pass = "synthetic_super_password_456"
    with pytest.raises(ValidationError) as exc_info:
        EnvironmentEndpoint(
            endpoint_id="bad-ep-userinfo",
            url=f"https://user:{synthetic_pass}@example.invalid/health",
        )

    diagnostics = safe_validation_diagnostics(exc_info.value)
    for diag in diagnostics:
        assert "input" not in diag
        assert "input_value" not in diag
        assert synthetic_pass not in diag["message"]

    formatted = format_safe_validation_error(exc_info.value)
    assert synthetic_pass not in formatted


def test_endpoint_rejects_synthetic_token_in_fragment() -> None:
    synthetic_key = "synthetic_api_key_789"
    with pytest.raises(ValidationError) as exc_info:
        EnvironmentEndpoint(
            endpoint_id="bad-ep-frag",
            url=f"https://example.invalid/api#api_key={synthetic_key}",
        )

    diagnostics = safe_validation_diagnostics(exc_info.value)
    for diag in diagnostics:
        assert "input" not in diag
        assert "input_value" not in diag
        assert synthetic_key not in diag["message"]

    formatted = format_safe_validation_error(exc_info.value)
    assert synthetic_key not in formatted


def test_endpoint_preserves_safe_urls_without_silent_mutation() -> None:
    safe_url = "https://example.invalid:8443/health?check=ready#live"
    ep = EnvironmentEndpoint(endpoint_id="safe-ep", url=safe_url)
    assert ep.url == safe_url


def test_manifest_rejects_dsn_in_services() -> None:
    synthetic_dsn = "postgres://operator:secret_pass_999@db.example.invalid:5432/app"
    base = make_valid_manifest().model_dump()
    base["services"] = [synthetic_dsn]

    with pytest.raises(ValidationError) as exc_info:
        EnvironmentManifest.model_validate(base)

    diagnostics = safe_validation_diagnostics(exc_info.value)
    for diag in diagnostics:
        assert "input" not in diag
        assert "input_value" not in diag
        assert "secret_pass_999" not in diag["message"]

    formatted = format_safe_validation_error(exc_info.value)
    assert "secret_pass_999" not in formatted


def test_manifest_rejects_password_assignment_in_services() -> None:
    synthetic_secret = "password=synthetic_plain_secret_111"
    base = make_valid_manifest().model_dump()
    base["services"] = [synthetic_secret]

    with pytest.raises(ValidationError) as exc_info:
        EnvironmentManifest.model_validate(base)

    diagnostics = safe_validation_diagnostics(exc_info.value)
    for diag in diagnostics:
        assert "input" not in diag
        assert "input_value" not in diag
        assert "synthetic_plain_secret_111" not in diag["message"]

    formatted = format_safe_validation_error(exc_info.value)
    assert "synthetic_plain_secret_111" not in formatted


def test_manifest_accepts_valid_service_identifiers() -> None:
    manifest = make_valid_manifest()
    assert "local workflow runner" in manifest.services
    assert "redis" in manifest.services


def test_safe_validation_diagnostics_omits_input_payload() -> None:
    with pytest.raises(ValidationError) as exc_info:
        EnvironmentEndpoint(endpoint_id="bad", url="https://user:pass@host/path")

    diags = safe_validation_diagnostics(exc_info.value)
    assert len(diags) > 0
    for d in diags:
        assert set(d.keys()) == {"code", "loc", "message"}
        assert "input" not in d
        assert "input_value" not in d


def test_safe_export_manifest_roundtrip_clean() -> None:
    manifest = make_valid_manifest()
    exported = safe_export_manifest(manifest)

    assert exported["environment_ref"] == "env-target-local"
    assert exported["ticket_id"] == "HF-04-01"
    assert len(exported["secret_refs"]) == 1
    assert exported["secret_refs"][0]["ref_id"] == "sec-1"
    assert exported["secret_refs"][0]["provider"] == "vault"
    assert exported["secret_refs"][0]["locator"] == "secret/data/hf04"
    assert exported["endpoints"][0]["url"] == "https://example.invalid:8443/health?check=ready#live"
    assert exported["services"] == ["local workflow runner", "redis"]


def test_safe_export_model_redacts_nested_secrets() -> None:
    data = {
        "config": {
            "token": "sensitive_token_value_abc",
            "safe_name": "worker_one",
            "nested_list": [
                {"password": "secret_password_123"},
                "plain_text_entry",
            ],
        },
        "url_with_secret": "postgres://user:secret_pw@host/db",
    }
    exported = safe_export_model(data)

    assert exported["config"]["token"] == "[REDACTED]"
    assert exported["config"]["safe_name"] == "worker_one"
    assert exported["config"]["nested_list"][0]["password"] == "[REDACTED]"
    assert exported["config"]["nested_list"][1] == "plain_text_entry"
    assert "secret_pw" not in str(exported)
