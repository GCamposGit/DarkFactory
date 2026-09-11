"""Focused tests for the opt-in read-only HF-01 probes."""

from datetime import timezone
import hashlib
import urllib.error

import pytest
from pydantic import ValidationError

from core.planning.baseline_probes import (
    MAX_PROBE_BYTES,
    MAX_PROBE_TIMEOUT_SECONDS,
    ProbeResponse,
    ProbeSpec,
    ProbeStatus,
    deduplicate_git_receipts,
    deduplicate_observations,
    parse_git_receipt,
    probe_endpoint,
)


def spec(**overrides: object) -> ProbeSpec:
    values: dict[str, object] = {"probe_id": "health", "item_id": "DF-21", "url": "https://hub.example.test/health", "environment": "target-vps"}
    values.update(overrides)
    return ProbeSpec.model_validate(values)


@pytest.mark.parametrize(("status_code", "expected"), [(200, ProbeStatus.OK), (404, ProbeStatus.NOT_FOUND), (401, ProbeStatus.UNAUTHORIZED), (302, ProbeStatus.REDIRECT), (500, ProbeStatus.HTTP_ERROR)])
def test_probe_classifies_http_results_without_retaining_payload(status_code: int, expected: ProbeStatus) -> None:
    payload = b"private payload that must not appear in the observation"
    observation = probe_endpoint(spec(), lambda url, timeout, max_bytes: ProbeResponse(status_code, payload))
    assert observation.status == expected
    assert observation.status_code == status_code
    assert "private payload" not in observation.model_dump_json()
    if expected == ProbeStatus.OK:
        assert observation.response_sha256
        assert observation.response_size == len(payload)


def test_probe_classifies_timeout_and_response_limit() -> None:
    timeout = probe_endpoint(spec(), lambda url, timeout, max_bytes: (_ for _ in ()).throw(TimeoutError()))
    oversized = probe_endpoint(spec(max_bytes=4), lambda url, timeout, max_bytes: ProbeResponse(200, b"12345"))
    assert timeout.status == ProbeStatus.TIMEOUT
    assert oversized.status == ProbeStatus.RESPONSE_TOO_LARGE
    assert oversized.response_sha256 is None


def test_probe_rejects_credentials_and_origin_mismatch() -> None:
    with pytest.raises(ValidationError):
        spec(url="https://user:password@hub.example.test/health")
    with pytest.raises(ValidationError):
        spec(url="https://hub.example.test/health?token=secret")
    mismatch = probe_endpoint(spec(expected_origin="https://other.example.test"), lambda *args: ProbeResponse(200))
    assert mismatch.status == ProbeStatus.INVALID_CONFIG


def test_observation_deduplication_ignores_only_timestamp() -> None:
    first = probe_endpoint(spec(), lambda *args: ProbeResponse(200, b"same"))
    second = first.model_copy(update={"observed_at": first.observed_at.replace(tzinfo=timezone.utc)})
    assert len(deduplicate_observations([first, second])) == 1


def test_git_receipt_parser_keeps_only_sanitized_metadata() -> None:
    receipt = parse_git_receipt({"repository": "owner/repo", "number": 12, "head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}, "state": "open", "checks": [{"conclusion": "success", "raw_secret": "do-not-copy"}], "body": "do-not-copy"})
    assert receipt.head_sha == "a" * 40
    assert "do-not-copy" not in receipt.model_dump_json()
    assert len(deduplicate_git_receipts([receipt, receipt])) == 1


def test_cr13_spec_rejects_timeout_above_3_seconds() -> None:
    with pytest.raises(ValidationError):
        spec(timeout_seconds=3.001)
    with pytest.raises(ValidationError):
        spec(timeout_seconds=5.0)
    with pytest.raises(ValidationError):
        spec(timeout_seconds=10.0)
    with pytest.raises(ValidationError):
        spec(timeout_seconds=0)
    with pytest.raises(ValidationError):
        spec(timeout_seconds=-1.0)
    s = spec(timeout_seconds=3.0)
    assert s.timeout_seconds == 3.0
    default_s = spec()
    assert default_s.timeout_seconds == 3.0
    assert MAX_PROBE_TIMEOUT_SECONDS == 3.0


def test_cr13_spec_rejects_max_bytes_above_65536() -> None:
    with pytest.raises(ValidationError):
        spec(max_bytes=65537)
    with pytest.raises(ValidationError):
        spec(max_bytes=100000)
    with pytest.raises(ValidationError):
        spec(max_bytes=0)
    with pytest.raises(ValidationError):
        spec(max_bytes=-1)
    s = spec(max_bytes=65536)
    assert s.max_bytes == 65536
    default_s = spec()
    assert default_s.max_bytes == 65536
    assert MAX_PROBE_BYTES == 65536


def test_cr13_payload_boundary_exact_65536_accepted() -> None:
    payload = b"A" * 65536
    observation = probe_endpoint(spec(), lambda url, timeout, max_bytes: ProbeResponse(200, payload))
    assert observation.status == ProbeStatus.OK
    assert observation.status_code == 200
    assert observation.response_size == 65536
    assert observation.response_sha256 == hashlib.sha256(payload).hexdigest()


def test_cr13_payload_boundary_65537_rejected_without_body_leak() -> None:
    sensitive_marker = b"SENSITIVE_SECRET_TOKEN_XYZ_123"
    payload = b"B" * (65537 - len(sensitive_marker)) + sensitive_marker
    assert len(payload) == 65537
    observation = probe_endpoint(spec(), lambda url, timeout, max_bytes: ProbeResponse(200, payload))
    assert observation.status == ProbeStatus.RESPONSE_TOO_LARGE
    assert observation.error_code == "PROBE_RESPONSE_TOO_LARGE"
    assert observation.response_sha256 is None
    assert observation.response_size is None
    dumped = observation.model_dump_json()
    assert "SENSITIVE_SECRET_TOKEN" not in dumped


def test_cr13_controlled_clock_exact_deadline_and_exceeded() -> None:
    # Prazo exato: elapsed == 3.0 -> OK
    times = [0.0, 3.0]
    clock = lambda: times.pop(0) if times else 3.0
    exact_obs = probe_endpoint(
        spec(),
        lambda url, timeout, max_bytes: ProbeResponse(200, b"ok"),
        timer=clock,
    )
    assert exact_obs.status == ProbeStatus.OK

    # Prazo excedido: elapsed == 3.001 -> TIMEOUT
    times_over = [0.0, 3.001]
    clock_over = lambda: times_over.pop(0) if times_over else 3.001
    exceeded_obs = probe_endpoint(
        spec(),
        lambda url, timeout, max_bytes: ProbeResponse(200, b"ok"),
        timer=clock_over,
    )
    assert exceeded_obs.status == ProbeStatus.TIMEOUT
    assert exceeded_obs.error_code == "TIMEOUT"

    # Prazo excedido com falha HTTP tardia: elapsed > 3.0 -> TIMEOUT
    times_late_err = [0.0, 3.1]
    clock_late_err = lambda: times_late_err.pop(0) if times_late_err else 3.1
    late_err_obs = probe_endpoint(
        spec(),
        lambda url, timeout, max_bytes: (_ for _ in ()).throw(
            urllib.error.HTTPError("https://hub.example.test/health", 500, "Server Error", {}, None)
        ),
        timer=clock_late_err,
    )
    assert late_err_obs.status == ProbeStatus.TIMEOUT
    assert late_err_obs.error_code == "TIMEOUT"


def test_cr13_transport_failure_structured_without_sensitive_leak() -> None:
    # Connection refused
    conn_refused = probe_endpoint(
        spec(),
        lambda url, timeout, max_bytes: (_ for _ in ()).throw(
            ConnectionRefusedError("Connection refused to internal secret-host:8080")
        ),
    )
    assert conn_refused.status == ProbeStatus.NETWORK_ERROR
    assert conn_refused.error_code == "PROBE_TRANSPORT_ERROR"
    assert "secret-host" not in conn_refused.model_dump_json()

    # URLError without timeout
    url_err = probe_endpoint(
        spec(),
        lambda url, timeout, max_bytes: (_ for _ in ()).throw(
            urllib.error.URLError("getaddrinfo failed for private-db.internal")
        ),
    )
    assert url_err.status == ProbeStatus.NETWORK_ERROR
    assert "private-db" not in url_err.model_dump_json()

