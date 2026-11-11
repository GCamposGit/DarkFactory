from __future__ import annotations

import sqlite3
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


def test_failure_scope_includes_main_sha_and_closes_on_new_sha(tmp_path):
    store = AdmissionStore(tmp_path / "admission.sqlite3")
    fp_a = make_fingerprint(
        project_id="darkfac", ticket_id="USR-497", candidate_digest="candidate",
        context_digest="context", error="gate_failed", resource_version="notebook",
        main_sha="sha-a",
    )
    store.record_failure(fp_a)
    # Mesmo sha do main: a falha continua bloqueando.
    assert store.is_failure_open(
        project_id="darkfac", ticket_id="USR-497", candidate_digest=fp_a.candidate_digest,
        context_digest=fp_a.context_digest, resource_version=fp_a.resource_version, main_sha="sha-a",
    )
    # Novo sha do main: a falha identica deixa de valer.
    assert not store.is_failure_open(
        project_id="darkfac", ticket_id="USR-497", candidate_digest=fp_a.candidate_digest,
        context_digest=fp_a.context_digest, resource_version=fp_a.resource_version, main_sha="sha-b",
    )
    # Ausencia de main_sha equivale ao escopo legado (vazio), nao ao sha-a.
    assert not store.is_failure_open(
        project_id="darkfac", ticket_id="USR-497", candidate_digest=fp_a.candidate_digest,
        context_digest=fp_a.context_digest, resource_version=fp_a.resource_version,
    )


def test_close_failures_for_other_sha_closes_other_and_legacy_rows(tmp_path):
    store = AdmissionStore(tmp_path / "admission.sqlite3")
    fp_a = make_fingerprint(
        project_id="darkfac", ticket_id="USR-1", candidate_digest="candidate",
        context_digest="context", error="gate_failed", resource_version="notebook",
        main_sha="sha-a",
    )
    fp_legacy = make_fingerprint(
        project_id="darkfac", ticket_id="USR-2", candidate_digest="candidate",
        context_digest="context", error="gate_failed", resource_version="notebook",
    )
    fp_current = make_fingerprint(
        project_id="darkfac", ticket_id="USR-3", candidate_digest="candidate",
        context_digest="context", error="gate_failed", resource_version="notebook",
        main_sha="sha-b",
    )
    store.record_failure(fp_a)
    store.record_failure(fp_legacy)
    store.record_failure(fp_current)

    closed = store.close_failures_for_other_sha("sha-b")
    assert closed == 2

    rows = {row["ticket_id"]: row for row in store.snapshot()}
    assert rows["USR-1"]["open"] == 0
    assert rows["USR-2"]["open"] == 0
    assert rows["USR-3"]["open"] == 1
    assert not store.is_failure_open(
        project_id="darkfac", ticket_id="USR-1", candidate_digest=fp_a.candidate_digest,
        context_digest=fp_a.context_digest, resource_version=fp_a.resource_version, main_sha="sha-a",
    )
    assert store.is_failure_open(
        project_id="darkfac", ticket_id="USR-3", candidate_digest=fp_current.candidate_digest,
        context_digest=fp_current.context_digest, resource_version=fp_current.resource_version, main_sha="sha-b",
    )

    # Rodar de novo com o mesmo sha corrente nao fecha a falha ja vigente nesse sha.
    assert store.close_failures_for_other_sha("sha-b") == 0


def test_prune_closed_failures_only_removes_old_closed_rows(tmp_path):
    now = datetime(2026, 11, 10, tzinfo=timezone.utc)
    store = AdmissionStore(tmp_path / "admission.sqlite3", clock=lambda: now)
    old_a = make_fingerprint(
        project_id="darkfac", ticket_id="USR-OLD-A", candidate_digest="c",
        context_digest="x", error="gate_failed", resource_version="notebook", main_sha="sha-a",
    )
    old_b = make_fingerprint(
        project_id="darkfac", ticket_id="USR-OLD-B", candidate_digest="c",
        context_digest="x", error="gate_failed", resource_version="notebook", main_sha="sha-a",
    )
    store.record_failure(old_a)
    store.record_failure(old_b)
    assert store.close_failures_for_other_sha("sha-b") == 2  # fecha old_a e old_b (sha-a != sha-b)

    recent = make_fingerprint(
        project_id="darkfac", ticket_id="USR-RECENT", candidate_digest="c",
        context_digest="x", error="gate_failed", resource_version="notebook", main_sha="sha-c",
    )
    later = AdmissionStore(store.path, clock=lambda: now + timedelta(days=10))
    later.record_failure(recent)
    assert later.close_failures_for_other_sha("sha-c") == 0  # recent ja esta no sha corrente

    pruner = AdmissionStore(store.path, clock=lambda: now + timedelta(days=10))
    removed = pruner.prune_closed_failures(older_than_days=5)
    assert removed == 2

    remaining = {row["ticket_id"] for row in pruner.snapshot()}
    assert remaining == {"USR-RECENT"}


def test_ticket_hold_is_released_on_new_main_sha_but_kept_on_same_sha(tmp_path):
    store = AdmissionStore(tmp_path / "admission.sqlite3")
    fp = make_fingerprint(
        project_id="darkfac", ticket_id="USR-497", candidate_digest="candidate",
        context_digest="context", error="gate_failed", resource_version="notebook",
        main_sha="sha-a",
    )
    store.set_ticket_hold(fp, occurrences=3)

    hold = store.ticket_hold(
        project_id="darkfac", ticket_id="USR-497", candidate_digest=fp.candidate_digest,
        context_digest=fp.context_digest, main_sha="sha-a",
    )
    assert hold is not None and hold["cause"] == "gate_failed"

    released = store.ticket_hold(
        project_id="darkfac", ticket_id="USR-497", candidate_digest=fp.candidate_digest,
        context_digest=fp.context_digest, main_sha="sha-b",
    )
    assert released is None
    # O hold foi removido: nem uma nova consulta no sha antigo o recupera.
    assert store.ticket_hold(
        project_id="darkfac", ticket_id="USR-497", candidate_digest=fp.candidate_digest,
        context_digest=fp.context_digest, main_sha="sha-a",
    ) is None


def test_opening_a_pre_main_sha_database_does_not_raise(tmp_path):
    path = tmp_path / "admission.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute("""CREATE TABLE failures (
            fingerprint TEXT PRIMARY KEY, project_id TEXT NOT NULL, ticket_id TEXT NOT NULL,
            candidate_digest TEXT NOT NULL, context_digest TEXT NOT NULL,
            error_class TEXT NOT NULL, resource_version TEXT NOT NULL,
            recorded_at TEXT NOT NULL, next_probe_at TEXT NOT NULL,
            backoff_sec INTEGER NOT NULL, open INTEGER NOT NULL DEFAULT 1
        )""")
        connection.execute("""CREATE TABLE ticket_holds (
            project_id TEXT NOT NULL, ticket_id TEXT NOT NULL, cause TEXT NOT NULL,
            candidate_digest TEXT NOT NULL, context_digest TEXT NOT NULL,
            occurrences INTEGER NOT NULL, fingerprint TEXT NOT NULL, held_at TEXT NOT NULL,
            PRIMARY KEY (project_id, ticket_id)
        )""")
        connection.commit()
    finally:
        connection.close()

    store = AdmissionStore(path)
    fp = make_fingerprint(
        project_id="darkfac", ticket_id="USR-497", candidate_digest="candidate",
        context_digest="context", error="gate_failed", resource_version="notebook",
        main_sha="sha-a",
    )
    store.record_failure(fp)
    store.set_ticket_hold(fp, occurrences=1)
    assert store.is_failure_open(
        project_id="darkfac", ticket_id="USR-497", candidate_digest=fp.candidate_digest,
        context_digest=fp.context_digest, resource_version=fp.resource_version, main_sha="sha-a",
    )
    assert store.ticket_holds()[0]["main_sha"] == "sha-a"
