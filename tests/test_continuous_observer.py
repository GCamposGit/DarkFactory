"""Deterministic acceptance tests for ContinuousObserver and Negative Invariant Protocol.

Governed by:
- docs/handoffs/continuous-autonomy/HF-15-01.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- docs/handoffs/continuous-autonomy/acceptance-protocol.json
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import pytest

from core.acceptance.continuous_observer import (
    ContinuousObserver,
    RuleViolation,
    AuditReport,
    CorrelationResult,
)
from core.workflow.control_contracts import JobKey


@pytest.fixture
def observer() -> ContinuousObserver:
    return ContinuousObserver()


# ---------------------------------------------------------------------------
# Test V01: consumer_off_health200_rejected
# ---------------------------------------------------------------------------

def test_rule_v01_consumer_off_health200_rejected(observer: ContinuousObserver) -> None:
    """V01: Inactive consumer returning HTTP 200 health must fail closed."""
    # Violation case
    context = {
        "run_id": "run-v01",
        "consumer_active": False,
        "consumer_status": "offline",
        "health_status_code": 200,
    }
    violation = observer.verify_rule("V01", context)
    assert violation is not None
    assert violation.rule_id == "V01"
    assert violation.severity == "critical"

    report = observer.audit_run(context)
    assert not report.passed
    assert any(v.rule_id == "V01" for v in report.violations)

    # Valid passing case: consumer active and healthy
    valid_context = {
        "run_id": "run-v01-ok",
        "consumer_active": True,
        "consumer_status": "healthy",
        "health_status_code": 200,
    }
    assert observer.verify_rule("V01", valid_context) is None

    # Valid passing case: consumer offline and health correctly reports 503
    unhealthy_context = {
        "run_id": "run-v01-degraded",
        "consumer_active": False,
        "consumer_status": "offline",
        "health_status_code": 503,
    }
    assert observer.verify_rule("V01", unhealthy_context) is None


# ---------------------------------------------------------------------------
# Test V02: manual_stage_advance_rejected
# ---------------------------------------------------------------------------

def test_rule_v02_manual_stage_advance_rejected(observer: ContinuousObserver) -> None:
    """V02: Automated controller advancing manual human stage directly must fail closed."""
    # Violation case: controller advancing manual approval stage
    context = {
        "run_id": "run-v02",
        "stage": "manual_approval",
        "is_manual_stage": True,
        "invoked_by": "controller",
        "human_approved": False,
    }
    violation = observer.verify_rule("V02", context)
    assert violation is not None
    assert violation.rule_id == "V02"
    assert violation.severity == "critical"

    # Valid passing case: human approval present
    valid_context = {
        "run_id": "run-v02-ok",
        "stage": "manual_approval",
        "is_manual_stage": True,
        "invoked_by": "controller",
        "human_approved": True,
    }
    assert observer.verify_rule("V02", valid_context) is None

    # Valid passing case: automated non-manual stage
    auto_stage_context = {
        "run_id": "run-v02-auto",
        "stage": "development",
        "is_manual_stage": False,
        "invoked_by": "controller",
    }
    assert observer.verify_rule("V02", auto_stage_context) is None


# ---------------------------------------------------------------------------
# Test V03: self_served_evidence_rejected
# ---------------------------------------------------------------------------

def test_rule_v03_self_served_evidence_rejected(observer: ContinuousObserver) -> None:
    """V03: Evidence produced solely by candidate under test must fail closed."""
    # Violation case: candidate matches auditor/producer
    context = {
        "run_id": "run-v03",
        "candidate_identity": "worker-model-a",
        "auditor_identity": "worker-model-a",
    }
    violation = observer.verify_rule("V03", context)
    assert violation is not None
    assert violation.rule_id == "V03"

    # Violation case: explicit self-served evidence flag in evidence payload
    context_payload = {
        "run_id": "run-v03-b",
        "evidence": {"self_served": True, "producer": "worker-candidate"},
    }
    assert observer.verify_rule("V03", context_payload) is not None

    # Valid passing case: distinct independent auditor
    valid_context = {
        "run_id": "run-v03-ok",
        "candidate_identity": "worker-candidate",
        "auditor_identity": "independent-oracle-b",
    }
    assert observer.verify_rule("V03", valid_context) is None


# ---------------------------------------------------------------------------
# Test V04: anomalous_clock_rejected
# ---------------------------------------------------------------------------

def test_rule_v04_anomalous_clock_rejected(observer: ContinuousObserver) -> None:
    """V04: Timestamp skew or future clock must fail closed."""
    # Violation case: clock skew exceeding 30s
    context = {
        "run_id": "run-v04",
        "clock_skew_seconds": 45.0,
        "max_skew_seconds": 30.0,
    }
    violation = observer.verify_rule("V04", context)
    assert violation is not None
    assert violation.rule_id == "V04"

    # Violation case: timestamp in the future beyond tolerance
    future_time = datetime.now(UTC) + timedelta(minutes=10)
    future_context = {
        "run_id": "run-v04-fut",
        "timestamp": future_time.isoformat(),
        "max_skew_seconds": 30.0,
    }
    assert observer.verify_rule("V04", future_context) is not None

    # Valid passing case: timestamp within normal tolerance
    valid_context = {
        "run_id": "run-v04-ok",
        "clock_skew_seconds": 0.5,
        "max_skew_seconds": 30.0,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    assert observer.verify_rule("V04", valid_context) is None


# ---------------------------------------------------------------------------
# Test V05: expired_lease_claim_rejected
# ---------------------------------------------------------------------------

def test_rule_v05_expired_lease_claim_rejected(observer: ContinuousObserver) -> None:
    """V05: Expired lease or stale claim must fail closed."""
    # Violation case: expires_at in the past
    past_time = datetime.now(UTC) - timedelta(seconds=10)
    context = {
        "run_id": "run-v05",
        "claim": {
            "lease_id": "lease-123",
            "expires_at": past_time.isoformat(),
        },
    }
    violation = observer.verify_rule("V05", context)
    assert violation is not None
    assert violation.rule_id == "V05"

    # Violation case: stale_fencing_token flag
    stale_context = {
        "run_id": "run-v05-stale",
        "stale_fencing_token": True,
    }
    assert observer.verify_rule("V05", stale_context) is not None

    # Valid passing case: active lease
    future_time = datetime.now(UTC) + timedelta(minutes=5)
    valid_context = {
        "run_id": "run-v05-ok",
        "claim": {
            "lease_id": "lease-123",
            "expires_at": future_time.isoformat(),
        },
    }
    assert observer.verify_rule("V05", valid_context) is None


# ---------------------------------------------------------------------------
# Test V06: idempotency_payload_mismatch_rejected
# ---------------------------------------------------------------------------

def test_rule_v06_idempotency_payload_mismatch_rejected(observer: ContinuousObserver) -> None:
    """V06: Idempotency conflict with divergent payload digests must fail closed."""
    # Violation case: different digests
    context = {
        "run_id": "run-v06",
        "existing_payload_digest": "sha256:aaaabbbbcccc",
        "new_payload_digest": "sha256:111122223333",
    }
    violation = observer.verify_rule("V06", context)
    assert violation is not None
    assert violation.rule_id == "V06"

    # Valid passing case: identical payload digests
    valid_context = {
        "run_id": "run-v06-ok",
        "existing_payload_digest": "sha256:aaaabbbbcccc",
        "new_payload_digest": "sha256:aaaabbbbcccc",
    }
    assert observer.verify_rule("V06", valid_context) is None


# ---------------------------------------------------------------------------
# Test V07: credential_leak_in_logs_rejected
# ---------------------------------------------------------------------------

def test_rule_v07_credential_leak_in_logs_rejected(observer: ContinuousObserver) -> None:
    """V07: Cleartext secrets or credentials in logs must fail closed."""
    # Violation case: GitHub token in logs
    context = {"run_id": "run-v07"}
    leaked_logs = [
        "Starting job execution",
        "Authenticated with token: ghp_1234567890abcdefghijklmnopqrstuv12",
        "Job completed",
    ]
    violation = observer.verify_rule("V07", context, logs=leaked_logs)
    assert violation is not None
    assert violation.rule_id == "V07"
    assert "leak_sample" in violation.details

    # Violation case: API key in context
    context_leak = {
        "run_id": "run-v07-b",
        "raw_dump": "api_key = 'sk-abcdef1234567890abcdef'",
    }
    assert observer.verify_rule("V07", context_leak) is not None

    # Valid passing case: clean sanitized logs
    clean_logs = [
        "Starting job execution",
        "Using SecretReference(provider='darkfac', locator='env:API_KEY')",
        "Job completed successfully",
    ]
    assert observer.verify_rule("V07", context, logs=clean_logs) is None


# ---------------------------------------------------------------------------
# Test V08: missing_independent_oracle_rejected
# ---------------------------------------------------------------------------

def test_rule_v08_missing_independent_oracle_rejected(observer: ContinuousObserver) -> None:
    """V08: Verification/validation stage missing distinct oracle must fail closed."""
    # Violation case: validation stage with no oracle
    context = {
        "run_id": "run-v08",
        "stage": "validation",
        "candidate_identity": "worker-candidate-1",
        "oracle_identity": None,
    }
    violation = observer.verify_rule("V08", context)
    assert violation is not None
    assert violation.rule_id == "V08"

    # Violation case: oracle matches candidate
    identical_context = {
        "run_id": "run-v08-b",
        "stage": "independent_review",
        "candidate_identity": "worker-1",
        "oracle_identity": "worker-1",
    }
    assert observer.verify_rule("V08", identical_context) is not None

    # Valid passing case: independent oracle present
    valid_context = {
        "run_id": "run-v08-ok",
        "stage": "validation",
        "candidate_identity": "worker-candidate-1",
        "oracle_identity": "oracle-verifier-2",
    }
    assert observer.verify_rule("V08", valid_context) is None


# ---------------------------------------------------------------------------
# Test V09: timeout_without_checkpoint_rejected
# ---------------------------------------------------------------------------

def test_rule_v09_timeout_without_checkpoint_rejected(observer: ContinuousObserver) -> None:
    """V09: Timeout without prior checkpoint persistence must fail closed."""
    # Violation case: timed out with 0 checkpoints
    context = {
        "run_id": "run-v09",
        "timed_out": True,
        "checkpoints_count": 0,
    }
    violation = observer.verify_rule("V09", context)
    assert violation is not None
    assert violation.rule_id == "V09"

    # Valid passing case: timed out but saved checkpoints
    valid_context = {
        "run_id": "run-v09-ok",
        "timed_out": True,
        "checkpoints_count": 3,
    }
    assert observer.verify_rule("V09", valid_context) is None

    # Valid passing case: normal run without timeout
    normal_context = {
        "run_id": "run-v09-norm",
        "timed_out": False,
        "checkpoints_count": 0,
    }
    assert observer.verify_rule("V09", normal_context) is None


# ---------------------------------------------------------------------------
# Test V10: resource_starvation_rejected
# ---------------------------------------------------------------------------

def test_rule_v10_resource_starvation_rejected(observer: ContinuousObserver) -> None:
    """V10: Resource exhaustion without mitigation/backoff policy must fail closed."""
    # Violation case: rate limit exceeded with no fallback policy
    context = {
        "run_id": "run-v10",
        "rate_limited": True,
        "has_backoff_policy": False,
    }
    violation = observer.verify_rule("V10", context)
    assert violation is not None
    assert violation.rule_id == "V10"

    # Valid passing case: rate limited but with active backoff policy
    valid_context = {
        "run_id": "run-v10-ok",
        "rate_limited": True,
        "has_backoff_policy": True,
    }
    assert observer.verify_rule("V10", valid_context) is None


# ---------------------------------------------------------------------------
# Test V11: out_of_bounds_mutation_rejected
# ---------------------------------------------------------------------------

def test_rule_v11_out_of_bounds_mutation_rejected(observer: ContinuousObserver) -> None:
    """V11: File mutation outside permitted allowed_paths must fail closed."""
    # Violation case: writing to an unauthorized file
    context = {
        "run_id": "run-v11",
        "allowed_paths": [
            "core/acceptance/continuous_observer.py",
            "docs/handoffs/continuous-autonomy/acceptance-protocol.json",
            "tests/test_continuous_observer.py",
        ],
        "mutated_paths": [
            "core/acceptance/continuous_observer.py",
            "MISSION.md",  # Unauthorized!
        ],
    }
    violation = observer.verify_rule("V11", context)
    assert violation is not None
    assert violation.rule_id == "V11"
    assert "MISSION.md" in str(violation.details)

    # Valid passing case: all mutated paths within allowed_paths
    valid_context = {
        "run_id": "run-v11-ok",
        "allowed_paths": [
            "core/acceptance/continuous_observer.py",
            "docs/handoffs/continuous-autonomy/acceptance-protocol.json",
            "tests/test_continuous_observer.py",
        ],
        "mutated_paths": [
            "core/acceptance/continuous_observer.py",
            "tests/test_continuous_observer.py",
        ],
    }
    assert observer.verify_rule("V11", valid_context) is None


# ---------------------------------------------------------------------------
# Test V12: heartbeat_silence_window_rejected
# ---------------------------------------------------------------------------

def test_rule_v12_heartbeat_silence_window_rejected(observer: ContinuousObserver) -> None:
    """V12: Heartbeat silence exceeding 45 seconds must fail closed."""
    # Violation case: silence of 55 seconds (> 45s lease)
    context = {
        "run_id": "run-v12",
        "heartbeat_silence_seconds": 55.0,
    }
    violation = observer.verify_rule("V12", context)
    assert violation is not None
    assert violation.rule_id == "V12"

    # Violation case: last heartbeat was 60s ago
    past_hb = datetime.now(UTC) - timedelta(seconds=60)
    context_ts = {
        "run_id": "run-v12-b",
        "last_heartbeat_at": past_hb.isoformat(),
    }
    assert observer.verify_rule("V12", context_ts) is not None

    # Valid passing case: heartbeat sent 10s ago
    valid_context = {
        "run_id": "run-v12-ok",
        "heartbeat_silence_seconds": 10.0,
    }
    assert observer.verify_rule("V12", valid_context) is None


# ---------------------------------------------------------------------------
# Test V13: dirty_worktree_rejected
# ---------------------------------------------------------------------------

def test_rule_v13_dirty_worktree_rejected(observer: ContinuousObserver) -> None:
    """V13: Uncommitted changes or baseline mismatch must fail closed."""
    # Violation case: uncommitted files
    context = {
        "run_id": "run-v13",
        "uncommitted_files": ["dirty_file.py"],
    }
    violation = observer.verify_rule("V13", context)
    assert violation is not None
    assert violation.rule_id == "V13"

    # Violation case: baseline SHA divergence
    divergent_context = {
        "run_id": "run-v13-div",
        "baseline_sha": "abc111",
        "expected_baseline_sha": "abc999",
    }
    assert observer.verify_rule("V13", divergent_context) is not None

    # Valid passing case: clean worktree and matching baseline
    valid_context = {
        "run_id": "run-v13-ok",
        "uncommitted_files": [],
        "baseline_sha": "83e5298eb231599076811802dceac8575c7f6feb",
        "expected_baseline_sha": "83e5298eb231599076811802dceac8575c7f6feb",
    }
    assert observer.verify_rule("V13", valid_context) is None


# ---------------------------------------------------------------------------
# Test Observer Immutability
# ---------------------------------------------------------------------------

def test_observer_immutability(observer: ContinuousObserver) -> None:
    """ContinuousObserver is strictly read-only and immutable."""
    # Attribute mutation must be forbidden
    with pytest.raises(AttributeError, match="strictly read-only"):
        observer.some_new_attr = "mutation"  # type: ignore[attr-defined]

    with pytest.raises(AttributeError, match="strictly read-only"):
        observer._frozen = False

    with pytest.raises(AttributeError, match="strictly read-only"):
        del observer.rules

    # Audit operations must not mutate context or logs
    context = {
        "run_id": "run-immutable-check",
        "consumer_active": False,
        "health_status_code": 200,
        "tags": ["alpha", "beta"],
    }
    original_copy = {
        "run_id": "run-immutable-check",
        "consumer_active": False,
        "health_status_code": 200,
        "tags": ["alpha", "beta"],
    }
    logs = ["log line 1", "log line 2"]
    logs_copy = list(logs)

    report = observer.audit_run(context, logs=logs)
    assert not report.passed
    assert context == original_copy
    assert logs == logs_copy

    # Verify no mutating methods exist on the observer
    for forbidden_method in ("save", "write", "commit", "update", "delete", "transition_state"):
        assert not hasattr(observer, forbidden_method)


# ---------------------------------------------------------------------------
# Test Correlation of run_id, JobKey, and nonce
# ---------------------------------------------------------------------------

def test_correlation_run_jobkey_and_nonce(observer: ContinuousObserver) -> None:
    """Correlation verifies run_id, JobKey composite key, and nonce uniqueness."""
    jk = JobKey(
        run_id="run-corr-1",
        ticket_id="HF-15-01",
        plan_version="1.0",
        stage="validation",
        iteration=0,
    )
    nonce = "nonce-unique-20260919"

    # Valid correlation
    res = observer.correlate(run_id="run-corr-1", job_key=jk, nonce=nonce)
    assert res.correlated
    assert res.error is None
    assert res.run_id == "run-corr-1"
    assert res.job_key == jk.canonical_key()

    # Mismatched run_id in JobKey vs run_id argument
    res_mismatch = observer.correlate(run_id="run-divergent", job_key=jk, nonce=nonce)
    assert not res_mismatch.correlated
    assert "does not match" in (res_mismatch.error or "")

    # Empty nonce rejected
    res_empty_nonce = observer.correlate(run_id="run-corr-1", job_key=jk, nonce="")
    assert not res_empty_nonce.correlated
    assert "nonce" in (res_empty_nonce.error or "").lower()

    # Empty run_id rejected
    res_empty_run = observer.correlate(run_id="", job_key=jk, nonce=nonce)
    assert not res_empty_run.correlated

    # Dict representation of JobKey supported
    dict_jk = {"run_id": "run-corr-1", "stage": "validation"}
    res_dict = observer.correlate(run_id="run-corr-1", job_key=dict_jk, nonce=nonce)
    assert res_dict.correlated

    # String representation of JobKey supported
    str_jk = "run-corr-1:HF-15-01:1.0:validation:0"
    res_str = observer.correlate(run_id="run-corr-1", job_key=str_jk, nonce=nonce)
    assert res_str.correlated


def test_audit_run_with_correlation(observer: ContinuousObserver) -> None:
    """Audit run verifies end-to-end integration of correlation and 13 rules."""
    jk = JobKey(
        run_id="run-full-pass",
        ticket_id="HF-15-01",
        plan_version="1.0",
        stage="validation",
        iteration=0,
    )
    clean_context = {
        "run_id": "run-full-pass",
        "job_key": jk,
        "nonce": "nonce-safe-9876",
        "consumer_active": True,
        "health_status_code": 200,
        "stage": "validation",
        "candidate_identity": "worker-dev-1",
        "oracle_identity": "oracle-validator-1",
        "clock_skew_seconds": 0.0,
        "uncommitted_files": [],
        "heartbeat_silence_seconds": 5.0,
    }
    report = observer.audit_run(clean_context)
    assert report.passed
    assert len(report.violations) == 0
    assert len(report.evaluated_rules) == 13
    assert report.correlation is not None
    assert report.correlation.correlated
