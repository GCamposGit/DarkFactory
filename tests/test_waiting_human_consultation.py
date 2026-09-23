"""Tests for DH-03 (USR-47): Read-only WAITING_HUMAN consultation in DarkHub.

Validates that:
1. `core/workflow/job_board.py` projects WAITING_HUMAN jobs with proper cause_code
   and diagnostic information (e.g. Grill alignment or pending manual dependencies).
2. `hub/frontend/tasks.js` is strictly read-only (zero POST/PATCH/DELETE mutation calls)
   and renders the observation card at the top of the Operational Queue.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.workflow.control_store import SQLiteControlStore
from core.workflow.job_board import (
    JobBoardEntry,
    fold_job_rows,
    read_sqlite_job_board,
)
from hub.backend.service import HubService

ROOT = Path(__file__).resolve().parent.parent
TASKS_JS_PATH = ROOT / "hub" / "frontend" / "tasks.js"
NOW = "2026-09-23T15:00:00+00:00"


def _make_service(tmp_path: Path, control_db: Path) -> HubService:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "default_services.json").write_text("[]", encoding="utf-8")
    (data_dir / "default_prompts.json").write_text("[]", encoding="utf-8")
    return HubService(
        data_dir=data_dir,
        usage_dir=tmp_path / "usage",
        roadmap_root=ROOT,
        state_path=tmp_path / "state.json",
        orchestrator_path=tmp_path / "orchestrator.sqlite3",
        control_db_path=control_db,
        control_database_url="",
    )


def test_job_board_projects_waiting_human_grill_stage_diagnostic() -> None:
    """A WAITING_HUMAN job in the grill stage without cause_code gets grill diagnostic."""
    rows = [
        {
            "run_id": "run-grill-1",
            "ticket_id": "USR-47",
            "project_id": "darkfac",
            "demand_id": "USR-47",
            "mode": "autonomous",
            "run_status": "active",
            "stage": "grill",
            "status": "waiting_human",
            "role": "grill_engine",
            "iteration": 0,
            "cause_code": None,
            "actual_cost": 0.05,
            "evidence_refs": json.dumps(["grill_deadline:2026-09-24T12:00:00Z"]),
            "updated_at": NOW,
            "intake_payload": json.dumps({"title": "Ticket de Demonstração DH-03"}),
        }
    ]

    entries = fold_job_rows(rows)
    assert len(entries) == 1
    entry = entries[0]

    assert entry.status == "waiting_human"
    assert entry.needs_attention is True
    assert entry.cause_code == "grill_pending"
    assert entry.diagnostic is not None
    assert "Grill" in entry.diagnostic
    assert entry.title == "Ticket de Demonstração DH-03"


def test_job_board_projects_waiting_human_dependency_stage_diagnostic() -> None:
    """A WAITING_HUMAN job suspended due to manual dependencies gets dependency diagnostic."""
    rows = [
        {
            "run_id": "run-dep-1",
            "ticket_id": "USR-48",
            "project_id": "darkfac",
            "demand_id": "USR-48",
            "mode": "autonomous",
            "run_status": "active",
            "stage": "waiting_dependency",
            "status": "waiting_human",
            "role": "developer",
            "iteration": 1,
            "cause_code": None,
            "actual_cost": 0.20,
            "evidence_refs": json.dumps(["dependency://postgres-cluster"]),
            "updated_at": NOW,
            "intake_payload": None,
        }
    ]

    entries = fold_job_rows(rows)
    assert len(entries) == 1
    entry = entries[0]

    assert entry.status == "waiting_human"
    assert entry.cause_code == "waiting_dependency"
    assert entry.diagnostic is not None
    assert "Dependência" in entry.diagnostic or "depend" in entry.diagnostic.lower()


def test_job_board_preserves_explicit_cause_code() -> None:
    """Explicit cause_code on WAITING_HUMAN is preserved with diagnostic info."""
    rows = [
        {
            "run_id": "run-custom-1",
            "ticket_id": "USR-49",
            "project_id": "darkfac",
            "demand_id": "USR-49",
            "mode": "autonomous",
            "run_status": "active",
            "stage": "development",
            "status": "waiting_human",
            "role": "developer",
            "iteration": 0,
            "cause_code": "missing_secret",
            "actual_cost": 0.15,
            "evidence_refs": "[]",
            "updated_at": NOW,
            "intake_payload": None,
        }
    ]

    entries = fold_job_rows(rows)
    assert len(entries) == 1
    entry = entries[0]

    assert entry.status == "waiting_human"
    assert entry.cause_code == "missing_secret"
    assert entry.diagnostic is not None
    assert "missing_secret" in entry.diagnostic


def test_job_board_entry_model_validator_guarantees_diagnostic() -> None:
    """JobBoardEntry validator enforces cause_code and diagnostic on WAITING_HUMAN."""
    entry = JobBoardEntry(
        run_id="run-val-1",
        ticket_id="USR-50",
        project_id="darkfac",
        demand_id="USR-50",
        mode="autonomous",
        run_status="active",
        stage="grill",
        status="waiting_human",
        role="grill_engine",
        iteration=0,
    )

    assert entry.cause_code == "grill_pending"
    assert entry.diagnostic is not None
    assert "Grill" in entry.diagnostic


def test_sqlite_job_board_waiting_human_projection(tmp_path: Path) -> None:
    """Read a real SQLite control store with a WAITING_HUMAN job."""
    db_path = tmp_path / "control.db"
    store = SQLiteControlStore(db_path=db_path)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, project_id, demand_id, demand_version, runtime_owner, mode, status,"
            " plan_digest, config_version, created_at, updated_at)"
            " VALUES ('run-wh-1', 'darkfac', 'USR-47', 'v1', 'hf05_sqlite', 'autonomous', 'active', 'd', 'c', ?, ?)",
            (NOW, NOW),
        )
        conn.execute(
            "INSERT INTO jobs (run_id, ticket_id, plan_version, stage, iteration, status, role, cause_code,"
            " actual_cost, evidence_refs, created_at, updated_at)"
            " VALUES ('run-wh-1', 'USR-47', 'p1', 'grill', 0, 'waiting_human', 'grill_engine', NULL, 0.05, ?, ?, ?)",
            (json.dumps(["grill_deadline:2026-09-24T12:00:00Z"]), NOW, NOW),
        )
        conn.commit()

    snapshot = read_sqlite_job_board(db_path)
    assert snapshot.source == "ok"
    assert len(snapshot.entries) == 1
    entry = snapshot.entries[0]
    assert entry.status == "waiting_human"
    assert entry.cause_code == "grill_pending"
    assert entry.diagnostic is not None
    assert "Grill" in entry.diagnostic


def test_hub_service_exposes_waiting_human_diagnostic(tmp_path: Path) -> None:
    """HubService.get_task_dashboard includes diagnostic and cause_code for WAITING_HUMAN."""
    db_path = tmp_path / "control.db"
    store = SQLiteControlStore(db_path=db_path)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, project_id, demand_id, demand_version, runtime_owner, mode, status,"
            " plan_digest, config_version, created_at, updated_at)"
            " VALUES ('run-svc-1', 'darkfac', 'USR-47', 'v1', 'hf05_sqlite', 'autonomous', 'active', 'd', 'c', ?, ?)",
            (NOW, NOW),
        )
        conn.execute(
            "INSERT INTO jobs (run_id, ticket_id, plan_version, stage, iteration, status, role, cause_code,"
            " actual_cost, evidence_refs, created_at, updated_at)"
            " VALUES ('run-svc-1', 'USR-47', 'p1', 'grill', 0, 'waiting_human', 'grill_engine', NULL, 0.05, '[]', ?, ?)",
            (NOW, NOW),
        )
        conn.commit()

    service = _make_service(tmp_path, db_path)
    report = service.get_task_dashboard()

    assert len(report.queue) == 1
    first = report.queue[0]
    assert first.status == "WAITING_HUMAN"
    assert first.cause_code == "grill_pending"
    assert first.diagnostic is not None
    assert "Grill" in first.diagnostic
    assert any(diag_ev.label == "diagnóstico" for diag_ev in first.evidence)


def test_tasks_js_strictly_read_only() -> None:
    """hub/frontend/tasks.js must be strictly read-only with no mutative calls or elements."""
    assert TASKS_JS_PATH.exists(), f"tasks.js not found at {TASKS_JS_PATH}"
    content = TASKS_JS_PATH.read_text(encoding="utf-8")

    # 1. No HTTP mutation verbs in fetch / requests
    forbidden_methods = re.findall(
        r"""method\s*:\s*["'](POST|PATCH|DELETE|PUT)["']""",
        content,
        re.IGNORECASE,
    )
    assert not forbidden_methods, f"Mutative HTTP methods found in tasks.js: {forbidden_methods}"

    # 2. No mutation URLs like /resolve, /cancel, /answer, /resume, /abort
    mutative_endpoints = re.findall(
        r"""["'](?:/[^"']*)?/(?:resolve|cancel|answer|resume|abort|mutate|action|submit)[^"']*["']""",
        content,
        re.IGNORECASE,
    )
    assert not mutative_endpoints, f"Mutative endpoints found in tasks.js: {mutative_endpoints}"

    # 3. No forms or submit inputs in the generated markup
    forbidden_elements = re.findall(r"""<form[\s>]|<input\s+type=["']submit["']|<textarea|<select""", content)
    assert not forbidden_elements, f"Interactive form elements found in tasks.js: {forbidden_elements}"

    # 4. No action buttons designed to mutate workflow state
    action_buttons = re.findall(r"""<button[^>]*class=["'][^"']*(?:resolve|resume|cancel|submit)[^"']*["']""", content, re.IGNORECASE)
    assert not action_buttons, f"Mutation action buttons found in tasks.js: {action_buttons}"


def test_tasks_js_renders_waiting_human_observation_card() -> None:
    """hub/frontend/tasks.js must render the informative observation card for WAITING_HUMAN jobs."""
    assert TASKS_JS_PATH.exists()
    content = TASKS_JS_PATH.read_text(encoding="utf-8")

    # 1. Visual indicator in prominent display
    assert "Aguardando Decisão do Owner (WAITING_HUMAN)" in content

    # 2. Canonical terminal resolution instructions
    assert "Resolução pelo terminal canônico: python -m core.demands.cli grill" in content
    assert "python -m core.demands.cli grill <ticket_id>" in content

    # 3. Dynamic binding of demand title, run ID and suspension reason
    assert "item.title" in content or "title" in content
    assert "item.run_id" in content or "runId" in content
    assert "Motivo da Suspensão" in content
    assert "Somente Leitura" in content
