from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from core.line.admission_store import AdmissionStore, make_fingerprint
from core.line.failure_taxonomy import CAUSES, classify
import pytest


def test_infra_backoff_survives_restart_and_new_main_preserves_candidate_debt(tmp_path):
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    path = tmp_path / "admission.sqlite3"
    alerts = []
    for index in range(8):
        store = AdmissionStore(path, clock=lambda: now)
        budget = store.record_attempt("darkfac", "USR-1", "network", run_id=str(index), main_sha="a",
                                      notify=lambda *args: alerts.append(args))
        assert budget["candidate_failures"] == 0
        assert budget["next_attempt_at"] == now.timestamp() + min(600 * 2 ** index, 7200)
        assert store.retry_reason("darkfac", "USR-1", main_sha="a") == "backoff_infra"
        assert not store.ticket_holds()
    assert not alerts
    after = AdmissionStore(path, clock=lambda: now + timedelta(hours=3))
    assert after.retry_reason("darkfac", "USR-1", main_sha="a") == ""
    after.record_attempt("darkfac", "USR-1", "gate_failed", run_id="candidate", main_sha="a")
    after.record_attempt("darkfac", "USR-1", "network", run_id="network", main_sha="a")
    assert after.retry_reason("darkfac", "USR-1", main_sha="b") == ""
    assert after.budget("darkfac", "USR-1")["candidate_failures"] == 1
    assert after.budget("darkfac", "USR-1")["infra_streak"] == 0


def test_same_sha_gate_timeout_twice_holds_with_gate_timeout_repeated(tmp_path):
    store = AdmissionStore(tmp_path / "admission.sqlite3")
    store.record_attempt("darkfac", "USR-2", "gate_timeout", run_id="1", main_sha="a")
    assert not store.ticket_holds()
    store.record_attempt("darkfac", "USR-3", "gate_timeout", run_id="x", main_sha="a")
    store.record_attempt("darkfac", "USR-2", "gate_timeout", run_id="2", main_sha="a")
    assert [(h["ticket_id"], h["cause"]) for h in store.ticket_holds()] == [("USR-2", "gate_timeout_repeated")]
    # a new main sha is a new candidate: one timeout there is only retried
    store.record_attempt("darkfac", "USR-3", "gate_timeout", run_id="y", main_sha="b")
    assert [h["ticket_id"] for h in store.ticket_holds()] == ["USR-2"]


def test_ticket_budget_counts_once_per_run_and_holds_once_across_hosts_and_sha(tmp_path):
    path = tmp_path / "admission.sqlite3"
    alerts = []
    for index in range(4):
        store = AdmissionStore(path)
        kwargs = dict(run_id=f"host-{index}", main_sha=str(index), detail="AssertionError: x", log_path="run.log",
                      notify=lambda *args: alerts.append(args))
        for _ in range(2):
            store.record_attempt("darkfac", "USR-1", "gate_failed", **kwargs)
    assert store.budget("darkfac", "USR-1")["candidate_failures"] == 4
    assert alerts == [("USR-1", "gate_failed", "AssertionError: x", "run.log")]
    assert store.retry_reason("darkfac", "USR-1", main_sha="new-main") == "ticket_hold:gate_failed"
    assert store.ticket_hold(project_id="darkfac", ticket_id="USR-1", candidate_digest="new", context_digest="new")
    assert store.retry_reason("other-project", "USR-1") == ""
    store.reset_budget("darkfac", "USR-1")
    assert store.retry_reason("darkfac", "USR-1") == ""
    assert store.budget("darkfac", "USR-1")["candidate_failures"] == 0


@pytest.mark.parametrize("cause", sorted(CAUSES))
def test_canonical_failures_have_a_known_class(cause):
    assert classify(cause) != "unknown"


@pytest.mark.parametrize("cause", ["review_retry:review_timeout:harness=codex", "focal_timeout",
                                   "review_context_unavailable", "PermissionError: host ACL", "gh failed"])
def test_launcher_legacy_infra_causes_never_consume_candidate_budget(tmp_path, cause):
    store = AdmissionStore(tmp_path / "admission.sqlite3")
    budget = store.record_attempt("darkfac", "USR-1", cause, run_id="legacy")
    assert budget["candidate_failures"] == 0 and budget["infra_streak"] == 1


def test_rebase_inference_is_once_per_fingerprint_and_merge_clears_it(tmp_path):
    store = AdmissionStore(tmp_path / "admission.sqlite3")
    first = make_fingerprint(
        project_id="darkfac", ticket_id="USR-475", candidate_digest="candidate-a",
        context_digest={"head": "head-a", "files": "mod.py"}, error="rebase_conflict",
        resource_version="rebase-recovery", main_sha="main-a",
    )
    changed = make_fingerprint(
        project_id="darkfac", ticket_id="USR-475", candidate_digest="candidate-a",
        context_digest={"head": "head-b", "files": "mod.py"}, error="rebase_conflict",
        resource_version="rebase-recovery", main_sha="main-a",
    )
    assert store.rebase_inference_spent(first) is False
    store.record_rebase_inference(first)
    store.record_rebase_inference(first)
    assert store.rebase_inference_spent(first) is True
    assert store.rebase_inference_spent(changed) is False
    hold = store.record_rebase_technical_hold(
        first, cause="rebase_conflict: mod.py", resume_command="python C:\\dev\\DarkFac\\run_ticket.py --resume-delivery USR-475",
    )
    assert hold["inferred"] == 1 and hold["cause"] == "rebase_conflict"
    assert "resume-delivery" in hold["resume_command"]
    assert store.ticket_hold(project_id="darkfac", ticket_id="USR-475") is None
    restarted = AdmissionStore(tmp_path / "admission.sqlite3")
    assert restarted.rebase_inference_spent(first) is True
    restarted.reset_budget("darkfac", "USR-475")
    assert restarted.rebase_inference_spent(first) is False
    assert restarted.rebase_recovery_rows(project_id="darkfac", ticket_id="USR-475") == []


def test_rebase_recovery_claim_and_result_survive_restart(tmp_path):
    store = AdmissionStore(tmp_path / "admission.sqlite3")
    fingerprint = make_fingerprint(
        project_id="darkfac", ticket_id="USR-476", candidate_digest="candidate",
        context_digest={"worktree": "C:/work/USR-476", "conflict": "motor.md", "evidence": "digest-1"},
        error="rebase_conflict", resource_version="rebase-recovery", main_sha="main-a",
    )

    assert store.begin_rebase_recovery(fingerprint) is True
    assert store.begin_rebase_recovery(fingerprint) is False
    assert store.reserve_rebase_inference(fingerprint) is True
    assert store.reserve_rebase_inference(fingerprint) is False
    saved = store.record_rebase_recovery_result(fingerprint, result="technical_hold")
    assert saved["recovery_started"] == 1
    assert saved["recovery_result"] == "technical_hold"
    assert saved["inferred"] == 1

    restarted = AdmissionStore(tmp_path / "admission.sqlite3")
    resumed = restarted.rebase_recovery_state(fingerprint)
    assert resumed is not None
    assert resumed["recovery_result"] == "technical_hold"
    assert resumed["inferred"] == 1

    changed_evidence = make_fingerprint(
        project_id="darkfac", ticket_id="USR-476", candidate_digest="candidate",
        context_digest={"worktree": "C:/work/USR-476", "conflict": "motor.md", "evidence": "digest-2"},
        error="rebase_conflict", resource_version="rebase-recovery", main_sha="main-a",
    )
    assert restarted.begin_rebase_recovery(changed_evidence) is True
    assert restarted.reserve_rebase_inference(changed_evidence) is True


def test_legacy_store_opens_without_reintroducing_fingerprint_breaker(tmp_path):
    path = tmp_path / "admission.sqlite3"
    store = AdmissionStore(path)
    fp = make_fingerprint(project_id="darkfac", ticket_id="USR-1", error="gate_failed", resource_version="host-a")
    store.record_failure(fp)
    assert AdmissionStore(path).retry_reason("darkfac", "USR-1") == ""


def test_legacy_infra_holds_are_released_but_candidate_holds_survive(tmp_path):
    path = tmp_path / "admission.sqlite3"
    store = AdmissionStore(path)
    for ticket, cause in (("USR-1", "gate_dirty_worktree"), ("USR-2", "gate_failed")):
        store.set_ticket_hold(make_fingerprint(project_id="darkfac", ticket_id=ticket,
                                               error=cause, resource_version="old-host"), 3)
    reopened = AdmissionStore(path)
    assert reopened.retry_reason("darkfac", "USR-1") == ""
    assert reopened.retry_reason("darkfac", "USR-2") == "ticket_hold:gate_failed"


def test_concurrent_attempts_commit_the_third_debit_and_one_hold_together(tmp_path):
    store = AdmissionStore(tmp_path / "admission.sqlite3")
    alerts = []
    def fail(index):
        return store.record_attempt("darkfac", "USR-1", "ci_red", run_id=str(index),
                                    notify=lambda *args: alerts.append(args))
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(fail, range(3)))
    assert store.budget("darkfac", "USR-1")["candidate_failures"] == 3
    assert store.retry_reason("darkfac", "USR-1") == "ticket_hold:ci_red"
    assert len(alerts) == 1


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


