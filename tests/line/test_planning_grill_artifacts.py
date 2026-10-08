"""Planning must not poll a grill whose context files never reached the run's branch.

Production evidence (2026-10-07, `/api/line/live`): three runs of "HF-03-08: Autonomia Operacional
Cloud e Verificacao Continua" had a `grill` job `succeeded` in ~0.05 s (evidence `sha256:`, `lease:`,
`fencing:`: the signature of `scripts/cloud_e2e_task.py`, which finishes the job without git) and then
`planning` answered `retry(grill_not_ready)` 31 times until `failed(loop_cap:grill_not_ready)`.

These tests pin: that shape (grill "succeeded", nothing on the branch), the one-shot recovery
(DEMAND.md re-rendered; grill rescheduled once), the structured fast failure, the grill's refusal to
trust a job commit without artifacts, the guard of the E2E script and that a healthy run is unchanged.
No network; a local bare repository stands in for the project's origin.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from core.line import bindings, stage_grill, stage_planning
from core.line import workspace as ws_mod
from core.line.agent_cli import AgentResult
from core.line.stage_grill import run_grill
from core.line.stage_planning import (
    GRILL_ARTIFACTS_MISSING,
    MAX_GRILL_RESCHEDULES,
    run_planning,
)
from core.projects.models import ProjectDescriptor
from core.workflow import successors
from core.workflow.control_contracts import Claim, JobKey, StageContext
from tests.line.conftest import copy_bare_origin

_PLAN = {
    "spec": {"objective": "Adicionar endpoint /health", "design": "Novo router"},
    "tickets": [{"id": "T1", "title": "Criar endpoint /health"}],
    "is_product_scale": False,
    "milestones": [],
}
_EMPTY_GRILL = {"questions": [], "assumptions": ["Usar Python 3.12"], "is_product_scale": False}


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectDescriptor:
    origin = copy_bare_origin(tmp_path / "origin.git")
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    return ProjectDescriptor(id="acme", name="Acme Project", repo_url=str(origin))


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stage_grill, "default_telegram_sender", lambda: None)


def _reply(payload: dict) -> AgentResult:
    return AgentResult(ok=True, text=json.dumps(payload), harness="claude", duration_s=0.01)


def _git(args: list[str], cwd: Path) -> None:
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(cwd),
        capture_output=True, text=True, encoding="utf-8", errors="replace", **kwargs,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"


class _Humans:
    """Records the HumanRequests planning asks for."""

    def __init__(self, *, fail: bool = False) -> None:
        self.requests: list[Any] = []
        self.fail = fail

    def __call__(self, project: ProjectDescriptor, request: Any) -> None:
        self.requests.append((project.id, request))
        if self.fail:
            raise RuntimeError("telegram down")


def _agent_must_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> AgentResult:
        raise AssertionError("the planning agent must not run without the grill artifacts")

    monkeypatch.setattr(stage_grill, "run_read_agent", _boom)


# --------------------------------------------------------------------------
# The reproduced scenario: grill "succeeded" in the store, nothing on the branch
# --------------------------------------------------------------------------


def test_missing_grill_artifacts_reschedule_the_grill_once_instead_of_polling(project, monkeypatch):
    _agent_must_not_run(monkeypatch)

    result = run_planning(project, "run-fake-grill")

    assert result.outcome == "retry"
    assert result.cause_code == f"retry:grill\n{GRILL_ARTIFACTS_MISSING}"
    # The scheduler routes it as a cross-stage bounce to the grill (not a same-stage planning retry).
    assert successors._parse_retry_cause_code(result.cause_code) == ("grill", None)


def test_missing_grill_artifacts_after_a_reschedule_fail_fast_with_a_structured_cause(project, monkeypatch):
    _agent_must_not_run(monkeypatch)
    humans = _Humans()

    result = run_planning(
        project, "run-fake-grill", grill_reschedules_used=MAX_GRILL_RESCHEDULES, human_requester=humans
    )

    assert result.outcome == "waiting_human"
    assert result.cause_code == GRILL_ARTIFACTS_MISSING
    assert "run:run-fake-grill" in result.evidence_refs
    assert "branch:df/run-fake-grill" in result.evidence_refs
    assert {"missing:DEMAND.md", "missing:GRILL.md"} <= set(result.evidence_refs)
    (project_id, request), = humans.requests
    assert project_id == "acme"
    assert request.kind == "infra" and request.run_id == "run-fake-grill" and request.blocking_stage == "planning"
    assert "df/run-fake-grill" in request.guide_md


def test_a_lost_human_request_does_not_change_the_outcome(project, monkeypatch):
    _agent_must_not_run(monkeypatch)

    result = run_planning(
        project, "run-x", grill_reschedules_used=MAX_GRILL_RESCHEDULES, human_requester=_Humans(fail=True)
    )

    assert result.outcome == "waiting_human" and result.cause_code == GRILL_ARTIFACTS_MISSING


def test_an_empty_grill_file_counts_as_missing(project, monkeypatch):
    _agent_must_not_run(monkeypatch)
    ws = ws_mod.checkout(project, "run-empty")
    ws_mod.write_context(ws, "DEMAND.md", "# DEMAND\n\nx\n")
    ws_mod.write_context(ws, "GRILL.md", "  \n")
    ws_mod.commit(ws, "grill: empty", "run-empty:grill")
    ws_mod.push(ws)

    result = run_planning(project, "run-empty", grill_reschedules_used=MAX_GRILL_RESCHEDULES, human_requester=_Humans())

    assert result.outcome == "waiting_human"
    assert result.evidence_refs[-1] == "missing:GRILL.md"


# --------------------------------------------------------------------------
# Recovery: DEMAND.md is derivable from the intake payload
# --------------------------------------------------------------------------


def test_a_missing_demand_file_is_rebuilt_from_the_intake_payload(project, monkeypatch):
    ws = ws_mod.checkout(project, "run-demand")
    ws_mod.write_context(ws, "GRILL.md", "# GRILL\n\n## Premissas\n- Usar Python 3.12\n")
    ws_mod.commit(ws, "grill: only grill", "run-demand:grill")
    ws_mod.push(ws)
    prompts: list[str] = []

    def _agent(stage: str, prompt: str, *args: Any, **kwargs: Any) -> AgentResult:
        prompts.append(prompt)
        return _reply(_PLAN)

    monkeypatch.setattr(stage_grill, "run_read_agent", _agent)

    result = run_planning(project, "run-demand", demand_text="Adicionar endpoint de health-check")

    assert result.outcome == "success"
    assert len(prompts) == 1 and "Adicionar endpoint de health-check" in prompts[0]
    ws = ws_mod.checkout(project, "run-demand")
    demand = (ws_mod.context_dir(ws) / "DEMAND.md").read_text(encoding="utf-8")
    assert demand.startswith("# DEMAND") and "Adicionar endpoint de health-check" in demand
    assert not stage_grill.grill_artifacts_missing(ws)


def test_a_healthy_run_plans_exactly_as_before(project, monkeypatch):
    ws = ws_mod.checkout(project, "run-ok")
    ws_mod.write_context(ws, "DEMAND.md", "# DEMAND\n\nAdicionar endpoint.\n")
    ws_mod.write_context(ws, "GRILL.md", "# GRILL\n\n- ok\n")
    ws_mod.commit(ws, "grill: seed", "run-ok:grill")
    ws_mod.push(ws)
    calls = {"n": 0}

    def _agent(*args: Any, **kwargs: Any) -> AgentResult:
        calls["n"] += 1
        return _reply(_PLAN)

    monkeypatch.setattr(stage_grill, "run_read_agent", _agent)
    humans = _Humans()

    first = run_planning(project, "run-ok", demand_text="ignored", human_requester=humans)
    second = run_planning(project, "run-ok", demand_text="ignored", human_requester=humans)

    assert first.outcome == "success" and first.output_refs == second.output_refs
    assert calls["n"] == 1 and not humans.requests
    ws = ws_mod.checkout(project, "run-ok")
    assert "Adicionar endpoint." in (ws_mod.context_dir(ws) / "DEMAND.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# The handler: reschedule budget comes from the persisted job history
# --------------------------------------------------------------------------


class _FakeStore:
    def __init__(self, jobs: list[dict[str, Any]] | None, *, has_status: bool = True) -> None:
        self._jobs = jobs or []
        self._has_status = has_status

    def get_run_payload(self, run_id: str) -> dict[str, Any]:
        return {"title": "HF-03-08", "problem": "p", "journey": "j"}

    def __getattr__(self, name: str) -> Any:
        if name == "get_run_status" and self._has_status:
            return lambda run_id: {"jobs": self._jobs}
        raise AttributeError(name)


def _context(run_id: str) -> StageContext:
    key = JobKey(run_id=run_id, ticket_id="acme", plan_version="1.0", stage="planning", iteration=0)
    claim = Claim(job_key=key, lease_id="l1", owner="w1", fencing_token=1, expires_at="2099-01-01T00:00:00+00:00")
    return StageContext(
        claim=claim, plan_ref="plan://line", plan_digest="a" * 64, config_version="1.0",
        environment_ref="env", identity="w1", route_ref="route", memory_version="1.0", input_refs=[],
    )


def _handler(project: ProjectDescriptor, store: Any) -> bindings.PlanningStageHandler:
    return bindings.PlanningStageHandler(
        store=store, host_caps=["harness:claude"], routing_config=None, project_resolver=lambda _pid: project
    )


def test_handler_reschedules_the_grill_when_no_earlier_planning_job_asked_for_it(project, monkeypatch):
    _agent_must_not_run(monkeypatch)
    jobs = [{"stage": "grill", "iteration": 0, "status": "succeeded", "cause_code": None}]

    result = _handler(project, _FakeStore(jobs)).handle(_context("run-h1"))

    assert result.outcome == "retry" and result.cause_code == f"retry:grill\n{GRILL_ARTIFACTS_MISSING}"


def test_handler_parks_the_run_once_a_planning_job_already_bounced_to_the_grill(project, monkeypatch):
    _agent_must_not_run(monkeypatch)
    monkeypatch.setattr(stage_planning, "request_human_help", _Humans())
    jobs = [
        {"stage": "grill", "iteration": 0, "status": "succeeded", "cause_code": None},
        {"stage": "planning", "iteration": 0, "status": "retry", "cause_code": f"retry:grill\n{GRILL_ARTIFACTS_MISSING}"},
        {"stage": "grill", "iteration": 1, "status": "succeeded", "cause_code": None},
    ]

    result = _handler(project, _FakeStore(jobs)).handle(_context("run-h2"))

    assert result.outcome == "waiting_human" and result.cause_code == GRILL_ARTIFACTS_MISSING


def test_handler_without_a_job_history_fails_closed(project, monkeypatch):
    _agent_must_not_run(monkeypatch)
    monkeypatch.setattr(stage_planning, "request_human_help", _Humans())

    result = _handler(project, _FakeStore(None, has_status=False)).handle(_context("run-h3"))

    assert result.outcome == "waiting_human"


def test_handler_rebuilds_the_demand_file_from_the_run_payload(project, monkeypatch):
    ws = ws_mod.checkout(project, "run-h4")
    ws_mod.write_context(ws, "GRILL.md", "# GRILL\n\n- ok\n")
    ws_mod.commit(ws, "grill: only grill", "run-h4:grill")
    ws_mod.push(ws)
    monkeypatch.setattr(stage_grill, "run_read_agent", lambda *a, **k: _reply(_PLAN))

    result = _handler(project, _FakeStore([])).handle(_context("run-h4"))

    assert result.outcome == "success"
    ws = ws_mod.checkout(project, "run-h4")
    assert "HF-03-08" in (ws_mod.context_dir(ws) / "DEMAND.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Grill: a job commit whose artifacts are gone is not trusted
# --------------------------------------------------------------------------


def test_grill_reruns_when_its_job_commit_has_no_artifacts(project, monkeypatch):
    calls = {"n": 0}

    def _agent(*args: Any, **kwargs: Any) -> AgentResult:
        calls["n"] += 1
        return _reply(_EMPTY_GRILL)

    monkeypatch.setattr(stage_grill, "run_read_agent", _agent)
    assert run_grill(project, "run-g", "Demanda simples").outcome == "success"

    ws = ws_mod.checkout(project, "run-g")
    _git(["rm", "-r", "-q", ".darkfac"], ws.path)
    _git(["commit", "-q", "-m", "drop the context files"], ws.path)
    ws_mod.push(ws)
    assert stage_grill.grill_artifacts_missing(ws_mod.checkout(project, "run-g")) == ["DEMAND.md", "GRILL.md"]

    again = run_grill(project, "run-g", "Demanda simples")

    assert again.outcome == "success" and calls["n"] == 2
    assert not stage_grill.grill_artifacts_missing(ws_mod.checkout(project, "run-g"))


def test_grill_with_artifacts_is_still_idempotent(project, monkeypatch):
    calls = {"n": 0}

    def _agent(*args: Any, **kwargs: Any) -> AgentResult:
        calls["n"] += 1
        return _reply(_EMPTY_GRILL)

    monkeypatch.setattr(stage_grill, "run_read_agent", _agent)

    first = run_grill(project, "run-g2", "Demanda simples")
    second = run_grill(project, "run-g2", "Demanda simples")

    assert first.output_refs == second.output_refs and calls["n"] == 1


# --------------------------------------------------------------------------
# Source of the production incident: the E2E script and the ambient production URL
# --------------------------------------------------------------------------


def test_e2e_script_refuses_a_real_control_store_url(monkeypatch):
    from scripts import cloud_e2e_task

    def _never_connect(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("must refuse before constructing (and so connecting to) the store")

    import core.orchestrator.adapters.control_postgres as control_postgres

    monkeypatch.setattr(control_postgres, "PostgresControlStore", _never_connect)
    monkeypatch.setenv("DARKFAC_HF02_DATABASE_URL", "postgresql://worker:pw@db.invalid:5432/darkfac")
    monkeypatch.delenv(cloud_e2e_task.ALLOW_REAL_STORE_ENV, raising=False)

    with pytest.raises(cloud_e2e_task.RealStoreRefusedError):
        cloud_e2e_task.run_e2e_autonomous_task()


def test_e2e_script_refuses_an_injected_real_store(monkeypatch):
    from scripts import cloud_e2e_task

    monkeypatch.delenv(cloud_e2e_task.ALLOW_REAL_STORE_ENV, raising=False)

    class _Real:
        mock_mode = False

    with pytest.raises(cloud_e2e_task.RealStoreRefusedError):
        cloud_e2e_task.run_e2e_autonomous_task(store=_Real())


def test_the_suite_never_inherits_a_production_store_url():
    import os

    for name in ("DARKFAC_HF02_DATABASE_URL", "DARKHUB_LINE_DATABASE_URL", "DARKHUB_CONTROL_DATABASE_URL"):
        assert not os.environ.get(name), f"{name} leaked into the test session (tests/conftest.py must scrub it)"
