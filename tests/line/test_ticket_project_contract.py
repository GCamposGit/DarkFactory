"""USR-303: JobKey.ticket_id is the ticket identity; the project comes from runs.project_id."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.demands.models import UserTicket
from core.line import bindings, owner_intake
from core.orchestrator.cloud_worker import CloudWorker
from core.projects.models import ProjectDescriptor
from core.workflow.control_contracts import StageResult
from core.workflow.control_store import SQLiteControlStore


def _store(tmp_path) -> SQLiteControlStore:
    return SQLiteControlStore(db_path=tmp_path / "control.db")


def _accept(store: SQLiteControlStore, ticket_id: str = "AUDIT-READONLY", project_id: str = "darkfac") -> str:
    ticket = UserTicket(id=ticket_id, project_id=project_id, title="Ticket audit", problem_statement="Problema do ticket")
    receipt = store.accept(owner_intake.command_for_ticket(ticket), datetime.now(UTC))
    assert receipt.run_id
    return receipt.run_id


def test_public_ticket_reaches_grill_handler_of_its_project(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    run_id = _accept(store)
    seen: dict[str, object] = {}

    def fake_run_grill(project, rid, demand_text, **kwargs):
        seen["project"] = project.id
        seen["run_id"] = rid
        return StageResult(outcome="success", output_refs=["grill-ok"])

    monkeypatch.setattr(bindings.stage_grill, "run_grill", fake_run_grill)
    # real default resolver (project registry): NOT a permissive stub
    registry = bindings.build_line_registry(["grill_engine", "git", "harness:any"], store=store)
    worker = CloudWorker(
        worker_id="w1", max_slots=1, store=store,
        capabilities=["grill_engine", "git", "harness:any"], registry=registry,
    )

    assert worker.poll_and_execute_once() is True

    assert seen == {"project": "darkfac", "run_id": run_id}
    status = store.get_run_status(run_id)
    assert status["project_id"] == "darkfac"
    grill = next(j for j in status["jobs"] if j["stage"] == "grill")
    assert grill["ticket_id"] == "AUDIT-READONLY"
    assert store.get_run_payload(run_id)["ticket_id"] == "AUDIT-READONLY"
    assert grill["cause_code"] in (None, "")


def test_accept_is_idempotent_and_keeps_ticket_identity(tmp_path) -> None:
    store = _store(tmp_path)
    first = _accept(store)
    second = _accept(store)
    assert first == second
    status = store.get_run_status(first)
    assert [j["stage"] for j in status["jobs"]] == ["grill"]
    assert status["jobs"][0]["ticket_id"] == "AUDIT-READONLY"


def test_project_for_run_uses_run_project_and_never_the_ticket_id(tmp_path) -> None:
    store = _store(tmp_path)
    run_id = _accept(store)
    asked: list[str] = []
    project = ProjectDescriptor(id="darkfac", name="darkfac", repo_url="https://example.invalid/r.git")

    def resolver(pid: str):
        asked.append(pid)
        return project if pid == "darkfac" else None

    assert bindings.project_for_run(store, run_id, resolver) is project
    assert asked == ["darkfac"]


def test_legacy_run_with_project_id_in_ticket_column_still_resolves(tmp_path) -> None:
    store = _store(tmp_path)
    run_id = _accept(store, ticket_id="USR-1")
    conn = store._connect()
    try:
        conn.execute("UPDATE jobs SET ticket_id = 'darkfac' WHERE run_id = ?", (run_id,))
        conn.commit()
    finally:
        conn.close()
    project = bindings.project_for_run(store, run_id, lambda pid: ProjectDescriptor(
        id=pid, name=pid, repo_url="https://example.invalid/r.git") if pid == "darkfac" else None)
    assert project is not None and project.id == "darkfac"


def test_unknown_project_error_is_preserved_for_unregistered_run_project(tmp_path) -> None:
    store = _store(tmp_path)
    run_id = _accept(store, project_id="no-such-project")
    status = store.get_run_status(run_id)
    from core.workflow.control_contracts import Claim, JobKey, StageContext

    jk = JobKey(run_id=run_id, ticket_id="AUDIT-READONLY", plan_version="1.0", stage="grill", iteration=0)
    claim = Claim(job_key=jk, lease_id="l", owner="w", fencing_token=1, expires_at=datetime.now(UTC).isoformat())
    ctx = StageContext(
        claim=claim, project_id=status["project_id"], plan_ref="p", plan_digest="a" * 64,
        config_version="1.0", environment_ref="e", identity="w", route_ref="r", memory_version="1.0",
    )
    with pytest.raises(bindings.UnknownProjectError):
        bindings._resolve_project(ctx, lambda pid: None)


def test_planning_successor_keeps_persisted_project_and_business_ticket(tmp_path, monkeypatch) -> None:
    from core.workflow.successors import materialize_result

    store = _store(tmp_path)
    run_id = _accept(store, ticket_id="USR-315")
    claim = store.claim("grill-worker", ["grill_engine", "git", "harness:any"], datetime.now(UTC))
    assert claim is not None
    result = StageResult(outcome="success", output_refs=["grill-ok"])
    store.finish(claim, result, datetime.now(UTC))
    materialize_result(claim.job_key, result, store, now=datetime.now(UTC))
    seen = []

    def planning(project, rid, **kwargs):
        seen.append((project.id, rid))
        return StageResult(outcome="success", output_refs=["planning-ok"])

    monkeypatch.setattr(bindings.stage_planning, "run_planning", planning)
    caps = ["planner", "git", "harness:any"]
    worker = CloudWorker(worker_id="planning-worker", max_slots=1, store=store, capabilities=caps,
                         registry=bindings.build_line_registry(caps, store=store))
    monkeypatch.setattr(worker, "_agent_route_unavailable", lambda stage: None)
    assert worker.poll_and_execute_once()
    assert seen == [("darkfac", run_id)]
    jobs = store.get_run_status(run_id)["jobs"]
    assert next(j for j in jobs if j["stage"] == "planning")["ticket_id"] == "USR-315"


@pytest.mark.parametrize("fault,cause", [
    ("unavailable", "project_lookup_unavailable"),
    ("missing", "run_missing"),
    ("empty", "run_project_missing"),
])
def test_failed_identity_lookup_never_resolves_ticket_or_leaves_running_job(
    tmp_path, monkeypatch, fault, cause
) -> None:
    store = _store(tmp_path)
    run_id = _accept(store, ticket_id="USR-315")

    def identity(rid):
        if fault == "unavailable":
            raise RuntimeError("private connection detail")
        return None if fault == "missing" else {"project_id": ""}

    monkeypatch.setattr(store, "get_run_identity", identity)
    resolved = []
    caps = ["grill_engine", "git", "harness:any"]
    registry = bindings.build_line_registry(caps, store=store, project_resolver=lambda pid: resolved.append(pid))
    worker = CloudWorker(worker_id="worker", max_slots=1, store=store, capabilities=caps, registry=registry)
    assert worker.poll_and_execute_once()
    job = store.get_run_status(run_id)["jobs"][0]
    assert job["cause_code"] == cause
    assert job["status"] == ("retry" if fault == "unavailable" else "failed")
    assert not resolved
    assert "private" not in job["cause_code"]


def test_missing_context_project_never_uses_ticket_as_legacy_project(tmp_path) -> None:
    from core.workflow.control_contracts import Claim, JobKey, StageContext

    claim = Claim(job_key=JobKey(run_id="r", ticket_id="darkfac", plan_version="1", stage="planning", iteration=0),
                  lease_id="l", owner="w", fencing_token=1, expires_at=datetime.now(UTC).isoformat())
    context = StageContext(claim=claim, plan_ref="p", plan_digest="a" * 64, config_version="1",
                           environment_ref="e", identity="w", route_ref="r", memory_version="1")
    resolved = []
    with pytest.raises(bindings.ProjectIdentityError, match="run_project_missing"):
        bindings._resolve_project(context, lambda pid: resolved.append(pid))
    assert not resolved


@pytest.mark.parametrize("backend", ["sqlite", "postgres_mock"])
def test_identity_read_is_independent_from_job_telemetry(tmp_path, monkeypatch, backend) -> None:
    from core.orchestrator.adapters.control_postgres import PostgresControlStore

    store = _store(tmp_path) if backend == "sqlite" else PostgresControlStore(mock_mode=True)
    run_id = _accept(store, ticket_id="USR-315", project_id="darkfac")
    monkeypatch.setattr(store, "get_run_status", lambda rid: (_ for _ in ()).throw(RuntimeError("bad telemetry")))
    assert bindings.project_id_for_run(store, run_id, strict=True) == "darkfac"
    assert store.get_run_identity("unknown-run") is None


def test_project_lookup_never_uses_fallback_for_unknown_run(tmp_path) -> None:
    asked = []
    assert bindings.project_for_run(_store(tmp_path), "missing", lambda pid: asked.append(pid)) is None
    assert not asked


def test_registered_projects_with_same_ticket_prefix_resolve_separately(tmp_path) -> None:
    from core.workflow.control_contracts import Claim, JobKey

    store = _store(tmp_path)
    worker = CloudWorker(worker_id="w", max_slots=1, store=store, capabilities=[])
    resolver = bindings.default_project_resolver()
    for pid, tid in [("darkfac", "USR-315"), ("pilot-hello", "USR-316")]:
        run_id = _accept(store, ticket_id=tid, project_id=pid)
        claim = Claim(job_key=JobKey(run_id=run_id, ticket_id=tid, plan_version="1", stage="planning", iteration=0),
                      lease_id="l", owner="w", fencing_token=1, expires_at=datetime.now(UTC).isoformat())
        context = worker._build_stage_context(claim)
        assert context.claim.job_key.ticket_id == tid
        assert bindings._resolve_project(context, resolver).id == pid


def test_postgres_identity_failure_is_not_an_absent_run() -> None:
    from types import SimpleNamespace
    from core.orchestrator.adapters.control_postgres import PostgresControlStore
    from core.workflow.control_contracts import StoreUnavailableError

    store = object.__new__(PostgresControlStore)
    store.mock_mode = False
    store.raw_url = "not-used"

    def unavailable(*args, **kwargs):
        raise RuntimeError("private credential details")

    store._psycopg = SimpleNamespace(connect=unavailable)
    with pytest.raises(StoreUnavailableError, match="^Run identity lookup unavailable$"):
        store.get_run_identity("run")
    with pytest.raises(bindings.ProjectIdentityError, match="project_lookup_unavailable"):
        bindings.project_id_for_run(store, "run", strict=True)
