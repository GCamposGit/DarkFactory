"""Focal test suite for DF-19: Context Selection & Learning Promotion Policy."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import pytest
from pydantic import ValidationError

from core.execution.agent_executor import TaskSpec
from core.learning.models import PolicyOrigin, PolicyStatus
from core.learning.promotion import (
    LearningCandidate,
    LearningPromotionEngine,
    PromotionDeniedError,
)
from core.orchestrator.context import (
    ContextSelector,
    FileReferenceSpec,
    TaskContext,
)


def test_learning_candidate_strict_schema() -> None:
    """Test LearningCandidate complies with Pydantic v2 strict contract."""
    candidate = LearningCandidate(
        rule_id="RULE-001",
        origin=PolicyOrigin.RCA,
        scope="core/orchestrator",
        supporting_runs=["run-101", "run-102"],
        eval_version="eval-sha-abc123",
        before_after={"before": "retry infinitely", "after": "fail-closed on 3rd attempt"},
        status=PolicyStatus.PROPOSED,
        confidence=0.95,
        rule_content="Always enforce max_retries <= 3 in orchestrator claims.",
    )
    assert candidate.rule_id == "RULE-001"
    assert candidate.origin == PolicyOrigin.RCA
    assert candidate.status == PolicyStatus.PROPOSED
    assert len(candidate.supporting_runs) == 2
    assert candidate.rollback_ref is None

    # Extra fields are forbidden
    with pytest.raises(ValidationError):
        LearningCandidate(
            rule_id="RULE-002",
            origin=PolicyOrigin.OBSERVATION,
            scope="core",
            eval_version="v1",
            unknown_arbitrary_field="not_allowed",  # type: ignore
        )

    # Missing required fields fail
    with pytest.raises(ValidationError):
        LearningCandidate(  # type: ignore
            rule_id="RULE-003",
        )


def test_promotion_rejected_without_evaluation(tmp_path: Path) -> None:
    """A proposed candidate cannot be promoted without passing evaluation first."""
    storage = tmp_path / "candidates.json"
    engine = LearningPromotionEngine(storage_path=storage)

    candidate = engine.register_candidate(
        rule_id="RULE-UNTESTED",
        origin=PolicyOrigin.INFERRED_PREFERENCE,
        scope="core/harness",
        supporting_runs=["run-smoke-01"],
        eval_version="v1.0.0",
        before_after={"before": "slow", "after": "fast"},
        rule_content="Use quick harness for smoke tests.",
    )
    assert candidate.status == PolicyStatus.PROPOSED

    with pytest.raises(PromotionDeniedError, match="must be in EVALUATED status"):
        engine.promote_candidate("RULE-UNTESTED", verified_eval_version="v1.0.0")


def test_promotion_rejected_without_supporting_runs(tmp_path: Path) -> None:
    """A candidate with zero supporting runs cannot be promoted even if marked evaluated."""
    storage = tmp_path / "candidates.json"
    engine = LearningPromotionEngine(storage_path=storage)

    engine.register_candidate(
        rule_id="RULE-NO-RUNS",
        origin=PolicyOrigin.OBSERVATION,
        scope="core/execution",
        supporting_runs=[],  # Empty supporting runs
        eval_version="v1.0.0",
        before_after={"before": "old", "after": "new"},
        rule_content="Hypothetical rule without run evidence.",
    )
    engine.record_eval_pass("RULE-NO-RUNS", eval_version="v1.0.0")

    with pytest.raises(PromotionDeniedError, match="at least one supporting run"):
        engine.promote_candidate("RULE-NO-RUNS", verified_eval_version="v1.0.0")


def test_promotion_rejected_on_eval_version_mismatch(tmp_path: Path) -> None:
    """A candidate evaluated on an older/different eval suite cannot be promoted on a mismatched version."""
    storage = tmp_path / "candidates.json"
    engine = LearningPromotionEngine(storage_path=storage)

    engine.register_candidate(
        rule_id="RULE-OLD-EVAL",
        origin=PolicyOrigin.RCA,
        scope="core/learning",
        supporting_runs=["run-prev-01"],
        eval_version="eval-v1",
        before_after={"before": "unbounded", "after": "bounded"},
        rule_content="Cap context length.",
    )
    engine.record_eval_pass("RULE-OLD-EVAL", eval_version="eval-v1")

    with pytest.raises(PromotionDeniedError, match="eval_version mismatch"):
        engine.promote_candidate("RULE-OLD-EVAL", verified_eval_version="eval-v2-different")


def test_successful_promotion_and_activation(tmp_path: Path) -> None:
    """A candidate with supporting runs and matching evaluated version promotes cleanly to ACTIVE."""
    storage = tmp_path / "candidates.json"
    engine = LearningPromotionEngine(storage_path=storage)

    engine.register_candidate(
        rule_id="RULE-PROMOTABLE",
        origin=PolicyOrigin.RCA,
        scope="core/orchestrator",
        supporting_runs=["run-live-01", "run-live-02"],
        eval_version="eval-2026-09-07",
        before_after={"before": "stale locks", "after": "monotonic fencing"},
        rule_content="Use monotonic fencing token on store renewal.",
    )
    engine.record_eval_pass("RULE-PROMOTABLE", eval_version="eval-2026-09-07")

    promoted = engine.promote_candidate("RULE-PROMOTABLE", verified_eval_version="eval-2026-09-07")
    assert promoted.status == PolicyStatus.ACTIVE
    assert promoted.promoted_at is not None

    active_rules = engine.get_active_candidates(scope="core/orchestrator")
    assert len(active_rules) == 1
    assert active_rules[0].rule_id == "RULE-PROMOTABLE"


def test_rollback_candidate_reverts_to_retired(tmp_path: Path) -> None:
    """An active candidate can be atomically rolled back, recording rollback_ref and retiring."""
    storage = tmp_path / "candidates.json"
    engine = LearningPromotionEngine(storage_path=storage)

    engine.register_candidate(
        rule_id="RULE-REGRESSIVE",
        origin=PolicyOrigin.ANALOGY,
        scope="core/execution",
        supporting_runs=["run-99"],
        eval_version="eval-v1",
        before_after={"before": "A", "after": "B"},
        rule_content="Aggressive concurrency limit.",
    )
    engine.record_eval_pass("RULE-REGRESSIVE", eval_version="eval-v1")
    engine.promote_candidate("RULE-REGRESSIVE", verified_eval_version="eval-v1")

    assert len(engine.get_active_candidates(scope="core/execution")) == 1

    # Now rollback
    rolled_back = engine.rollback_candidate(
        "RULE-REGRESSIVE",
        reason="Caused starvation on 4-core runners",
        rollback_ref="git-revert-sha123",
    )
    assert rolled_back.status == PolicyStatus.RETIRED
    assert rolled_back.rollback_ref == "git-revert-sha123"
    assert rolled_back.retired_at is not None

    # Must no longer appear in active candidates
    assert len(engine.get_active_candidates(scope="core/execution")) == 0


def test_candidates_persistence_roundtrip(tmp_path: Path) -> None:
    """Ensure atomic persistence and reload preserves all attributes and datetimes."""
    storage = tmp_path / "candidates.json"
    engine1 = LearningPromotionEngine(storage_path=storage)

    engine1.register_candidate(
        rule_id="RULE-PERSIST",
        origin=PolicyOrigin.EXPLICIT_PREFERENCE,
        scope="core/harness",
        supporting_runs=["run-1"],
        eval_version="v1",
        before_after={"before": "verbose", "after": "concise"},
        rule_content="Concise summary in outputs.",
    )
    engine1.record_eval_pass("RULE-PERSIST", eval_version="v1")
    engine1.promote_candidate("RULE-PERSIST", verified_eval_version="v1")

    # Load in fresh engine
    engine2 = LearningPromotionEngine(storage_path=storage)
    active = engine2.get_active_candidates()
    assert len(active) == 1
    assert active[0].rule_id == "RULE-PERSIST"
    assert active[0].status == PolicyStatus.ACTIVE
    assert isinstance(active[0].promoted_at, datetime)


def test_context_selector_filters_active_and_scoped_rules(tmp_path: Path) -> None:
    """Context selector only injects rules that are ACTIVE and relevant to the task scope."""
    storage = tmp_path / "candidates.json"
    engine = LearningPromotionEngine(storage_path=storage)

    # 1. Matching scope + ACTIVE -> should be included
    engine.register_candidate(
        rule_id="RULE-ACTIVE-ORCH",
        origin=PolicyOrigin.RCA,
        scope="core/orchestrator",
        supporting_runs=["run-1"],
        eval_version="v1",
        before_after={},
        rule_content="Rule 1: Always check lease before writing.",
    )
    engine.record_eval_pass("RULE-ACTIVE-ORCH", eval_version="v1")
    engine.promote_candidate("RULE-ACTIVE-ORCH", verified_eval_version="v1")

    # 2. Matching scope + PROPOSED -> MUST be excluded
    engine.register_candidate(
        rule_id="RULE-PROPOSED-ORCH",
        origin=PolicyOrigin.INFERRED_PREFERENCE,
        scope="core/orchestrator",
        supporting_runs=["run-1"],
        eval_version="v1",
        before_after={},
        rule_content="Rule 2: Unverified intuition.",
    )

    # 3. Matching scope + RETIRED -> MUST be excluded
    engine.register_candidate(
        rule_id="RULE-RETIRED-ORCH",
        origin=PolicyOrigin.ANALOGY,
        scope="core/orchestrator",
        supporting_runs=["run-1"],
        eval_version="v1",
        before_after={},
        rule_content="Rule 3: Deprecated rule.",
    )
    engine.record_eval_pass("RULE-RETIRED-ORCH", eval_version="v1")
    engine.promote_candidate("RULE-RETIRED-ORCH", verified_eval_version="v1")
    engine.rollback_candidate("RULE-RETIRED-ORCH", reason="Obsolescence")

    # 4. Non-matching scope + ACTIVE -> MUST be excluded
    engine.register_candidate(
        rule_id="RULE-ACTIVE-AUDIO",
        origin=PolicyOrigin.EXPLICIT_PREFERENCE,
        scope="core/audio",
        supporting_runs=["run-2"],
        eval_version="v1",
        before_after={},
        rule_content="Rule 4: CUDA FP16 audio requirement.",
    )
    engine.record_eval_pass("RULE-ACTIVE-AUDIO", eval_version="v1")
    engine.promote_candidate("RULE-ACTIVE-AUDIO", verified_eval_version="v1")

    task_spec = TaskSpec(
        task_id="TASK-DF19-DEMO",
        objective="Refactor orchestrator runtime loop",
        allowed_paths=["core/orchestrator/runtime.py", "core/orchestrator/store.py"],
        non_goals=["Audio changes"],
        acceptance_criteria=["Clean lease claims"],
        risk_class="B",
    )

    selector = ContextSelector(promotion_engine=engine)
    context: TaskContext = selector.assemble_context(task_spec)

    # Verify only RULE-ACTIVE-ORCH is included
    assert len(context.active_rules) == 1
    assert "Rule 1: Always check lease before writing." in context.active_rules[0]
    assert not any("Rule 2" in r for r in context.active_rules)
    assert not any("Rule 3" in r for r in context.active_rules)
    assert not any("Rule 4" in r for r in context.active_rules)


def test_context_selector_assembles_compact_summary_and_file_refs() -> None:
    """Context selector produces compact operational summary, structured file references and progress."""
    task_spec = TaskSpec(
        task_id="TASK-DF19-CTX",
        objective="Implement context policy",
        allowed_paths=["core/orchestrator/context.py", "tests/test_context_policy.py"],
        non_goals=["UI changes", "DB migration"],
        acceptance_criteria=["Deterministic context assembly", "Pydantic models"],
        budget_ceiling=2.5,
        risk_class="A",
    )

    checkpoint = {
        "last_step_index": 2,
        "outputs": {"0": "parsed", "1": "validated"},
    }

    selector = ContextSelector()
    context = selector.assemble_context(task_spec, checkpoint=checkpoint, max_summary_tokens=300)

    assert context.task_id == "TASK-DF19-CTX"
    assert "Implement context policy" in context.operational_summary
    assert "risk_class: A" in context.operational_summary
    assert len(context.file_references) == 2
    assert context.file_references[0].path == "core/orchestrator/context.py"
    assert len(context.durable_progress) == 3
    assert "step 0" in context.durable_progress[0]
    assert "step 1" in context.durable_progress[1]
    assert "last_step_index: 2" in context.durable_progress[2]
    assert context.token_estimate > 0
    # Summary token estimate stays well within ceiling
    assert context.token_estimate < 500


def test_context_selector_enforces_summary_bound_and_checkpoint_order() -> None:
    """Large task text is bounded and mixed checkpoint keys remain deterministic."""
    task_spec = TaskSpec(
        task_id="TASK-DF19-BOUND",
        objective="Implement " + ("bounded context policy " * 200),
        allowed_paths=["core/orchestrator/context.py"],
    )

    context = ContextSelector().assemble_context(
        task_spec,
        checkpoint={"outputs": {"10": "ten", "2": "two", "note": "text"}},
        max_summary_tokens=40,
    )

    assert len(context.operational_summary) <= 40 * 4
    assert context.durable_progress[:3] == [
        "step 2: two",
        "step 10: ten",
        "step note: text",
    ]


def test_failed_active_candidate_is_retired_and_non_active_rollback_is_denied(tmp_path: Path) -> None:
    """A failed re-evaluation cannot leave an active rule injectable."""
    engine = LearningPromotionEngine(storage_path=tmp_path / "candidates.json")
    engine.register_candidate(
        rule_id="RULE-FAILURE",
        origin=PolicyOrigin.RCA,
        scope="core/orchestrator",
        supporting_runs=["run-1"],
        eval_version="v1",
    )
    engine.record_eval_pass("RULE-FAILURE", "v1")
    engine.promote_candidate("RULE-FAILURE", "v1")

    failed = engine.record_eval_failure("RULE-FAILURE", "v2", "regression")
    assert failed.status is PolicyStatus.RETIRED
    assert engine.get_active_candidates() == []

    with pytest.raises(PromotionDeniedError, match="Only ACTIVE"):
        engine.rollback_candidate("RULE-FAILURE", reason="already retired")


def test_context_selector_with_tracker_preferences(tmp_path: Path) -> None:
    """Ensure ContinuousLearningTracker preferences are only included when ACTIVE."""
    from core.learning.tracker import ContinuousLearningTracker
    from core.learning.models import PreferenceCategory

    ledger_file = tmp_path / "learning_ledger.json"
    tracker = ContinuousLearningTracker(ledger_file=ledger_file)

    # Register an active preference
    tracker.register_preference(
        category=PreferenceCategory.ARCHITECTURE,
        rule="Prefer headless domain logic over Presentation CLI prints.",
        context_or_example="AGENTS.md universal rule",
        confidence=1.0,
        status=PolicyStatus.ACTIVE,
    )

    # Register a proposed (unpromoted) preference
    tracker.register_preference(
        category=PreferenceCategory.ARCHITECTURE,
        rule="Unverified speculative style rule.",
        context_or_example="Random comment",
        confidence=0.5,
        status=PolicyStatus.PROPOSED,
    )

    task_spec = TaskSpec(
        task_id="TASK-PREF-TEST",
        objective="Design architecture for new service",
        allowed_paths=["core/architecture/service.py"],
        risk_class="B",
    )

    selector = ContextSelector()
    context = selector.assemble_context(task_spec, tracker=tracker)

    # Active preference is included
    assert any("Prefer headless domain logic" in r for r in context.active_rules)
    # Proposed preference is NOT included
    assert not any("Unverified speculative style rule" in r for r in context.active_rules)
