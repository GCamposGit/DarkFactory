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

    assert bindings.project_for_run(store, run_id, resolver, fallback_id="AUDIT-READONLY") is project
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
