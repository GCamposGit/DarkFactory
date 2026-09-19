"""Comprehensive test suite for development and quality stage handlers (HF-09-02).

Validates:
- Conformance to StageHandler protocol.
- Worktree isolation and lease enforcement.
- Iteration management: defects increment iteration; 2 attempts without progress terminates with cause.
- Replan on architectural gap.
- Validation oracle enforcement: defect reproves even with self-pass assertion.
- Independent review invariant: strict prohibition of self-approval (reviewer != developer).
- Cross-model family isolation.
- EvidenceReceipt emission with VerificationContext authority.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.workflow.contracts import (
    SanitizedIdentity,
    WorkflowHandoff,
)
from core.workflow.control_contracts import (
    Claim,
    JobKey,
    StageContext,
    StageResult,
)
from core.workflow.cycle import ImplementationCandidate
from core.workflow.development_handlers import DevelopmentStageHandler
from core.workflow.handlers import StageHandler
from core.workflow.quality_handlers import (
    ReviewStageHandler,
    ValidationStageHandler,
)


def _make_context(
    ticket_id: str = "HF-09-TEST",
    stage: str = "development",
    identity: str = "dev-worker-1",
    iteration: int = 1,
    expires_delta_seconds: int = 60,
    candidate_digest: str | None = "cand_digest_12345678",
) -> StageContext:
    jk = JobKey(
        run_id=f"run_{ticket_id}",
        ticket_id=ticket_id,
        plan_version="1.0",
        stage=stage,
        iteration=iteration,
    )
    expires_at = (datetime.now(UTC) + timedelta(seconds=expires_delta_seconds)).isoformat()
    claim = Claim(
        job_key=jk,
        lease_id=f"lease_{ticket_id}_{iteration}",
        owner=identity,
        fencing_token=1,
        expires_at=expires_at,
    )
    return StageContext(
        claim=claim,
        plan_ref="plan://continuous-autonomy",
        plan_digest="a" * 64,
        candidate_digest=candidate_digest,
        config_version="v1.0",
        environment_ref="env-local-test",
        identity=identity,
        route_ref="route://economy/local",
        memory_version="mem_v1",
        input_refs=[],
    )


def test_development_handler_conforms_to_stage_handler(tmp_path: Path):
    handler = DevelopmentStageHandler(base_worktree_dir=tmp_path)
    assert isinstance(handler, StageHandler)
    assert handler.STAGE == "development"
    assert handler.descriptor.role == "developer"


def test_development_handler_creates_candidate_with_sha_and_diff(tmp_path: Path):
    handler = DevelopmentStageHandler(base_worktree_dir=tmp_path)
    ctx = _make_context(ticket_id="HF-09-DEV1", stage="development")

    result = handler.handle(ctx)
    assert result.outcome == "success"
    assert len(result.output_refs) >= 2
    assert any("ref://candidate/" in ref for ref in result.output_refs)
    assert any("ref://candidate-digest/" in ref for ref in result.output_refs)
    assert any("ref://worktree/" in ref for ref in result.output_refs)

    # Check candidate stored
    cand = next(iter(handler._candidates.values()))
    assert cand.ticket_id == "HF-09-DEV1"
    assert len(cand.candidate_sha) >= 7
    assert cand.diff != ""


def test_development_handler_worktree_isolation_per_job(tmp_path: Path):
    handler = DevelopmentStageHandler(base_worktree_dir=tmp_path)
    ctx1 = _make_context(ticket_id="HF-09-A", stage="development", iteration=1)
    ctx2 = _make_context(ticket_id="HF-09-B", stage="development", iteration=1)

    res1 = handler.handle(ctx1)
    res2 = handler.handle(ctx2)

    wt1 = next(r for r in res1.output_refs if "ref://worktree/" in r)
    wt2 = next(r for r in res2.output_refs if "ref://worktree/" in r)
    assert wt1 != wt2
    assert "HF-09-A" in wt1
    assert "HF-09-B" in wt2


def test_development_handler_replan_on_architecture_gap(tmp_path: Path):
    def gap_executor(ctx: StageContext, wt: Path) -> dict[str, Any]:
        return {
            "status": "replan",
            "cause_code": "needs_architecture_binding",
            "output_refs": ["ref://planning/needs_architecture_binding/HF-GAP"],
        }

    handler = DevelopmentStageHandler(base_worktree_dir=tmp_path, executor_func=gap_executor)
    ctx = _make_context(ticket_id="HF-GAP", stage="development")

    result = handler.handle(ctx)
    assert result.outcome == "replan"
    assert result.cause_code == "needs_architecture_binding"


def test_development_handler_two_attempts_without_progress_terminates(tmp_path: Path):
    def failing_executor(ctx: StageContext, wt: Path) -> dict[str, Any]:
        return {"status": "failed", "error": "syntax defect in candidate"}

    handler = DevelopmentStageHandler(base_worktree_dir=tmp_path, executor_func=failing_executor)
    ticket_id = "HF-LOOP-TICKET"

    ctx1 = _make_context(ticket_id=ticket_id, stage="development", iteration=1)
    res1 = handler.handle(ctx1)
    assert res1.outcome == "failed"

    ctx2 = _make_context(ticket_id=ticket_id, stage="development", iteration=2)
    res2 = handler.handle(ctx2)
    assert res2.outcome == "failed"

    # Third attempt is halted because 2 consecutive failures occurred
    ctx3 = _make_context(ticket_id=ticket_id, stage="development", iteration=3)
    res3 = handler.handle(ctx3)
    assert res3.outcome == "replan"
    assert res3.cause_code == "two_attempts_without_progress"


def test_development_handler_failsafe_stale_lease(tmp_path: Path):
    handler = DevelopmentStageHandler(base_worktree_dir=tmp_path)
    ctx = _make_context(ticket_id="HF-STALE", stage="development", expires_delta_seconds=-10)

    result = handler.handle(ctx)
    assert result.outcome == "failed"
    assert result.cause_code == "stale_lease"


def test_validation_handler_conforms_to_stage_handler():
    handler = ValidationStageHandler()
    assert isinstance(handler, StageHandler)
    assert handler.STAGE == "validation"
    assert handler.descriptor.role == "validator"


def test_validation_handler_failure_reproves_even_with_self_assertion():
    def failing_validator(ctx: StageContext, sha: str) -> dict[str, Any]:
        # Externally observed oracle fails
        return {"passed": False, "error": "unit tests failed: 3 failed, 12 passed"}

    handler = ValidationStageHandler(validator_func=failing_validator)
    ctx = _make_context(ticket_id="HF-VAL-FAIL", stage="validation")

    result = handler.handle(ctx)
    assert result.outcome == "retry"
    assert "test_failure" in (result.cause_code or "")


def test_validation_handler_success_emits_report():
    def passing_validator(ctx: StageContext, sha: str) -> dict[str, Any]:
        return {"passed": True, "actual_cost": 0.05}

    handler = ValidationStageHandler(validator_func=passing_validator)
    ctx = _make_context(ticket_id="HF-VAL-PASS", stage="validation")

    result = handler.handle(ctx)
    assert result.outcome == "success"
    assert any("ref://validation-report/" in r for r in result.output_refs)
    assert result.actual_cost == 0.05


def test_review_handler_self_approval_strictly_prohibited():
    dev_identity = "developer-alice"
    # Reviewer has same identity subject as developer
    reviewer_identity = SanitizedIdentity(subject=dev_identity, role="reviewer", host="local")

    handler = ReviewStageHandler(reviewer_identity=reviewer_identity)
    ctx = _make_context(ticket_id="HF-SELF-APPROVE", stage="independent_review", identity=dev_identity)

    result = handler.handle(ctx)
    assert result.outcome == "failed"
    assert result.cause_code == "self_approval_prohibited"


def test_review_handler_cross_model_family_isolation_enforced():
    handler = ReviewStageHandler(
        reviewer_identity=SanitizedIdentity(subject="reviewer-bob", role="reviewer"),
        reviewer_model_family="qwen-fast",
        developer_model_family="qwen-fast",  # Same family violation
    )
    ctx = _make_context(ticket_id="HF-FAMILY-VIOLATION", stage="independent_review", identity="dev-alice")

    result = handler.handle(ctx)
    assert result.outcome == "failed"
    assert result.cause_code == "model_family_isolation_violation"


def test_review_handler_approval_emits_evidence_receipt():
    def passing_review(ctx: StageContext, cand: ImplementationCandidate | None) -> dict[str, Any]:
        return {"approved": True, "findings": [], "actual_cost": 0.02}

    handler = ReviewStageHandler(
        reviewer_identity=SanitizedIdentity(subject="reviewer-carol", role="reviewer"),
        reviewer_model_family="gpt-review",
        developer_model_family="qwen-fast",
        review_evaluator=passing_review,
    )
    ctx = _make_context(ticket_id="HF-REVIEW-PASS", stage="independent_review", identity="dev-alice")

    result = handler.handle(ctx)
    assert result.outcome == "success"
    assert len(result.evidence_refs) >= 1
    assert any("receipt://review/" in r for r in result.evidence_refs)
    assert result.actual_cost == 0.02


def test_review_handler_rejection_requests_changes():
    def rejecting_review(ctx: StageContext, cand: ImplementationCandidate | None) -> dict[str, Any]:
        return {"approved": False, "findings": ["Missing error handling in edge case"]}

    handler = ReviewStageHandler(
        reviewer_identity=SanitizedIdentity(subject="reviewer-dave", role="reviewer"),
        reviewer_model_family="deepseek-v4",
        developer_model_family="qwen-fast",
        review_evaluator=rejecting_review,
    )
    ctx = _make_context(ticket_id="HF-REVIEW-REJECT", stage="independent_review", identity="dev-alice")

    result = handler.handle(ctx)
    assert result.outcome == "retry"
    assert result.cause_code == "review_changes_required"
