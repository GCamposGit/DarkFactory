"""Tests for the Planning stage (HF-27-04).

Like `test_stage_grill.py`, the agent CLI is faked by monkeypatching
`stage_grill.run_read_agent` (the seam `stage_planning` reuses), so these
tests focus on: deterministic SPEC.md/tickets.json rendering, the
reprompt-once-then-fail(plan_invalid) contract, and idempotent, non-
duplicating submission of milestone children through a fake
`AutonomousIntakeService`-shaped collaborator. No network.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from core.line import stage_grill, workspace as ws_mod
from core.line.agent_cli import AgentResult
from core.line.stage_planning import run_planning
from core.projects.models import ProjectDescriptor
from core.workflow.control_contracts import IdempotencyConflict, IntakeCommand, IntakeReceipt
from tests.line.conftest import copy_bare_origin


# --------------------------------------------------------------------------
# Local git fixtures (self-contained; no network)
# --------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", **kwargs,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc


def _init_bare_origin(tmp_path: Path) -> Path:
    """Fresh local "origin" remote: a directory copy of the shared
    tests/line/conftest.py template (branch main, one README.md seed
    commit) instead of ~8 real git subprocess calls every time.
    """
    return copy_bare_origin(tmp_path / "origin.git")


def _project(repo_url: str) -> ProjectDescriptor:
    return ProjectDescriptor(id="acme", name="Acme Project", repo_url=repo_url)


@pytest.fixture()
def project(tmp_path, monkeypatch) -> ProjectDescriptor:
    origin = _init_bare_origin(tmp_path)
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    return _project(str(origin))


def _seed_grill_outputs(project: ProjectDescriptor, run_id: str) -> None:
    """Write DEMAND.md/GRILL.md as the grill stage would, so planning can run."""
    ws = ws_mod.checkout(project, run_id)
    ws_mod.write_context(ws, "DEMAND.md", "# DEMAND\n\nAdicionar endpoint de health-check.\n")
    ws_mod.write_context(ws, "GRILL.md", "# GRILL\n\n## Premissas\n- Usar Python 3.12\n")
    ws_mod.commit(ws, "grill: seed", f"{run_id}:grill")
    ws_mod.push(ws)


def _fake_agent_reply(payload: dict) -> AgentResult:
    return AgentResult(ok=True, text=json.dumps(payload), harness="claude", duration_s=0.01)


_SMALL_PLAN = {
    "spec": {
        "objective": "Adicionar endpoint /health",
        "out_of_scope": ["autenticacao"],
        "design": "Novo router FastAPI",
        "files_to_touch": ["app/api/health.py"],
        "risks": ["nenhum risco relevante"],
    },
    "tickets": [
        {
            "id": "T1",
            "title": "Criar endpoint /health",
            "goal": "Responder 200 com status ok",
            "files_hint": ["app/api/health.py"],
            "acceptance": ["GET /health retorna 200"],
            "tests_to_add": ["tests/test_health.py"],
            "smoke": ["curl /health"],
        }
    ],
    "is_product_scale": False,
    "milestones": [],
}


# --------------------------------------------------------------------------
# Happy path: SPEC.md + tickets.json rendered and committed
# --------------------------------------------------------------------------


def test_small_demand_produces_spec_and_tickets(project, monkeypatch):
    _seed_grill_outputs(project, "run-1")
    monkeypatch.setattr(stage_grill, "run_read_agent", lambda *a, **k: _fake_agent_reply(_SMALL_PLAN))

    result = run_planning(project, "run-1")

    assert result.outcome == "success"
    assert result.output_refs

    ws = ws_mod.checkout(project, "run-1")
    spec = (ws_mod.context_dir(ws) / "SPEC.md").read_text(encoding="utf-8")
    tickets = json.loads((ws_mod.context_dir(ws) / "tickets.json").read_text(encoding="utf-8"))
    assert "Adicionar endpoint /health" in spec
    assert len(tickets) == 1
    assert tickets[0]["id"] == "T1"
    assert not (ws_mod.context_dir(ws) / "milestones.json").exists()


def test_planning_is_idempotent_on_replay(project, monkeypatch):
    _seed_grill_outputs(project, "run-2")
    calls = {"n": 0}

    def _fake(*args, **kwargs):
        calls["n"] += 1
        return _fake_agent_reply(_SMALL_PLAN)

    monkeypatch.setattr(stage_grill, "run_read_agent", _fake)

    first = run_planning(project, "run-2")
    second = run_planning(project, "run-2")

    assert first.output_refs == second.output_refs
    assert calls["n"] == 1


def test_planning_retries_when_grill_not_ready(project):
    # No DEMAND.md/GRILL.md committed yet for this run.
    result = run_planning(project, "run-3")
    assert result.outcome == "retry"
    assert result.cause_code == "grill_not_ready"


# --------------------------------------------------------------------------
# Invalid JSON: reprompt once, then failed(plan_invalid)
# --------------------------------------------------------------------------


def test_invalid_json_twice_fails_with_plan_invalid(project, monkeypatch):
    _seed_grill_outputs(project, "run-4")
    calls = {"n": 0}

    def _fake(*args, **kwargs):
        calls["n"] += 1
        return AgentResult(ok=True, text="not json at all", harness="claude", duration_s=0.01)

    monkeypatch.setattr(stage_grill, "run_read_agent", _fake)

    result = run_planning(project, "run-4")

    assert result.outcome == "failed"
    assert result.cause_code == "plan_invalid"
    assert calls["n"] == 2  # one attempt + one reprompt


def test_invalid_json_then_valid_on_reprompt_succeeds(project, monkeypatch):
    _seed_grill_outputs(project, "run-5")
    calls = {"n": 0}

    def _fake(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return AgentResult(ok=True, text="garbage", harness="claude", duration_s=0.01)
        return _fake_agent_reply(_SMALL_PLAN)

    monkeypatch.setattr(stage_grill, "run_read_agent", _fake)

    result = run_planning(project, "run-5")

    assert result.outcome == "success"
    assert calls["n"] == 2


# --------------------------------------------------------------------------
# Product-scale demand: N-1 child demands, submitted once, no duplication
# --------------------------------------------------------------------------


class _FakeIntakeService:
    """In-memory stand-in for AutonomousIntakeService.accept()'s idempotency contract."""

    def __init__(self) -> None:
        self._by_external_id: dict[str, tuple[str, IntakeReceipt]] = {}
        self.accept_calls: list[IntakeCommand] = []

    def accept(self, command: IntakeCommand, now: Optional[datetime] = None) -> IntakeReceipt:
        self.accept_calls.append(command)
        digest = command.payload_digest
        existing = self._by_external_id.get(command.external_id)
        if existing is not None:
            existing_digest, receipt = existing
            if existing_digest != digest:
                raise IdempotencyConflict(
                    f"external_id {command.external_id} resubmitted with a different payload"
                )
            return receipt
        receipt = IntakeReceipt(
            demand_id=f"demand-{command.external_id}",
            demand_version="1",
            run_id=f"run-{command.external_id}",
            initial_job_id=f"job-{command.external_id}",
            mode="autonomous",
            committed_at=(now or datetime.now(timezone.utc)).isoformat(),
        )
        self._by_external_id[command.external_id] = (digest, receipt)
        return receipt


_PRODUCT_SCALE_PLAN = {
    "spec": {
        "objective": "Lancar produto novo de agendamento",
        "out_of_scope": [],
        "design": "Marco 1: cadastro de usuarios",
        "files_to_touch": [],
        "risks": [],
    },
    "tickets": [
        {
            "id": "T1",
            "title": "Cadastro de usuarios",
            "goal": "Permitir criar conta",
            "files_hint": [],
            "acceptance": ["usuario consegue se cadastrar"],
            "tests_to_add": [],
            "smoke": [],
        }
    ],
    "is_product_scale": True,
    "milestones": [
        {"title": "Agenda e reservas", "demand_text": "Implementar agenda de horarios e reservas."},
        {"title": "Notificacoes", "demand_text": "Enviar lembretes por e-mail e SMS."},
    ],
}


def test_product_scale_demand_submits_n_minus_1_children_once(project, monkeypatch):
    _seed_grill_outputs(project, "run-6")
    monkeypatch.setattr(
        stage_grill, "run_read_agent", lambda *a, **k: _fake_agent_reply(_PRODUCT_SCALE_PLAN)
    )
    intake = _FakeIntakeService()

    result = run_planning(project, "run-6", intake_service=intake)

    assert result.outcome == "success"
    assert len(intake.accept_calls) == 2
    external_ids = {cmd.external_id for cmd in intake.accept_calls}
    assert external_ids == {"run-6:m2", "run-6:m3"}
    for cmd in intake.accept_calls:
        assert cmd.payload["depends_on"] == "run-6"
        assert "Usar Python 3.12" in cmd.payload["parent_grill"]

    ws = ws_mod.checkout(project, "run-6")
    milestones = json.loads((ws_mod.context_dir(ws) / "milestones.json").read_text(encoding="utf-8"))
    assert len(milestones) == 2
    tickets = json.loads((ws_mod.context_dir(ws) / "tickets.json").read_text(encoding="utf-8"))
    assert len(tickets) == 1  # only milestone 1's tickets belong to this run


def test_product_scale_replay_does_not_duplicate_children(project, monkeypatch):
    _seed_grill_outputs(project, "run-7")
    monkeypatch.setattr(
        stage_grill, "run_read_agent", lambda *a, **k: _fake_agent_reply(_PRODUCT_SCALE_PLAN)
    )
    intake = _FakeIntakeService()

    first = run_planning(project, "run-7", intake_service=intake)
    assert first.outcome == "success"
    assert len(intake.accept_calls) == 2

    # Replaying the whole stage (e.g. reconciler retry) must not resubmit —
    # the planning commit already exists, so it short-circuits before ever
    # calling the agent or the intake service again.
    second = run_planning(project, "run-7", intake_service=intake)
    assert second.outcome == "success"
    assert second.output_refs == first.output_refs
    assert len(intake.accept_calls) == 2

    # And even if the intake service itself were called again with the same
    # external_id/payload, it must not create a duplicate receipt.
    command = intake.accept_calls[0]
    receipt_again = intake.accept(command)
    assert receipt_again == intake._by_external_id[command.external_id][1]
