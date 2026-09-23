"""Tests for the deploy/smoke/rollback release stage (HF-27-07).

Two scopes, per `docs/handoffs/production-line/HF-27-07.md`'s acceptance
list:

1. `ReleaseStageHandler` against a fake `DeploymentAdapter` (no network,
   no real Dokploy/FTP/local process) — success records `last_good_sha`,
   a failed smoke rolls back and returns `retry` with the log, and two
   consecutive smoke failures for the same run give up with
   `failed(prod_smoke_failed)`.
2. `ControlStore.claim()`'s existing `required_capabilities` filter, proving
   a `local_service` job is never claimed by a worker lacking
   `target:local_service` — the mechanism this stage relies on instead of
   checking host capabilities itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import pytest

from core.line.stage_release import ReleaseStageHandler, ReleaseStateStore
from core.orchestrator.deployment_adapter import (
    DeploymentOperation,
    DeploymentStatus,
    RollbackResult,
    TargetConfig,
)
from core.projects.models import DeployConfig, DeployTargetType, ProjectDescriptor, SmokeCheck
from core.workflow.control_contracts import Claim, IntakeCommand, JobKey, RuntimeOwner, StageContext
from core.workflow.control_store import SQLiteControlStore


# --------------------------------------------------------------------------
# Fake deployment adapter (no network)
# --------------------------------------------------------------------------


class FakeAdapter:
    """Minimal in-memory `DeploymentAdapter` double."""

    def __init__(self) -> None:
        self.installed: dict[str, str] = {}
        self.rollback_calls: list[tuple[str, Optional[str], str]] = []
        self.start_calls: list[str] = []

    def start(self, artifact_ref, target_config: TargetConfig, claim=None) -> DeploymentOperation:
        self.start_calls.append(artifact_ref.byte_digest)
        self.installed[target_config.project_id] = artifact_ref.byte_digest
        return DeploymentOperation(
            operation_id=f"op_{artifact_ref.byte_digest[:8]}",
            external_operation_id=f"ext_{artifact_ref.byte_digest[:8]}",
            project_id=target_config.project_id,
            target_type=target_config.target_type,
            artifact_ref=artifact_ref,
            status=DeploymentStatus.SUCCEEDED,
        )

    def reconcile(self, operation_id: str) -> DeploymentStatus:
        return DeploymentStatus.SUCCEEDED

    def installed_digest(self, target_config: TargetConfig) -> Optional[str]:
        return self.installed.get(target_config.project_id)

    def rollback(self, target_config: TargetConfig, failed_digest: str, reason: str, claim=None) -> RollbackResult:
        restored = target_config.last_known_good_digest
        self.installed[target_config.project_id] = restored
        self.rollback_calls.append((failed_digest, restored, reason))
        return RollbackResult(
            rollback_id=f"rb_{failed_digest[:8]}",
            project_id=target_config.project_id,
            failed_digest=failed_digest,
            restored_digest=restored,
            status=DeploymentStatus.ROLLED_BACK,
            reason=reason,
        )


def _opener_factory(responses: list[tuple[int, bytes]]) -> Callable[[str, float], tuple[int, bytes]]:
    """Returns an opener yielding `responses` in order, repeating the last one."""
    state = {"i": 0}

    def opener(url: str, timeout: float) -> tuple[int, bytes]:
        idx = min(state["i"], len(responses) - 1)
        state["i"] += 1
        return responses[idx]

    return opener


def _project(*, requires_commercial_acceptance: bool = False) -> ProjectDescriptor:
    return ProjectDescriptor(
        id="acme",
        name="Acme Project",
        deploy=DeployConfig(type=DeployTargetType.DOKPLOY, params={}),
        smoke=[SmokeCheck(url="https://acme.example/healthz", expect_status=200)],
        requires_commercial_acceptance=requires_commercial_acceptance,
    )


def _context(run_id: str, pr_url: str, merge_sha: str) -> StageContext:
    jk = JobKey(run_id=run_id, ticket_id="T1", plan_version="1.0", stage="release", iteration=0)
    claim = Claim(
        job_key=jk,
        lease_id=f"lease_{run_id}",
        owner="worker-1",
        fencing_token=1,
        expires_at=(datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    )
    return StageContext(
        claim=claim,
        plan_ref="plan://line",
        plan_digest="a" * 64,
        config_version="v1",
        environment_ref="acme",
        identity="worker-1",
        route_ref="route://line/release",
        memory_version="mem_v1",
        input_refs=[pr_url, merge_sha],
    )


def _handler(project: ProjectDescriptor, adapter: FakeAdapter, tmp_path: Path, opener) -> ReleaseStageHandler:
    return ReleaseStageHandler(
        project,
        dokploy_adapter=adapter,
        state_store=ReleaseStateStore(state_dir=tmp_path / "release_state"),
        smoke_opener=opener,
        smoke_retries=1,
        smoke_delay_s=0.0,
        reconcile_polls=3,
        reconcile_delay_s=0.0,
        sleep=lambda _s: None,
    )


# --------------------------------------------------------------------------
# 1. success records last_good_sha
# --------------------------------------------------------------------------


def test_success_deploys_and_records_last_good_sha(tmp_path: Path) -> None:
    project = _project()
    adapter = FakeAdapter()
    opener = _opener_factory([(200, b"ok")])
    handler = _handler(project, adapter, tmp_path, opener)

    context = _context("run-1", "https://github.com/acme/acme/pull/1", "sha_v1_" + "a" * 33)
    result = handler.handle(context)

    assert result.outcome == "success"
    assert result.output_refs[0] == f"op_{context.input_refs[-1][:8]}"
    assert adapter.start_calls == [context.input_refs[-1]]

    state = handler.state_store.load(project.id)
    assert state.last_good_sha == context.input_refs[-1]
    assert state.last_success_operation_id == result.output_refs[0]


def test_success_is_idempotent_for_same_sha(tmp_path: Path) -> None:
    project = _project()
    adapter = FakeAdapter()
    opener = _opener_factory([(200, b"ok")])
    handler = _handler(project, adapter, tmp_path, opener)
    sha = "sha_v1_" + "a" * 33

    context = _context("run-1", "https://github.com/acme/acme/pull/1", sha)
    first = handler.handle(context)
    assert first.outcome == "success"
    assert adapter.start_calls == [sha]

    # A second call for the same already-deployed SHA must not redeploy.
    second = handler.handle(_context("run-1", "https://github.com/acme/acme/pull/1", sha))
    assert second.outcome == "success"
    assert adapter.start_calls == [sha]  # unchanged: no second start()


# --------------------------------------------------------------------------
# 2. failed smoke rolls back to the previous SHA and returns retry with log
# --------------------------------------------------------------------------


def test_smoke_failure_rolls_back_and_retries_with_log(tmp_path: Path) -> None:
    project = _project()
    adapter = FakeAdapter()
    good_opener = _opener_factory([(200, b"ok")])
    handler = _handler(project, adapter, tmp_path, good_opener)

    sha_good = "sha_good" + "a" * 32
    handler.handle(_context("run-1", "https://github.com/acme/acme/pull/1", sha_good))
    assert handler.state_store.load(project.id).last_good_sha == sha_good

    # Next deploy: smoke fails (HTTP 500).
    handler.smoke_opener = _opener_factory([(500, b"boom")])
    sha_bad = "sha_bad_" + "b" * 32
    result = handler.handle(_context("run-1", "https://github.com/acme/acme/pull/1", sha_bad))

    assert result.outcome == "retry"
    assert "prod_smoke_failed_rolled_back_to_" in result.cause_code
    assert sha_good[:12] in result.cause_code
    assert "500" in result.cause_code

    # Rollback happened against the fake adapter, back to the previous good SHA.
    assert adapter.rollback_calls == [(sha_bad, sha_good, "post-deploy smoke check failed")]
    assert adapter.installed["acme"] == sha_good

    # last_good_sha is untouched by the failed deploy.
    state = handler.state_store.load(project.id)
    assert state.last_good_sha == sha_good
    assert state.runs["run-1"].consecutive_smoke_failures == 1


def test_two_consecutive_smoke_failures_give_up(tmp_path: Path) -> None:
    project = _project()
    adapter = FakeAdapter()
    good_opener = _opener_factory([(200, b"ok")])
    handler = _handler(project, adapter, tmp_path, good_opener)

    sha_good = "sha_good" + "a" * 32
    handler.handle(_context("run-1", "https://github.com/acme/acme/pull/1", sha_good))

    bad_opener = _opener_factory([(500, b"boom")])
    handler.smoke_opener = bad_opener
    sha_bad = "sha_bad_" + "b" * 32

    first = handler.handle(_context("run-1", "https://github.com/acme/acme/pull/1", sha_bad))
    assert first.outcome == "retry"

    second = handler.handle(_context("run-1", "https://github.com/acme/acme/pull/1", sha_bad))
    assert second.outcome == "failed"
    assert second.cause_code.startswith("prod_smoke_failed:")


# --------------------------------------------------------------------------
# 3. commercial acceptance gate
# --------------------------------------------------------------------------


def test_requires_commercial_acceptance_pauses_before_deploy(tmp_path: Path) -> None:
    project = _project(requires_commercial_acceptance=True)
    adapter = FakeAdapter()
    opener = _opener_factory([(200, b"ok")])
    handler = _handler(project, adapter, tmp_path, opener)

    sha = "sha_v1_" + "a" * 33
    result = handler.handle(_context("run-1", "https://github.com/acme/acme/pull/1", sha))

    assert result.outcome == "waiting_human"
    assert "commercial_acceptance" in result.cause_code
    assert "pull/1" in result.cause_code
    assert sha in result.cause_code
    assert adapter.start_calls == []  # never deployed


def test_missing_merge_sha_is_terminal_failure(tmp_path: Path) -> None:
    project = _project()
    adapter = FakeAdapter()
    handler = _handler(project, adapter, tmp_path, _opener_factory([(200, b"ok")]))

    context = _context("run-1", "", "")
    result = handler.handle(context)
    assert result.outcome == "failed"
    assert result.cause_code == "release_missing_merge_sha"


# --------------------------------------------------------------------------
# 4. `deploy.type == none` runs local smoke commands, not HTTP checks
# --------------------------------------------------------------------------


def test_deploy_none_uses_local_smoke_and_skips_remote_deploy(tmp_path: Path) -> None:
    project = ProjectDescriptor(id="acme-none", name="Acme None", deploy=None)
    adapter = FakeAdapter()
    handler = _handler(project, adapter, tmp_path, _opener_factory([(200, b"ok")]))

    sha = "sha_v1_" + "a" * 33
    result = handler.handle(_context("run-1", "https://github.com/acme/acme/pull/1", sha))

    assert result.outcome == "success"
    assert adapter.start_calls == []  # no remote deploy attempted


# --------------------------------------------------------------------------
# 5. `local_service` claim requires the `target:local_service` capability
# --------------------------------------------------------------------------


def test_local_service_claim_requires_capability(tmp_path: Path) -> None:
    """A `local_service` release job is never claimed without the capability.

    This is the mechanism `ReleaseStageHandler` relies on instead of
    checking host capabilities itself (see the module docstring): job
    creation (out of this ticket's scope, HF-27-08) is expected to set
    `required_capabilities=["target:local_service"]` on a release job whose
    project deploys to `local_service`; `ControlStore.claim()` already
    enforces the filter generically.
    """
    store = SQLiteControlStore(
        db_path=tmp_path / "control.db",
        runtime_owner=RuntimeOwner.HF05_SQLITE.value,
        lease_duration_sec=45,
    )
    now = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)

    # A run must exist first (jobs.run_id is a foreign key into runs).
    cmd = IntakeCommand(
        project_id="acme",
        channel="cli",
        external_id="ext-release-001",
        mode="autonomous",
        policy_ref="policy-v1",
        payload={
            "title": "Release stage capability test",
            "problem": "verify local_service capability gating",
            "journey": "n/a",
            "non_goals": [],
            "criteria": ["n/a"],
        },
    )
    receipt = store.accept(cmd, now)
    run_id = receipt.run_id

    with store._connect() as conn:
        # accept() seeds a pending 'grill' job for the run; mark it done so
        # it does not shadow the 'release' job below in claim()'s ORDER BY.
        conn.execute(
            "UPDATE jobs SET status = 'succeeded' WHERE run_id = ? AND stage = 'grill'",
            (run_id,),
        )
        conn.execute(
            """
            INSERT INTO jobs (
                run_id, ticket_id, plan_version, stage, iteration, status,
                role, required_capabilities, fencing_token, timeout_seconds,
                retry_count, max_retries, actual_cost, output_refs, evidence_refs,
                created_at, updated_at
            ) VALUES (
                ?, 'T1', '1.0', 'release', 0, 'pending',
                'releaser', '["target:local_service"]', 0, 1800,
                0, 3, 0.0, '[]', '[]', ?, ?
            )
            """,
            (run_id, now.isoformat(), now.isoformat()),
        )
        conn.commit()

    # A cloud worker without the local-service capability never claims it.
    assert store.claim(worker="cloud-worker", capabilities=["economy"], now=now) is None

    # The desktop worker that does have it claims it fine.
    claimed = store.claim(worker="desktop-worker", capabilities=["economy", "target:local_service"], now=now)
    assert claimed is not None
    assert claimed.job_key.stage == "release"
