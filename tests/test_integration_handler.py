"""Comprehensive test suite for the reconciled GitHub integration stage (HF-11-01).

Validates:
- Conformance to StageHandler protocol and StageResult contract.
- Idempotency: lost PR response or repeated invocation does not duplicate PR or merge.
- Delivery blocking: draft PR, failed checks, and checks on stale commit SHA block integration.
- Remote ancestry: candidates without remote main ancestry are blocked from delivery.
- Security and sanitization: auth tokens (PAT, Bearer, etc.) are strictly scrubbed from logs and results.
- Independence: strict prohibition of self-approval (no autoapprove, requires valid EvidenceReceipt).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.integrations.github import (
    GitHubCheck,
    GitHubClient,
    PullRequestSnapshot,
)
from core.orchestrator.delivery import (
    DeliveryDecision,
    DeliveryPolicy,
    DeliveryRequest,
    DeliveryRisk,
    DeliveryStatus,
    MergeQueue,
)
from core.orchestrator.delivery_executor import (
    RemoteDeliveryReconciler,
    RemoteDeliveryResult,
    RemoteDeliveryStatus,
)
from core.workflow.contracts import (
    EvidenceResult,
    SanitizedIdentity,
)
from core.workflow.control_contracts import (
    Claim,
    JobKey,
    StageContext,
    StageResult,
)
from core.workflow.handlers import StageHandler
from core.workflow.integration_handler import (
    IntegrationStageHandler,
    sanitize_ref,
    sanitize_text,
)
from core.workflow.verification import (
    EvidenceReceipt,
    ValidationMode,
)


def _make_context(
    ticket_id: str = "HF-11-INT-01",
    candidate_digest: str = "b" * 40,
    identity: str = "worker-dev-alice",
    pr_number: int = 11,
    iteration: int = 1,
    expires_delta_seconds: int = 60,
    input_refs: list[str] | None = None,
) -> StageContext:
    jk = JobKey(
        run_id=f"run_{ticket_id}",
        ticket_id=ticket_id,
        plan_version="1.0",
        stage="integration",
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
    refs = input_refs if input_refs is not None else [f"ref://pr/{pr_number}"]
    return StageContext(
        claim=claim,
        plan_ref="plan://continuous-autonomy",
        plan_digest="a" * 64,
        candidate_digest=candidate_digest,
        config_version="v1.0",
        environment_ref="release-binding",
        identity=identity,
        route_ref="route://release/github",
        memory_version="mem_v1",
        input_refs=refs,
    )


def _make_receipt(
    ticket_id: str = "HF-11-INT-01",
    candidate_digest: str = "b" * 40,
    reviewer_subject: str = "independent-reviewer-bob",
    reviewer_role: str = "reviewer",
    result: EvidenceResult = EvidenceResult.PASSED,
) -> EvidenceReceipt:
    now = datetime.now(UTC)
    return EvidenceReceipt(
        receipt_id=f"receipt://review/{ticket_id}/{now.strftime('%Y%m%d%H%M%S')}",
        producer=SanitizedIdentity(
            subject=reviewer_subject,
            role=reviewer_role,
            host="local",
        ),
        subject=ticket_id,
        requirement="independent_code_and_oracle_review",
        artifact_hash=candidate_digest,
        result=result,
        mode=ValidationMode.TARGET_ENVIRONMENT,
        observed_at=now,
        candidate_digest=candidate_digest,
        plan_digest="a" * 64,
        config_version="v1.0",
        environment_ref="release-binding",
        route="route://release/github",
    )


def _make_snapshot(
    *,
    repository: str = "GCamposGit/DarkFactory",
    number: int = 11,
    base_sha: str = "a" * 40,
    head_sha: str = "b" * 40,
    state: str = "open",
    draft: bool = False,
    mergeable: bool = True,
    checks: tuple[GitHubCheck, ...] | None = None,
) -> PullRequestSnapshot:
    if checks is None:
        checks = (
            GitHubCheck(name="trusted-pr-policy", head_sha=head_sha, status="completed", conclusion="success"),
            GitHubCheck(name="pr-validation", head_sha=head_sha, status="completed", conclusion="success"),
        )
    return PullRequestSnapshot(
        repository=repository,
        number=number,
        base_sha=base_sha,
        head_sha=head_sha,
        state=state,
        draft=draft,
        mergeable=mergeable,
        checks=checks,
    )


# ---------------------------------------------------------------------------
# 1. Conformance to StageHandler and StageResult Protocol
# ---------------------------------------------------------------------------

def test_integration_handler_conforms_to_stage_handler(tmp_path: Path) -> None:
    """IntegrationStageHandler implements StageHandler and returns valid StageResult."""
    cand_sha = "b" * 40
    base_sha = "a" * 40
    ctx = _make_context(candidate_digest=cand_sha)
    receipt = _make_receipt(candidate_digest=cand_sha)
    snapshot = _make_snapshot(base_sha=base_sha, head_sha=cand_sha)

    queue = MergeQueue(tmp_path / "merge_queue.json")
    reconciler = RemoteDeliveryReconciler(merge_queue=queue)

    handler = IntegrationStageHandler(
        reconciler=reconciler,
        receipt_provider=lambda tid: receipt,
        snapshot_provider=lambda repo, pr: snapshot,
        default_base_sha=base_sha,
        ancestry_verified=True,
    )

    # Protocol conformance
    assert isinstance(handler, StageHandler)
    assert handler.STAGE == "integration"
    assert handler.VERSION == "v1"
    assert handler.descriptor.role == "integrator"

    result = handler.handle(ctx)
    assert isinstance(result, StageResult)
    assert result.outcome == "success"
    assert len(result.output_refs) >= 2
    assert any("ref://integration/pr/" in ref for ref in result.output_refs)
    assert any("ref://integration/queue/" in ref for ref in result.output_refs)
    assert any(receipt.receipt_id in ref for ref in result.evidence_refs)
    assert any("op://github/merge_queue/" in ref for ref in result.operation_refs)
    assert result.actual_cost == 0.0


# ---------------------------------------------------------------------------
# 2. Idempotency: Lost PR Response Does Not Duplicate
# ---------------------------------------------------------------------------

def test_idempotency_lost_pr_response_does_not_duplicate(tmp_path: Path) -> None:
    """Lost PR response or retry does not duplicate queue entry or merge."""
    cand_sha = "c" * 40
    base_sha = "a" * 40
    ctx = _make_context(ticket_id="HF-11-IDEMP", candidate_digest=cand_sha, pr_number=42)
    receipt = _make_receipt(ticket_id="HF-11-IDEMP", candidate_digest=cand_sha)
    snapshot = _make_snapshot(number=42, base_sha=base_sha, head_sha=cand_sha)

    queue_file = tmp_path / "merge_queue.json"
    queue = MergeQueue(queue_file)
    reconciler = RemoteDeliveryReconciler(merge_queue=queue)

    handler = IntegrationStageHandler(
        reconciler=reconciler,
        receipt_provider=lambda tid: receipt,
        snapshot_provider=lambda repo, pr: snapshot,
        default_base_sha=base_sha,
        default_pr_number=42,
        ancestry_verified=True,
    )

    # First execution: successfully enqueued
    res1 = handler.handle(ctx)
    assert res1.outcome == "success"
    assert len(queue.entries()) == 1
    first_entry = queue.entries()[0]
    assert first_entry.replay_count == 0

    # Second execution (simulating lost response or replay of identical context):
    # Does NOT duplicate merge queue entry!
    res2 = handler.handle(ctx)
    assert res2.outcome == "success"
    assert len(queue.entries()) == 1
    second_entry = queue.entries()[0]
    assert second_entry.queue_id == first_entry.queue_id

    # And attempting with different candidate SHA triggers idempotency conflict
    conflicting_cand = "d" * 40
    ctx_conflict = _make_context(ticket_id="HF-11-IDEMP", candidate_digest=conflicting_cand, pr_number=42)
    receipt_conflict = _make_receipt(ticket_id="HF-11-IDEMP", candidate_digest=conflicting_cand)
    handler_conflict = IntegrationStageHandler(
        reconciler=reconciler,
        receipt_provider=lambda tid: receipt_conflict,
        snapshot_provider=lambda repo, pr: _make_snapshot(number=42, base_sha=base_sha, head_sha=conflicting_cand),
        default_base_sha=base_sha,
        default_pr_number=42,
        ancestry_verified=True,
    )
    res_conflict = handler_conflict.handle(ctx_conflict)
    assert res_conflict.outcome == "failed"
    assert "idempotency_conflict" in (res_conflict.cause_code or "")


# ---------------------------------------------------------------------------
# 3. Delivery Blocking: Draft PR and Checks on Stale Commit SHA
# ---------------------------------------------------------------------------

def test_blocked_by_draft_pr_and_stale_checks(tmp_path: Path) -> None:
    """Draft PR and checks evaluated on older commit SHA fail closed."""
    cand_sha = "b" * 40
    base_sha = "a" * 40
    ctx = _make_context(candidate_digest=cand_sha)
    receipt = _make_receipt(candidate_digest=cand_sha)

    queue = MergeQueue(tmp_path / "merge_queue.json")
    reconciler = RemoteDeliveryReconciler(merge_queue=queue)

    # 1. Draft PR blocks delivery
    draft_snapshot = _make_snapshot(base_sha=base_sha, head_sha=cand_sha, draft=True)
    handler_draft = IntegrationStageHandler(
        reconciler=reconciler,
        receipt_provider=lambda tid: receipt,
        snapshot_provider=lambda repo, pr: draft_snapshot,
        default_base_sha=base_sha,
        ancestry_verified=True,
    )
    res_draft = handler_draft.handle(ctx)
    assert res_draft.outcome == "failed"
    assert "draft" in (res_draft.cause_code or "").lower()
    assert len(queue.entries()) == 0

    # 2. Checks on stale SHA block delivery
    stale_check = GitHubCheck(
        name="trusted-pr-policy",
        head_sha="9" * 40,  # Old SHA!
        status="completed",
        conclusion="success",
    )
    stale_snapshot = _make_snapshot(
        base_sha=base_sha,
        head_sha=cand_sha,
        checks=(
            stale_check,
            GitHubCheck(name="pr-validation", head_sha=cand_sha, status="completed", conclusion="success"),
        ),
    )
    handler_stale = IntegrationStageHandler(
        reconciler=reconciler,
        receipt_provider=lambda tid: receipt,
        snapshot_provider=lambda repo, pr: stale_snapshot,
        default_base_sha=base_sha,
        ancestry_verified=True,
    )
    res_stale = handler_stale.handle(ctx)
    assert res_stale.outcome == "failed"
    assert "stale_checks" in (res_stale.cause_code or "")
    assert len(queue.entries()) == 0

    # 3. Failed check blocks delivery
    failed_check = GitHubCheck(
        name="pr-validation",
        head_sha=cand_sha,
        status="completed",
        conclusion="failure",
    )
    failed_snapshot = _make_snapshot(
        base_sha=base_sha,
        head_sha=cand_sha,
        checks=(
            GitHubCheck(name="trusted-pr-policy", head_sha=cand_sha, status="completed", conclusion="success"),
            failed_check,
        ),
    )
    handler_failed = IntegrationStageHandler(
        reconciler=reconciler,
        receipt_provider=lambda tid: receipt,
        snapshot_provider=lambda repo, pr: failed_snapshot,
        default_base_sha=base_sha,
        ancestry_verified=True,
    )
    res_failed = handler_failed.handle(ctx)
    assert res_failed.outcome == "failed"
    assert "failed_checks" in (res_failed.cause_code or "")
    assert len(queue.entries()) == 0


# ---------------------------------------------------------------------------
# 4. Delivery Blocking: Candidate Without Remote Main Ancestry
# ---------------------------------------------------------------------------

def test_blocked_when_candidate_lacks_remote_main_ancestry(tmp_path: Path) -> None:
    """Candidate without ancestry in remote main is blocked from delivery."""
    cand_sha = "b" * 40
    base_sha = "a" * 40
    ctx = _make_context(candidate_digest=cand_sha)
    receipt = _make_receipt(candidate_digest=cand_sha)
    snapshot = _make_snapshot(base_sha=base_sha, head_sha=cand_sha)

    queue = MergeQueue(tmp_path / "merge_queue.json")
    reconciler = RemoteDeliveryReconciler(
        merge_queue=queue,
        ancestry_verifier=lambda base, cand: False,  # Ancestry verification explicitly fails
    )

    handler = IntegrationStageHandler(
        reconciler=reconciler,
        receipt_provider=lambda tid: receipt,
        snapshot_provider=lambda repo, pr: snapshot,
        default_base_sha=base_sha,
        ancestry_verifier=lambda base, cand: False,
        ancestry_verified=False,
    )

    res = handler.handle(ctx)
    assert res.outcome == "failed"
    assert "ancestry" in (res.cause_code or "").lower()
    assert len(queue.entries()) == 0


# ---------------------------------------------------------------------------
# 5. Security & Sanitization: Auth Tokens Never Leaked in Results or Logs
# ---------------------------------------------------------------------------

def test_security_sanitization_no_tokens_in_logs_or_results(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Authentication tokens never appear in output_refs, evidence_refs, or logs."""
    secret_token = "ghp_VERY_SECRET_PAT_TOKEN_1234567890"
    cand_sha = "b" * 40
    base_sha = "a" * 40

    ctx = _make_context(
        ticket_id="HF-11-SEC",
        candidate_digest=cand_sha,
        input_refs=[
            f"ref://pr/11",
            f"https://{secret_token}@github.com/GCamposGit/DarkFactory/pull/11",
        ],
    )
    receipt = _make_receipt(ticket_id="HF-11-SEC", candidate_digest=cand_sha)
    snapshot = _make_snapshot(base_sha=base_sha, head_sha=cand_sha)

    client = GitHubClient(token=secret_token)
    queue = MergeQueue(tmp_path / "merge_queue.json")
    reconciler = RemoteDeliveryReconciler(github_client=client, merge_queue=queue)

    handler = IntegrationStageHandler(
        reconciler=reconciler,
        github_client=client,
        receipt_provider=lambda tid: receipt,
        snapshot_provider=lambda repo, pr: snapshot,
        default_base_sha=base_sha,
        ancestry_verified=True,
    )

    caplog.set_level(logging.DEBUG)
    result = handler.handle(ctx)

    assert result.outcome == "success"

    # 1. Check all output and evidence refs
    all_refs = result.output_refs + result.evidence_refs + result.operation_refs
    for ref in all_refs:
        assert secret_token not in ref
        assert "ghp_" not in ref

    if result.cause_code:
        assert secret_token not in result.cause_code

    # 2. Check logged output
    for record in caplog.records:
        assert secret_token not in record.getMessage()

    # 3. Direct sanitizer tests
    raw_text = f"Connecting with Bearer {secret_token} to https://user:{secret_token}@github.com"
    sanitized = sanitize_text(raw_text, token_hint=secret_token)
    assert secret_token not in sanitized
    assert "https://" in sanitized


# ---------------------------------------------------------------------------
# 6. Sem Autoapprove: Requires Valid Independent Review Receipt
# ---------------------------------------------------------------------------

def test_sem_autoapprove_requires_valid_receipt(tmp_path: Path) -> None:
    """Missing, mismatched, or self-approved review receipts fail closed."""
    cand_sha = "b" * 40
    base_sha = "a" * 40
    developer_identity = "agent_developer_alice"
    ctx = _make_context(candidate_digest=cand_sha, identity=developer_identity)
    snapshot = _make_snapshot(base_sha=base_sha, head_sha=cand_sha)

    queue = MergeQueue(tmp_path / "merge_queue.json")
    reconciler = RemoteDeliveryReconciler(merge_queue=queue)

    # 1. Missing receipt fails closed
    handler_missing = IntegrationStageHandler(
        reconciler=reconciler,
        receipt_provider=lambda tid: None,
        snapshot_provider=lambda repo, pr: snapshot,
        default_base_sha=base_sha,
        ancestry_verified=True,
    )
    res_missing = handler_missing.handle(ctx)
    assert res_missing.outcome == "failed"
    assert "missing_evidence_receipt" in (res_missing.cause_code or "")

    # 2. Self-approved receipt (reviewer == developer) fails closed
    self_receipt = _make_receipt(
        candidate_digest=cand_sha,
        reviewer_subject=developer_identity,  # Same as context.identity!
    )
    handler_self = IntegrationStageHandler(
        reconciler=reconciler,
        receipt_provider=lambda tid: self_receipt,
        snapshot_provider=lambda repo, pr: snapshot,
        default_base_sha=base_sha,
        ancestry_verified=True,
    )
    res_self = handler_self.handle(ctx)
    assert res_self.outcome == "failed"
    assert "self_approval_prohibited" in (res_self.cause_code or "")

    # 3. Candidate digest mismatch fails closed
    mismatched_receipt = _make_receipt(
        candidate_digest="9" * 40,  # Different SHA!
    )
    handler_mismatch = IntegrationStageHandler(
        reconciler=reconciler,
        receipt_provider=lambda tid: mismatched_receipt,
        snapshot_provider=lambda repo, pr: snapshot,
        default_base_sha=base_sha,
        ancestry_verified=True,
    )
    res_mismatch = handler_mismatch.handle(ctx)
    assert res_mismatch.outcome == "failed"
    assert "evidence_mismatch" in (res_mismatch.cause_code or "")

    # 4. Receipt with result != PASSED fails closed
    failed_receipt = _make_receipt(
        candidate_digest=cand_sha,
        result=EvidenceResult.FAILED,
    )
    handler_not_passed = IntegrationStageHandler(
        reconciler=reconciler,
        receipt_provider=lambda tid: failed_receipt,
        snapshot_provider=lambda repo, pr: snapshot,
        default_base_sha=base_sha,
        ancestry_verified=True,
    )
    res_not_passed = handler_not_passed.handle(ctx)
    assert res_not_passed.outcome == "failed"
    assert "evidence_receipt_not_passed" in (res_not_passed.cause_code or "")


# ---------------------------------------------------------------------------
# 7. Reconciler Lookup and Reconnection Recovery
# ---------------------------------------------------------------------------

def test_reconciler_lookup_by_repo_pr_recovers_after_probe_timeout(tmp_path: Path) -> None:
    """RemoteDeliveryReconciler recovers delivered entry after timeout/reconnection."""
    cand_sha = "a" * 40
    base_sha = "0" * 40
    queue = MergeQueue(tmp_path / "merge_queue.json")
    reconciler = RemoteDeliveryReconciler(merge_queue=queue)

    req = DeliveryRequest(
        task_id="HF-11-REC",
        repository="GCamposGit/DarkFactory",
        pull_request_number=99,
        base_sha=base_sha,
        candidate_sha=cand_sha,
        risk_class=DeliveryRisk.B,
        required_checks=("trusted-pr-policy", "pr-validation"),
        idempotency_key="hf11-rec-99",
    )

    green_snapshot = _make_snapshot(
        repository="GCamposGit/DarkFactory",
        number=99,
        base_sha=base_sha,
        head_sha=cand_sha,
    )

    # Initial delivery
    res_ok = reconciler.reconcile_and_evaluate(req, snapshot=green_snapshot, confirm_merged=True)
    assert res_ok.status == RemoteDeliveryStatus.DELIVERED
    assert res_ok.queue_entry is not None

    # Verify lookup_by_repo_pr
    found_entry = reconciler.lookup_by_repo_pr("GCamposGit/DarkFactory", 99)
    assert found_entry is not None
    assert found_entry.candidate_sha == cand_sha

    # Now simulate reconnection with network failure (snapshot=None, probe fails)
    failing_client = GitHubClient(base_url="https://api.github.com", transport=lambda u, h: (_ for _ in ()).throw(TimeoutError("probe timed out")))
    reconciler_reconnect = RemoteDeliveryReconciler(github_client=failing_client, merge_queue=queue)

    # The reconciler recovers from merge queue instead of failing
    res_recovered = reconciler_reconnect.reconcile_and_evaluate(req, snapshot=None, confirm_merged=True)
    assert res_recovered.status == RemoteDeliveryStatus.DELIVERED
    assert res_recovered.eligible is True
    assert "replayed_from_queue_after_reconnection" in res_recovered.reason
