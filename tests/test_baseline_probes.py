"""Focused tests for the opt-in read-only HF-01 probes."""

from datetime import timezone

import pytest
from pydantic import ValidationError

from core.planning.baseline_probes import ProbeResponse, ProbeSpec, ProbeStatus, deduplicate_git_receipts, deduplicate_observations, parse_git_receipt, probe_endpoint


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
