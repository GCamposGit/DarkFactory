from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from core.line.admission_store import AdmissionStore, make_fingerprint


def test_fingerprint_is_stable_sensitive_text_is_not_persisted_and_dimensions_reopen(tmp_path):
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    store = AdmissionStore(tmp_path / "admission.sqlite3", clock=lambda: now)
    fp = make_fingerprint(
        project_id="darkfac", ticket_id="USR-468", candidate_digest="candidate-a",
        context_digest="context-a", error="scanner failed with token=secret-value",
        resource_version="notebook:workers=0",
    )
    same = make_fingerprint(
        project_id="darkfac", ticket_id="USR-468", candidate_digest="candidate-a",
        context_digest="context-a", error="scanner failed with token=another-secret",
        resource_version="notebook:workers=0",
    )
    assert fp.sha256 == same.sha256
    store.record_failure(fp, initial_backoff_sec=30)
    assert store.is_failure_open(
        project_id="darkfac", ticket_id="USR-468", candidate_digest=fp.candidate_digest,
        context_digest=fp.context_digest, resource_version=fp.resource_version,
    )
    assert not store.is_failure_open(
        project_id="darkfac", ticket_id="USR-468", candidate_digest="changed-candidate",
        context_digest=fp.context_digest, resource_version=fp.resource_version,
    )
    snapshot = store.snapshot()
    assert "secret-value" not in str(snapshot) and "another-secret" not in str(snapshot)
    assert snapshot[0]["fingerprint"] == fp.sha256


def test_state_survives_store_recreation_and_backoff_progresses(tmp_path):
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    path = tmp_path / "admission.sqlite3"
    fp = make_fingerprint(
        project_id="darkfac", ticket_id="USR-1", baseline_sha="base", context_digest="ctx",
        error=RuntimeError("network unavailable"), resource_version="host:healthy",
    )
    first = AdmissionStore(path, clock=lambda: now)
    first.record_failure(fp, initial_backoff_sec=20)
    next_probe = first.snapshot()[0]["next_probe_at"]
    second = AdmissionStore(path, clock=lambda: now + timedelta(seconds=20))
    second.record_failure(fp, initial_backoff_sec=20)
    row = second.snapshot()[0]
    assert row["backoff_sec"] == 40
    assert row["next_probe_at"] != next_probe
    assert row["fingerprint"] == fp.sha256


def test_concurrent_dispatchers_get_at_most_one_reservation(tmp_path):
    path = tmp_path / "admission.sqlite3"
    stores = [AdmissionStore(path), AdmissionStore(path)]

    def claim(store):
        return store.reserve(
            project_id="darkfac", ticket_id="USR-2", candidate_digest="candidate",
            context_digest="context", resource_version="host-v1",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, stores))
    assert sum(token is not None for token in claims) == 1


def test_resource_change_uses_a_separate_failure_scope_and_reservation(tmp_path):
    store = AdmissionStore(tmp_path / "admission.sqlite3")
    fp = make_fingerprint(
        project_id="darkfac", ticket_id="USR-3", candidate_digest="candidate",
        context_digest="context", error="quota unavailable", resource_version="receipt-1",
    )
    store.record_failure(fp)
    assert not store.is_failure_open(
        project_id="darkfac", ticket_id="USR-3", candidate_digest=fp.candidate_digest,
        context_digest=fp.context_digest, resource_version="receipt-2",
    )
    assert not store.is_failure_open(
        project_id="darkfac", ticket_id="USR-3", candidate_digest=fp.candidate_digest,
        context_digest=fp.context_digest, resource_version=fp.resource_version,
        error_class="different_error_class",
    )
    assert store.reserve(
        project_id="darkfac", ticket_id="USR-3", candidate_digest=fp.candidate_digest,
        context_digest=fp.context_digest, resource_version="receipt-2",
    ) is not None


def test_recovery_latch_and_single_global_lease_survive_restarts(tmp_path):
    path = tmp_path / "admission.sqlite3"
    first = AdmissionStore(path)
    token = first.reserve_recovery()
    assert token
    restarted = AdmissionStore(path)
    assert restarted.reserve_recovery() is None
    state = restarted.set_recovery(active=True, cause="context", fingerprint="fp")
    assert state["active"] == 1 and state["cause"] == "context" and state["fingerprint"] == "fp"
    restarted.release_recovery("stale-token")
    assert restarted.reserve_recovery() is None
    restarted.release_recovery(token)
    assert restarted.reserve_recovery() is not None


def test_recovery_receipt_cause_and_completion_are_durable_and_idempotent(tmp_path):
    path = tmp_path / "admission.sqlite3"
    store = AdmissionStore(path)
    store.set_recovery(active=True, cause="context", fingerprint="fp")
    state = store.record_recovery_receipt_cause("receipt_ci_stale")
    assert state["active"] == 1 and state["receipt_cause"] == "receipt_ci_stale"
    state = store.set_recovery(active=True, cause="context", fingerprint="fp")
    assert state["receipt_cause"] == "receipt_ci_stale"

    completed = store.complete_recovery(receipt_digest="receipt-hash", candidate_digest="candidate-468")
    assert completed["active"] == 0 and completed["successes"] == 1
    assert completed["receipt_digest"] == "receipt-hash"
    assert completed["recovered_candidate_digest"] == "candidate-468"
    restarted = AdmissionStore(path)
    assert restarted.recovery_state() == completed
    replay = restarted.complete_recovery(receipt_digest="receipt-hash", candidate_digest="candidate-468")
    assert replay["successes"] == 1
    restarted.set_recovery(active=True, cause="new_incident", fingerprint="fp-2")
    reused = restarted.complete_recovery(receipt_digest="receipt-hash", candidate_digest="candidate-468")
    assert reused["active"] == 1 and reused["successes"] == 1
