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
from core.workflow.handlers import (
    StageHandler,
    build_handlers,
    dispatch_stage,
)
from core.workflow.quality_handlers import (
    ReviewStageHandler,
    ValidationStageHandler,
)
from core.workflow.verification import VerificationContext


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


def test_development_handler_worktree_isolation_per_iteration(tmp_path: Path):
    handler = DevelopmentStageHandler(base_worktree_dir=tmp_path)
    ctx_it1 = _make_context(ticket_id="HF-ISO-IT", stage="development", iteration=1)
    ctx_it2 = _make_context(ticket_id="HF-ISO-IT", stage="development", iteration=2)

    res1 = handler.handle(ctx_it1)
    res2 = handler.handle(ctx_it2)

    wt1 = next(r for r in res1.output_refs if "ref://worktree/" in r)
    wt2 = next(r for r in res2.output_refs if "ref://worktree/" in r)
    assert wt1 != wt2
    assert "_1" in wt1
    assert "_2" in wt2

    # Physical directory creation
    wt1_dir = tmp_path / wt1.replace("ref://worktree/", "")
    wt2_dir = tmp_path / wt2.replace("ref://worktree/", "")
    assert wt1_dir.is_dir()
    assert wt2_dir.is_dir()


def test_development_handler_replan_from_handoff_state(tmp_path: Path):
    class MockHandoff:
        state = "needs_architecture_binding"
        allowed_paths = ["core/workflow/contracts.py"]

    handler = DevelopmentStageHandler(
        base_worktree_dir=tmp_path,
        handoff_provider=lambda tid: MockHandoff(),
    )
    ctx = _make_context(ticket_id="HF-HANDOFF-GAP", stage="development")

    result = handler.handle(ctx)
    assert result.outcome == "replan"
    assert result.cause_code == "needs_architecture_binding"
    assert "ref://planning/needs_architecture_binding/HF-HANDOFF-GAP" in result.output_refs


def test_validation_handler_stale_lease():
    handler = ValidationStageHandler()
    ctx = _make_context(ticket_id="HF-VAL-STALE", stage="validation", expires_delta_seconds=-10)

    result = handler.handle(ctx)
    assert result.outcome == "failed"
    assert result.cause_code == "stale_lease"


def test_validation_handler_stores_and_retrieves_report():
    handler = ValidationStageHandler(validator_func=lambda ctx, sha: {"passed": True, "actual_cost": 0.03})
    ctx = _make_context(ticket_id="HF-REPORT-LOOKUP", stage="validation")

    result = handler.handle(ctx)
    assert result.outcome == "success"

    # Lookup by ticket_id
    report_by_ticket = handler.get_report("HF-REPORT-LOOKUP")
    assert report_by_ticket is not None
    assert report_by_ticket["passed"] is True
    assert report_by_ticket["actual_cost"] == 0.03

    # Lookup by report_id
    report_ref = next(r for r in result.output_refs if "ref://validation-report/" in r)
    report_id = report_ref.replace("ref://validation-report/", "")
    report_by_id = handler.get_report(report_id)
    assert report_by_id is not None
    assert report_by_id["ticket_id"] == "HF-REPORT-LOOKUP"


def test_review_handler_verification_context_authority_and_receipt_retrieval():
    fixed_now = datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC)
    cand_digest = "cand_digest_verified_123456"
    v_ctx = VerificationContext(
        now=fixed_now,
        policy_version="1.0",
        plan_digest="a" * 64,
        candidate_digest=cand_digest,
        config_version="v1.0",
        expected_environment_ref="env-local-test",
        expected_identity=SanitizedIdentity(subject="reviewer-auth", role="reviewer"),
        expected_route="route://economy/local",
    )

    handler = ReviewStageHandler(
        reviewer_identity=SanitizedIdentity(subject="reviewer-auth", role="reviewer"),
        reviewer_model_family="gpt-review",
        developer_model_family="qwen-fast",
        verification_context=v_ctx,
    )

    # 1. Matching candidate digest succeeds with fixed_now and canonical requirement
    ctx_pass = _make_context(
        ticket_id="HF-AUTH-REVIEW",
        stage="independent_review",
        identity="dev-alice",
        candidate_digest=cand_digest,
    )
    res_pass = handler.handle(ctx_pass)
    assert res_pass.outcome == "success"

    receipt = handler.get_receipt("HF-AUTH-REVIEW")
    assert receipt is not None
    assert receipt.observed_at == fixed_now
    assert receipt.requirement == "independent_review"
    assert receipt.candidate_digest == cand_digest

    # 2. Mismatched candidate digest under VerificationContext authority fails closed
    ctx_mismatch = _make_context(
        ticket_id="HF-AUTH-MISMATCH",
        stage="independent_review",
        identity="dev-alice",
        candidate_digest="cand_digest_TAMPERED",
    )
    res_mismatch = handler.handle(ctx_mismatch)
    assert res_mismatch.outcome == "failed"
    assert res_mismatch.cause_code == "candidate_digest_mismatch_with_verification_context"


def test_build_handlers_integration_canonical_flow(tmp_path: Path):
    ticket_id = "HF-E2E-CANONICAL"
    dev_handler = DevelopmentStageHandler(base_worktree_dir=tmp_path)
    val_handler = ValidationStageHandler(
        candidate_provider=dev_handler.get_candidate,
        validator_func=lambda ctx, sha: {"passed": True, "actual_cost": 0.05},
    )
    rev_handler = ReviewStageHandler(
        reviewer_identity=SanitizedIdentity(subject="reviewer-carol", role="reviewer"),
        reviewer_model_family="deepseek-v4",
        developer_model_family="qwen-fast",
        candidate_provider=dev_handler.get_candidate,
    )

    registry = build_handlers(
        bindings={
            "development": dev_handler,
            "validation": val_handler,
            "independent_review": rev_handler,
        }
    )

    # Verify registered in registry
    assert ("development", "v1") in registry
    assert ("validation", "v1") in registry
    assert ("independent_review", "v1") in registry

    # Stage 1: Development
    dev_ctx = _make_context(ticket_id=ticket_id, stage="development", identity="developer-alice")
    dev_res = dispatch_stage(registry, dev_ctx)
    assert dev_res.outcome == "success"
    cand = dev_handler.get_candidate(ticket_id)
    assert cand is not None
    assert len(cand.candidate_sha) >= 7

    # Stage 2: Validation
    val_ctx = _make_context(
        ticket_id=ticket_id,
        stage="validation",
        identity="validator-bob",
        candidate_digest=cand.candidate_digest,
    )
    val_res = dispatch_stage(registry, val_ctx)
    assert val_res.outcome == "success"
    report = val_handler.get_report(ticket_id)
    assert report is not None
    assert report["passed"] is True

    # Stage 3: Independent Review
    rev_ctx = _make_context(
        ticket_id=ticket_id,
        stage="independent_review",
        identity="developer-alice",
        candidate_digest=cand.candidate_digest,
    )
    rev_res = dispatch_stage(registry, rev_ctx)
    assert rev_res.outcome == "success"
    receipt = rev_handler.get_receipt(ticket_id)
    assert receipt is not None
    assert receipt.producer.subject == "reviewer-carol"
    assert receipt.requirement == "independent_review"


def test_build_handlers_integration_dispatches_replan_and_retry(tmp_path: Path):
    ticket_id = "HF-E2E-FAILURES"
    dev_handler = DevelopmentStageHandler(
        base_worktree_dir=tmp_path,
        executor_func=lambda ctx, wt: {"status": "replan", "cause_code": "needs_architecture_binding"},
    )
    val_handler = ValidationStageHandler(
        validator_func=lambda ctx, sha: {"passed": False, "error": "unit tests failed"},
    )
    rev_handler = ReviewStageHandler(
        reviewer_identity=SanitizedIdentity(subject="dev-alice", role="reviewer"),  # same subject
        reviewer_model_family="deepseek-v4",
        developer_model_family="qwen-fast",
    )

    registry = build_handlers(
        bindings={
            "development": dev_handler,
            "validation": val_handler,
            "independent_review": rev_handler,
        }
    )

    # Replan dispatch in development
    dev_ctx = _make_context(ticket_id=ticket_id, stage="development", identity="dev-alice")
    dev_res = dispatch_stage(registry, dev_ctx)
    assert dev_res.outcome == "replan"
    assert dev_res.cause_code == "needs_architecture_binding"

    # Retry dispatch in validation
    val_ctx = _make_context(ticket_id=ticket_id, stage="validation", identity="validator-bob")
    val_res = dispatch_stage(registry, val_ctx)
    assert val_res.outcome == "retry"
    assert "test_failure" in (val_res.cause_code or "")

    # Failed dispatch in review (self-approval prohibited)
    rev_ctx = _make_context(ticket_id=ticket_id, stage="independent_review", identity="dev-alice")
    rev_res = dispatch_stage(registry, rev_ctx)
    assert rev_res.outcome == "failed"
    assert rev_res.cause_code == "self_approval_prohibited"

