"""USR-329: residues of USR-320 -- ticket ids are never a project identity."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

from core.demands.models import UserTicket
from core.line import bindings, owner_intake
from core.orchestrator.cloud_worker import CloudWorker
from core.workflow.control_store import SQLiteControlStore


def _accept(store, ticket_id: str, project_id: str) -> str:
    ticket = UserTicket(id=ticket_id, project_id=project_id, title="Ticket USR", problem_statement="Problema do ticket")
    return store.accept(owner_intake.command_for_ticket(ticket), datetime.now(UTC)).run_id


def test_project_for_run_has_no_fallback_argument() -> None:
    assert "fallback_id" not in inspect.signature(bindings.project_for_run).parameters


def test_project_for_run_ignores_ticket_id_as_identity(tmp_path) -> None:
    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    run_id = _accept(store, "USR-329", "darkfac")
    asked: list[str] = []
    bindings.project_for_run(store, run_id, lambda pid: asked.append(pid))
    assert asked == ["darkfac"]


def test_no_route_branch_unregistered_project_fails_unknown_project(tmp_path, monkeypatch) -> None:
    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    run_id = _accept(store, "USR-329", "no-such-project")
    caps = ["grill_engine", "git", "harness:any"]
    worker = CloudWorker(worker_id="w", max_slots=1, store=store, capabilities=caps,
                         registry=bindings.build_line_registry(caps, store=store))
    monkeypatch.setattr(worker, "_agent_route_unavailable", lambda stage: "no_authenticated_harness")
    monkeypatch.setattr(worker, "_project_for", lambda claim: None)
    assert worker.poll_and_execute_once()
    job = store.get_run_status(run_id)["jobs"][0]
    assert job["status"] == "failed"
    assert job["cause_code"] == "unknown_project"
