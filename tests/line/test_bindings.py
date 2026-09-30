"""Tests for core.line.bindings (HF-27-08 items 3, 8, 9).

No network, no real git remote, no real agent CLI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.line import bindings
from core.projects.models import DeployConfig, DeployTargetType, ProjectDescriptor
from core.workflow.control_contracts import Claim, JobKey, StageContext
from core.workflow.control_store import SQLiteControlStore


def _ctx(stage: str, *, ticket_id: str = "acme", run_id: str = "run-x", input_refs: list[str] | None = None) -> StageContext:
    jk = JobKey(run_id=run_id, ticket_id=ticket_id, plan_version="1.0", stage=stage, iteration=0)
    claim = Claim(
        job_key=jk, lease_id="lease-1", owner="w1", fencing_token=1,
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    return StageContext(
        claim=claim, plan_ref="plan://line", plan_digest="a" * 64, config_version="1.0",
        environment_ref="env", identity="w1", route_ref="route", memory_version="1.0",
        input_refs=input_refs or [],
    )


# --------------------------------------------------------------------------
# Item 3: the line registry is fail-closed for anything outside LINE_STAGES
# --------------------------------------------------------------------------


def test_registry_only_contains_line_stages() -> None:
    store = SQLiteControlStore(":memory:")
    registry = bindings.build_line_registry(["git", "harness:any"], store=store)
    keys = {stage for stage, _version in registry.keys()}
    assert keys == set(bindings.LINE_STAGES)


@pytest.mark.parametrize("legacy_stage", ["research", "catalog_refresh", "memory_observation", "learning_eval", "environment"])
def test_legacy_stage_dispatch_is_missing_handler(legacy_stage: str) -> None:
    store = SQLiteControlStore(":memory:")
    registry = bindings.build_line_registry(["git", "harness:any"], store=store)
    result = registry.dispatch(_ctx(legacy_stage))
    assert result.outcome == "failed"
    assert result.cause_code == "missing_handler"
    assert result.output_refs == []


# --------------------------------------------------------------------------
# Item 9 / required_caps(): a registered project's grill/development caps
# --------------------------------------------------------------------------


_REPO_URL = "https://github.com/acme/acme.git"


def test_required_caps_agent_stages_need_git_and_harness_any() -> None:
    project = ProjectDescriptor(id="acme", name="Acme", repo_url=_REPO_URL)
    for stage in ("grill", "planning", "development", "independent_review", "integration"):
        caps = bindings.required_caps(project, stage)
        assert "git" in caps
        assert "harness:any" in caps


def test_required_caps_validation_needs_git_no_harness() -> None:
    project = ProjectDescriptor(id="acme", name="Acme", repo_url=_REPO_URL)
    caps = bindings.required_caps(project, "validation")
    assert "git" in caps
    assert "harness:any" not in caps


def test_required_caps_build_deploy_local_service_needs_target_capability() -> None:
    project = ProjectDescriptor(
        id="acme", name="Acme", repo_url=_REPO_URL, deploy=DeployConfig(type=DeployTargetType.LOCAL_SERVICE, params={})
    )
    caps = bindings.required_caps(project, "build_deploy")
    assert "target:local_service" in caps
    assert "harness:any" not in caps  # build_deploy itself needs no agent harness


def test_required_caps_retrospective_needs_only_git() -> None:
    project = ProjectDescriptor(id="acme", name="Acme", repo_url=_REPO_URL)
    assert bindings.required_caps(project, "retrospective") == ["git"]


def test_required_caps_no_repo_url_is_never_gated() -> None:
    """A project with no repo_url can never run through the line
    (workspace.checkout requires one), so gating it would only starve a
    non-line project (e.g. the default 'darkfac' registry entry, used by
    many pre-existing non-line ControlStore.accept() callers/tests) of
    workers. required_caps() must stay a no-op for it, as before HF-27-08."""
    project = ProjectDescriptor(id="darkfac", name="Dark Factory (Core)")
    assert project.repo_url is None
    for stage in bindings.LINE_STAGES:
        assert bindings.required_caps(project, stage) == []


# --------------------------------------------------------------------------
# Item 8: retrospective never pushes to the target repo, records a summary
# --------------------------------------------------------------------------


def test_retrospective_never_pushes_and_records_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    store = SQLiteControlStore(":memory:")

    def _boom_run_git(*args, **kwargs):
        raise AssertionError("retrospective must never call workspace git helpers (no push to target repo)")

    import core.line.workspace as ws_mod

    monkeypatch.setattr(ws_mod, "_run_git", _boom_run_git)
    monkeypatch.setattr(ws_mod, "_ensure_mirror", _boom_run_git)

    recorded: dict = {}

    class _FakeTelemetryStore:
        def __init__(self) -> None:
            pass

        def record(self, record):
            recorded["record"] = record

    monkeypatch.setattr("core.telemetry.store.TelemetryStore", _FakeTelemetryStore)

    handler = bindings.RetrospectiveStageHandler(store=store, project_resolver=lambda pid: ProjectDescriptor(id=pid, name=pid))
    result = handler.handle(_ctx("retrospective", input_refs=["op1", "smoke1"]))

    assert result.outcome == "success"
    assert result.output_refs == ["retrospective:run-x"]
    assert "record" in recorded
    assert recorded["record"].ticket_id == "run-x"


# --------------------------------------------------------------------------
# Grill handler: retry attempts adopt the previous attempt's grill
# --------------------------------------------------------------------------


class _GrillStore:
    """Minimal control-store surface the grill handler and owner_intake read/write."""

    def __init__(self, payloads: dict[str, dict], answers: dict[str, dict[str, str]]) -> None:
        self.payloads = payloads
        self.answers = answers
        self.recorded: list[tuple[str, str, str]] = []

    def get_run_payload(self, run_id):
        return self.payloads.get(run_id)

    def find_intake_runs(self, channel, external_id):
        return [("ticket:USR-62", "run-a1"), ("ticket:USR-62:a2", "run-a2")]

    def get_grill_answers(self, run_id):
        return dict(self.answers.get(run_id, {}))

    def record_grill_answer(self, run_id, question_id, choice, now):
        self.recorded.append((run_id, question_id, choice))


def _grill_handler(store):
    project = ProjectDescriptor(id="acme", name="Acme", repo_url="https://example.invalid/acme.git")
    return bindings.GrillStageHandler(
        store=store, host_caps=["git"], routing_config=None, project_resolver=lambda pid: project
    )


_BASE_PAYLOAD = {"title": "T", "problem": "P", "journey": ["j"], "ticket_id": "USR-62"}


def test_grill_handler_passes_earlier_attempts_and_carries_over_answers_on_adoption(monkeypatch) -> None:
    from core.line import stage_grill
    from core.workflow.control_contracts import StageResult

    store = _GrillStore(
        payloads={"run-a1": dict(_BASE_PAYLOAD), "run-a2": {**_BASE_PAYLOAD, "attempt": 2}},
        answers={"run-a1": {"q1": "verde", "q2": "sim"}},
    )
    seen: dict = {}

    def _fake_run_grill(project, run_id, demand_text, **kwargs):
        seen.update(kwargs, run_id=run_id)
        return StageResult(
            outcome="success", output_refs=["abc"], evidence_refs=[f"{stage_grill.ADOPTED_EVIDENCE_PREFIX}run-a1"]
        )

    monkeypatch.setattr(stage_grill, "run_grill", _fake_run_grill)

    result = _grill_handler(store).handle(_ctx("grill", ticket_id="acme", run_id="run-a2"))

    assert result.outcome == "success"
    assert seen["adopt_from_runs"] == ["run-a1"]
    assert sorted(store.recorded) == [("run-a2", "q1", "verde"), ("run-a2", "q2", "sim")]


def test_grill_handler_does_not_copy_answers_when_nothing_was_adopted(monkeypatch) -> None:
    from core.line import stage_grill
    from core.workflow.control_contracts import StageResult

    store = _GrillStore(
        payloads={"run-a1": dict(_BASE_PAYLOAD), "run-a2": {**_BASE_PAYLOAD, "attempt": 2}},
        answers={"run-a1": {"q1": "verde"}},
    )
    monkeypatch.setattr(
        stage_grill, "run_grill", lambda *a, **k: StageResult(outcome="waiting_human", evidence_refs=["grill_deadline:x"])
    )

    result = _grill_handler(store).handle(_ctx("grill", ticket_id="acme", run_id="run-a2"))

    assert result.outcome == "waiting_human"
    assert store.recorded == []


def test_grill_handler_first_attempt_has_no_adoption_candidates(monkeypatch) -> None:
    from core.line import stage_grill
    from core.workflow.control_contracts import StageResult

    store = _GrillStore(payloads={"run-a1": dict(_BASE_PAYLOAD)}, answers={})
    seen: dict = {}

    def _fake_run_grill(project, run_id, demand_text, **kwargs):
        seen.update(kwargs)
        return StageResult(outcome="success", output_refs=["abc"])

    monkeypatch.setattr(stage_grill, "run_grill", _fake_run_grill)
    _grill_handler(store).handle(_ctx("grill", ticket_id="acme", run_id="run-a1"))

    assert seen["adopt_from_runs"] == []
