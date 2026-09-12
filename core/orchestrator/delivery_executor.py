"""Autonomous remote delivery reconciliation and execution under DF-20 rules.

Governed by:
- HYBRID_WORKFLOW_PLAN_2026-09-08 (Section 5, 9, line 263 / HF-11)
- HYBRID_AUTONOMY_REQUIREMENTS (Section 5, 7, Scenario G8 / DF-20)
- Invariant: 'Entrega confirma estado/SHA remoto; checks antigos, timeout e replay não liberam candidato errado.'
- Invariant: 'Merge não é prova de operação.'
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from core.integrations.github import (
    GitHubCheck,
    GitHubClient,
    PullRequestSnapshot,
)
from core.orchestrator.delivery import (
    DeliveryDecision,
    DeliveryPolicy,
    DeliveryPolicyError,
    DeliveryRequest,
    DeliveryRisk,
    DeliveryStatus,
    IdempotencyConflictError,
    MergeQueue,
    MergeQueueEntry,
)

logger = logging.getLogger(__name__)


class RemoteDeliveryStatus(str, Enum):
    """Lifecycle state of the remote delivery reconciliation."""

    DELIVERED = "delivered"
    BLOCKED = "blocked"
    WAITING_CHECKS = "waiting_checks"
    MANUAL_REVIEW = "manual_review"
    FAILED = "failed"


class RemoteDeliveryResult(BaseModel):
    """Audit evidence record of remote delivery evaluation and reconciliation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    pull_request_number: int = Field(gt=0)
    candidate_sha: str = Field(min_length=7)
    remote_head_sha: str = Field(min_length=7)
    status: RemoteDeliveryStatus
    eligible: bool
    reason: str
    passed_checks: tuple[str, ...] = ()
    failed_checks: tuple[str, ...] = ()
    missing_checks: tuple[str, ...] = ()
    stale_checks: tuple[str, ...] = ()
    queue_entry: MergeQueueEntry | None = None
    remote_merged: bool = False
    reconciled_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RemoteDeliveryReconciler:
    """Reconciles remote GitHub pull request state, runs delivery policy and enqueues merge."""

    def __init__(
        self,
        github_client: GitHubClient | None = None,
        policy: DeliveryPolicy | None = None,
        merge_queue: MergeQueue | None = None,
        queue_storage_path: Path | str | None = None,
    ) -> None:
        self.github_client = github_client or GitHubClient()
        self.policy = policy or DeliveryPolicy()
        if merge_queue is not None:
            self.merge_queue = merge_queue
        elif queue_storage_path is not None:
            self.merge_queue = MergeQueue(queue_storage_path)
        else:
            self.merge_queue = MergeQueue()

    def reconcile_and_evaluate(
        self,
        request: DeliveryRequest,
        *,
        snapshot: PullRequestSnapshot | None = None,
        confirm_merged: bool = False,
    ) -> RemoteDeliveryResult:
        """Fetch remote snapshot (or use provided), evaluate DF-20 policy and enqueue if eligible."""
        try:
            current_snapshot = snapshot or self.github_client.get_pull_request_snapshot(
                request.repository, request.pull_request_number
            )
        except Exception as exc:
            logger.error(
                "Failed to fetch remote PR snapshot for %s#%d: %s",
                request.repository,
                request.pull_request_number,
                exc,
            )
            return RemoteDeliveryResult(
                task_id=request.task_id,
                repository=request.repository,
                pull_request_number=request.pull_request_number,
                candidate_sha=request.candidate_sha,
                remote_head_sha="",
                status=RemoteDeliveryStatus.FAILED,
                eligible=False,
                reason=f"remote_probe_failed: {exc}",
            )

        # 1. Evaluate deterministic delivery policy
        decision: DeliveryDecision = self.policy.evaluate(request, current_snapshot)

        # 2. Map decision to RemoteDeliveryStatus
        queue_entry: MergeQueueEntry | None = None
        if decision.status == DeliveryStatus.ELIGIBLE:
            # Enqueue into durable merge queue
            try:
                queue_entry = self.merge_queue.enqueue(request, decision)
                status = (
                    RemoteDeliveryStatus.DELIVERED
                    if confirm_merged
                    else RemoteDeliveryStatus.DELIVERED
                )
            except IdempotencyConflictError as exc:
                return RemoteDeliveryResult(
                    task_id=request.task_id,
                    repository=request.repository,
                    pull_request_number=request.pull_request_number,
                    candidate_sha=request.candidate_sha,
                    remote_head_sha=current_snapshot.head_sha,
                    status=RemoteDeliveryStatus.BLOCKED,
                    eligible=False,
                    reason=f"idempotency_conflict: {exc}",
                    passed_checks=decision.passed_checks,
                    failed_checks=decision.failed_checks,
                    missing_checks=decision.missing_checks,
                    stale_checks=decision.stale_checks,
                )
        elif decision.status == DeliveryStatus.MANUAL_REVIEW:
            status = RemoteDeliveryStatus.MANUAL_REVIEW
        else:
            is_only_missing_checks = (
                bool(decision.missing_checks)
                and not decision.failed_checks
                and not decision.stale_checks
                and "mismatch" not in decision.reason
                and "not_open" not in decision.reason
                and "draft" not in decision.reason
                and "mergeability" not in decision.reason
            )
            if is_only_missing_checks:
                status = RemoteDeliveryStatus.WAITING_CHECKS
            else:
                status = RemoteDeliveryStatus.BLOCKED

        return RemoteDeliveryResult(
            task_id=request.task_id,
            repository=request.repository,
            pull_request_number=request.pull_request_number,
            candidate_sha=request.candidate_sha,
            remote_head_sha=current_snapshot.head_sha,
            status=status,
            eligible=decision.eligible,
            reason=decision.reason,
            passed_checks=decision.passed_checks,
            failed_checks=decision.failed_checks,
            missing_checks=decision.missing_checks,
            stale_checks=decision.stale_checks,
            queue_entry=queue_entry,
            remote_merged=confirm_merged,
        )


__all__ = [
    "RemoteDeliveryReconciler",
    "RemoteDeliveryResult",
    "RemoteDeliveryStatus",
]
