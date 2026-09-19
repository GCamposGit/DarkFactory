"""Deterministic tests for HF-10-01: Memória por eventos e contexto fixado.

Normative implementation of:
- docs/handoffs/continuous-autonomy/HF-10-01.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- ADR-HF-001
"""

from __future__ import annotations

import pytest
from pathlib import Path
from datetime import UTC, datetime

from core.execution.agent_executor import TaskSpec
from core.learning.models import PolicyOrigin, PolicyStatus
from core.learning.service import PersistentMemoryService
from core.workflow.control_contracts import (
    Claim,
    JobKey,
    StageContext,
    StageResult,
)
from core.workflow.learning_handlers import (
    ContextLoader,
    LoadedContext,
    ObservationHandler,
)
from core.workflow.successors import LEARNING_STAGES


def _make_context(
    stage: str = "memory_observation",
    ticket_id: str = "HF-10-01",
    run_id: str = "run-mem-01",
    iteration: int = 0,
    memory_version: str = "mem-v1:initial",
    input_refs: list[str] | None = None,
) -> StageContext:
    jk = JobKey(
        run_id=run_id,
        ticket_id=ticket_id,
        plan_version="1.0",
        stage=stage,
        iteration=iteration,
    )
    claim = Claim(
        job_key=jk,
        lease_id=f"lease-{stage}-{iteration}",
        owner="worker-mem",
        fencing_token=1,
        expires_at="2026-09-19T22:00:00+00:00",
    )
    return StageContext(
        claim=claim,
        plan_ref="plan-mem-1",
        plan_digest="sha256:" + "a" * 64,
        config_version="1.0",
        environment_ref="env-local",
        identity="developer",
        route_ref="route-local",
        memory_version=memory_version,
        input_refs=input_refs or [f"ref://result/{ticket_id}/validation"],
    )


class TestContextLoader:
    def test_context_loader_fixes_deterministic_memory_version(self, tmp_path: Path) -> None:
        """ContextLoader.get(job) computes and fixes a deterministic memory_version."""
        service = PersistentMemoryService(base_dir=tmp_path / ".factory")
        loader = ContextLoader(memory_service=service)

        ctx = _make_context(stage="development", ticket_id="PRJ-101")
        loaded1 = loader.get(ctx)
        assert isinstance(loaded1, LoadedContext)
        assert loaded1.memory_version.startswith("sha256:") or loaded1.memory_version.startswith("mem-v1:")
        assert loaded1.project_id == "darkfac" or loaded1.project_id == "PRJ-101"

        # Calling again on same state yields identical memory_version
        loaded2 = loader.get(ctx)
        assert loaded1.memory_version == loaded2.memory_version

        # Promoting an active rule changes the memory_version deterministically
        service.register_candidate(
            rule_id="RULE-01",
            origin=PolicyOrigin.EXPLICIT_PREFERENCE,
            scope="global",
            rule_content="Always write UTF-8",
            supporting_runs=["run-001"],
        )
        service.evaluate_and_promote("RULE-01", eval_version="v1", passed=True)

        loaded3 = loader.get(ctx)
        assert loaded3.memory_version != loaded1.memory_version

    def test_context_loader_strict_project_isolation(self, tmp_path: Path) -> None:
        """Rules for project A never leak into project B; global rules appear in both."""
        service = PersistentMemoryService(base_dir=tmp_path / ".factory")
        loader = ContextLoader(memory_service=service)

        # Register and promote rule for project-a
        service.register_candidate(
            rule_id="RULE-A",
            origin=PolicyOrigin.EXPLICIT_PREFERENCE,
            scope="backend",
            rule_content="Rule exclusive to Project A",
            project_id="project-a",
            supporting_runs=["run-001"],
        )
        service.evaluate_and_promote("RULE-A", eval_version="v1", passed=True)

        # Register and promote rule for project-b
        service.register_candidate(
            rule_id="RULE-B",
            origin=PolicyOrigin.EXPLICIT_PREFERENCE,
            scope="backend",
            rule_content="Rule exclusive to Project B",
            project_id="project-b",
            supporting_runs=["run-001"],
        )
        service.evaluate_and_promote("RULE-B", eval_version="v1", passed=True)

        # Register and promote global rule
        service.register_candidate(
            rule_id="RULE-GLOBAL",
            origin=PolicyOrigin.EXPLICIT_PREFERENCE,
            scope="global",
            rule_content="Global standard rule",
            project_id="global",
            supporting_runs=["run-001"],
        )
        service.evaluate_and_promote("RULE-GLOBAL", eval_version="v1", passed=True)

        # Query project-a
        loaded_a = loader.get(job=_make_context(ticket_id="TICK-A"), project_id="project-a")
        rule_texts_a = " ".join(loaded_a.active_rules)
        assert "RULE-A" in rule_texts_a
        assert "RULE-GLOBAL" in rule_texts_a
        assert "RULE-B" not in rule_texts_a

        # Query project-b
        loaded_b = loader.get(job=_make_context(ticket_id="TICK-B"), project_id="project-b")
        rule_texts_b = " ".join(loaded_b.active_rules)
        assert "RULE-B" in rule_texts_b
        assert "RULE-GLOBAL" in rule_texts_b
        assert "RULE-A" not in rule_texts_b

    def test_context_loader_excludes_non_active_rules(self, tmp_path: Path) -> None:
        """Proposed and rejected candidates are strictly excluded from context."""
        service = PersistentMemoryService(base_dir=tmp_path / ".factory")
        loader = ContextLoader(memory_service=service)

        # Candidate in PROPOSED state
        service.register_candidate(
            rule_id="RULE-PROPOSED",
            origin=PolicyOrigin.RCA,
            scope="global",
            rule_content="Unapproved candidate rule",
        )

        # Candidate evaluated and REJECTED
        service.register_candidate(
            rule_id="RULE-REJECTED",
            origin=PolicyOrigin.RCA,
            scope="global",
            rule_content="Rejected flawed rule",
        )
        service.evaluate_and_promote("RULE-REJECTED", eval_version="v1", passed=False, error_message="Failed oracles")

        loaded = loader.get(job=_make_context(ticket_id="TICK-1"), project_id="project-a")
        rule_texts = " ".join(loaded.active_rules)
        assert "RULE-PROPOSED" not in rule_texts
        assert "RULE-REJECTED" not in rule_texts
        assert loaded.task_context.active_rules == []


class TestObservationHandler:
    def test_observation_handler_successful_execution(self, tmp_path: Path) -> None:
        """ObservationHandler executes memory_observation stage and emits valid StageResult."""
        service = PersistentMemoryService(base_dir=tmp_path / ".factory")
        handler = ObservationHandler(memory_service=service)

        ctx = _make_context(stage="memory_observation", ticket_id="HF-10-01")
        result = handler.handle(ctx)

        assert isinstance(result, StageResult)
        assert result.outcome == "success"
        assert len(result.output_refs) > 0
        assert any("memory" in ref or "hypothesis" in ref for ref in result.output_refs)

    def test_observation_handler_anti_recursion(self, tmp_path: Path) -> None:
        """Observing an event from a LEARNING_STAGE never recurses or re-enqueues memory_observation."""
        service = PersistentMemoryService(base_dir=tmp_path / ".factory")
        handler = ObservationHandler(memory_service=service)

        for learning_stage in LEARNING_STAGES:
            ctx = _make_context(
                stage="memory_observation",
                ticket_id="HF-10-01",
                input_refs=[f"ref://result/HF-10-01/{learning_stage}"],
            )
            result = handler.handle(ctx)
            assert result.outcome == "success"
            # Output refs indicate no-op / skipped recursion
            assert any("recursion_skipped" in ref or "noop" in ref or "learning_stage" in ref for ref in result.output_refs)

    def test_observation_handler_idempotency_by_event_and_version(self, tmp_path: Path) -> None:
        """Idempotency: duplicate processing of same (event_id, consumer_version) succeeds without error."""
        service = PersistentMemoryService(base_dir=tmp_path / ".factory")
        handler = ObservationHandler(memory_service=service, consumer_version="v1")

        ctx = _make_context(
            stage="memory_observation",
            ticket_id="HF-10-01",
            input_refs=["event://events/evt-unique-123"],
        )

        res1 = handler.handle(ctx)
        assert res1.outcome == "success"

        res2 = handler.handle(ctx)
        assert res2.outcome == "success"
        assert res1.output_refs == res2.output_refs

    def test_learning_pack_failure_does_not_block_production(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """If Owner Learning Pack generation fails, stage completes with success (non-blocking)."""
        service = PersistentMemoryService(base_dir=tmp_path / ".factory")
        handler = ObservationHandler(memory_service=service)

        # Force pack generation to fail
        def _failing_pack(*args, **kwargs):
            raise RuntimeError("Disk full or synthesis error")

        monkeypatch.setattr(service, "generate_owner_learning_pack", _failing_pack)

        ctx = _make_context(stage="memory_observation", ticket_id="HF-10-01")
        result = handler.handle(ctx)

        assert result.outcome == "success"
        assert len(result.output_refs) > 0

    def test_cold_restart_preserves_active_rules(self, tmp_path: Path) -> None:
        """Rules promoted in PersistentMemoryService survive a cold reload."""
        base_dir = tmp_path / ".factory"
        service = PersistentMemoryService(base_dir=base_dir)

        service.register_candidate(
            rule_id="RULE-DURABLE",
            origin=PolicyOrigin.EXPLICIT_PREFERENCE,
            scope="global",
            rule_content="Durable rule across reloads",
            supporting_runs=["run-001"],
        )
        service.evaluate_and_promote("RULE-DURABLE", eval_version="v1", passed=True)

        # Cold reload
        reloaded_service = PersistentMemoryService(base_dir=base_dir)
        active = reloaded_service.list_active_rules()
        active_ids = [c.rule_id for c in active]
        assert "RULE-DURABLE" in active_ids
