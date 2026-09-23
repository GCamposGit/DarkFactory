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


def test_required_caps_agent_stages_need_git_and_harness_any() -> None:
    project = ProjectDescriptor(id="acme", name="Acme")
    for stage in ("grill", "planning", "development", "independent_review", "integration"):
        caps = bindings.required_caps(project, stage)
        assert "git" in caps
        assert "harness:any" in caps


def test_required_caps_validation_needs_git_no_harness() -> None:
    project = ProjectDescriptor(id="acme", name="Acme")
    caps = bindings.required_caps(project, "validation")
    assert "git" in caps
    assert "harness:any" not in caps


def test_required_caps_build_deploy_local_service_needs_target_capability() -> None:
    project = ProjectDescriptor(
        id="acme", name="Acme", deploy=DeployConfig(type=DeployTargetType.LOCAL_SERVICE, params={})
    )
    caps = bindings.required_caps(project, "build_deploy")
    assert "target:local_service" in caps
    assert "harness:any" not in caps  # build_deploy itself needs no agent harness


def test_required_caps_retrospective_needs_only_git() -> None:
    project = ProjectDescriptor(id="acme", name="Acme")
    assert bindings.required_caps(project, "retrospective") == ["git"]


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
