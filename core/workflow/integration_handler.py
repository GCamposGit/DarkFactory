"""Integration stage handler and GitHub remote delivery reconciler (HF-11-01).

Governed by:
- Universal Engineering Standards (AGENTS.md)
- docs/handoffs/continuous-autonomy/HF-11-01.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- core/orchestrator/delivery_executor.py
- core/orchestrator/delivery.py
- core/workflow/control_contracts.py
- core/workflow/handlers.py

Key Invariants:
1. Conforms to StageHandler protocol: handle(context: StageContext) -> StageResult.
2. Stage: "integration", version: "v1".
3. Validates EvidenceReceipt of independent reviewer against candidate_digest.
4. Executes remote reconciliation with GitHub via RemoteDeliveryReconciler / GitHubClient.
5. Blocks delivery if draft PR, failed checks, checks on stale SHA, or missing remote main ancestry.
6. Strict sanitization: NEVER leak tokens in output_refs, evidence_refs, operation_refs, or logs.
7. Idempotency: lost PR response or re-invocation does not duplicate PR or merge.
8. No autoapprove: requires valid independent review receipt (reviewer != developer).
9. Returns deterministic StageResult.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Callable

from core.integrations.github import (
    GitHubClient,
    PullRequestSnapshot,
)
from core.orchestrator.delivery import (
    DeliveryRequest,
    DeliveryRisk,
    DeliveryStatus,
)
from core.orchestrator.delivery_executor import (
    RemoteDeliveryReconciler,
    RemoteDeliveryResult,
    RemoteDeliveryStatus,
)
from core.workflow.contracts import (
    EvidenceResult,
    SanitizedIdentity,
    _looks_like_secret_value,
)
from core.workflow.control_contracts import (
    HandlerDescriptor,
    JobKey,
    StageContext,
    StageResult,
)
from core.workflow.handlers import StageHandler
from core.workflow.verification import EvidenceReceipt

logger = logging.getLogger("darkfac.workflow.integration_handler")

INTEGRATION_STAGE: str = "integration"
VERSION: str = "v1"

_TOKEN_PATTERN = re.compile(
    r"(?:"
    r"ghp_[A-Za-z0-9_]{10,}|"
    r"gho_[A-Za-z0-9_]{10,}|"
    r"ghu_[A-Za-z0-9_]{10,}|"
    r"ghs_[A-Za-z0-9_]{10,}|"
    r"ghr_[A-Za-z0-9_]{10,}|"
    r"github_pat_[A-Za-z0-9_]{10,}|"
    r"Bearer\s+[A-Za-z0-9_.~+\/=-]{8,}|"
    r"sk-[A-Za-z0-9_.-]{20,}|"
    r"(?:synthetic[_-]?token|token[_-]?synthetic)[A-Za-z0-9_.-]*|"
    r"(?:password|passwd|token|api[_-]?key|secret|credential|access[_-]?token|auth[_-]?token)\s*[:=]\s*[^\s&]+"
    r")",
    re.IGNORECASE,
)
_CREDENTIAL_URL_PATTERN = re.compile(r"https?://(?:[^@/:\s]+)(?::(?:[^@/:\s]+))?@")


def sanitize_text(
    text: str,
    token_hint: str | None = None,
    extra_tokens: tuple[str, ...] | list[str] | None = None,
) -> str:
    """Sanitize secret tokens or embedded credentials from string."""
    if not text:
        return text
    result = text
    all_hints: list[str] = []
    if token_hint and len(token_hint) >= 4:
        all_hints.append(token_hint)
    if extra_tokens:
        for t in extra_tokens:
            if t and len(t) >= 4 and t not in all_hints:
                all_hints.append(t)
    for hint in all_hints:
        result = result.replace(hint, "[REDACTED_TOKEN]")
    result = _TOKEN_PATTERN.sub("[REDACTED_TOKEN]", result)
    result = _CREDENTIAL_URL_PATTERN.sub("https://", result)
    return result


def sanitize_ref(
    ref: str,
    token_hint: str | None = None,
    extra_tokens: tuple[str, ...] | list[str] | None = None,
) -> str:
    """Sanitize reference string, verifying that no secret pattern remains."""
    cleaned = sanitize_text(ref, token_hint=token_hint, extra_tokens=extra_tokens)
    if _looks_like_secret_value(cleaned):
        raise ValueError(f"Sanitized reference still looks like a secret value: {cleaned}")
    return cleaned



class IntegrationStageHandler:
    """StageHandler for the reconciled GitHub integration stage (HF-11-01).

    Reconciles PR state on GitHub, verifies checks/draft/head/branch/ancestry,
    enforces independent review receipt, and guarantees idempotency.
    """

    STAGE: str = INTEGRATION_STAGE
    VERSION: str = VERSION

    descriptor: HandlerDescriptor = HandlerDescriptor(
        stage=INTEGRATION_STAGE,
        version=VERSION,
        input_schema_ref="schema://contracts/EvidenceReceipt",
        output_schema_ref="schema://contracts/IntegrationSnapshot",
        role="integrator",
        required_capabilities=["github_pr_snapshot", "git_merge", "verify_remote_head"],
        timeout_seconds=600,
        conflict_scope="job",
    )

    def __init__(
        self,
        reconciler: RemoteDeliveryReconciler | None = None,
        github_client: GitHubClient | None = None,
        receipt_provider: Callable[[str], EvidenceReceipt | None] | None = None,
        receipt_map: Mapping[str, EvidenceReceipt] | None = None,
        delivery_request_provider: Callable[[StageContext], DeliveryRequest | None] | None = None,
        snapshot_provider: Callable[[str, int], PullRequestSnapshot | None] | None = None,
        ancestry_verifier: Callable[[str, str], bool] | None = None,
        repository: str = "GCamposGit/DarkFactory",
        default_pr_number: int | None = None,
        default_base_sha: str = "0" * 40,
        required_checks: tuple[str, ...] = ("trusted-pr-policy", "pr-validation"),
        confirm_merged: bool = False,
        remote_main_sha: str | None = None,
        ancestry_verified: bool | None = None,
        token_hints: tuple[str, ...] = (),
    ) -> None:
        self.github_client = github_client or (reconciler.github_client if reconciler else GitHubClient())
        self.reconciler = reconciler or RemoteDeliveryReconciler(
            github_client=self.github_client,
            ancestry_verifier=ancestry_verifier,
        )
        self.receipt_provider = receipt_provider
        self.receipt_map = dict(receipt_map) if receipt_map is not None else None
        self.delivery_request_provider = delivery_request_provider
        self.snapshot_provider = snapshot_provider
        self.ancestry_verifier = ancestry_verifier
        self.repository = repository
        self.default_pr_number = default_pr_number
        self.default_base_sha = default_base_sha
        self.required_checks = required_checks
        self.confirm_merged = confirm_merged
        self.remote_main_sha = remote_main_sha
        self.ancestry_verified = ancestry_verified
        self.token_hints = token_hints

    def _extract_pr_number(self, context: StageContext) -> int:
        """Extract PR number from input_refs, route_ref, or default."""
        if self.default_pr_number is not None:
            return self.default_pr_number

        for ref in context.input_refs:
            match = re.search(r"(?:pr|pull|pulls)[/:#](\d+)", ref, re.IGNORECASE)
            if match:
                return int(match.group(1))

        match_route = re.search(r"(?:pr|pull|pulls)[/:#](\d+)", context.route_ref, re.IGNORECASE)
        if match_route:
            return int(match_route.group(1))

        return 1

    def _sanitize_refs(self, refs: list[str], token_hint: str | None = None) -> list[str]:
        return [
            sanitize_ref(ref, token_hint=token_hint, extra_tokens=self.token_hints)
            for ref in refs
        ]

    def _sanitize_text(self, text: str, token_hint: str | None = None) -> str:
        return sanitize_text(text, token_hint=token_hint, extra_tokens=self.token_hints)

    def handle(self, context: StageContext) -> StageResult:
        """Execute integration reconciliation stage under strict fail-closed rules."""
        token_hint = self.github_client.token if self.github_client else None
        jk = context.claim.job_key
        ticket_id = jk.ticket_id

        # 1. Lease freshness check
        if context.claim.expires_at:
            try:
                exp_clean = context.claim.expires_at.replace("Z", "+00:00")
                exp_dt = datetime.fromisoformat(exp_clean)
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=UTC)
                if exp_dt < datetime.now(UTC):
                    logger.warning("Integration stage rejected due to stale lease for ticket %s", ticket_id)
                    return StageResult(
                        outcome="failed",
                        cause_code="stale_lease",
                        output_refs=[],
                        evidence_refs=[],
                        actual_cost=0.0,
                    )
            except Exception as exc:
                logger.warning("Invalid lease expires_at '%s' for ticket %s: %s", context.claim.expires_at, ticket_id, exc)
                return StageResult(
                    outcome="failed",
                    cause_code="stale_lease",
                    output_refs=[],
                    evidence_refs=[],
                    actual_cost=0.0,
                )

        # 2. Candidate digest requirement
        candidate_sha = (context.candidate_digest or "").strip().lower()
        if not candidate_sha or len(candidate_sha) < 7:
            logger.error("Missing or invalid candidate_digest for ticket %s", ticket_id)
            return StageResult(
                outcome="failed",
                cause_code="missing_candidate_digest",
                output_refs=[],
                evidence_refs=[],
                actual_cost=0.0,
            )

        # 3. EvidenceReceipt retrieval and strict validation (sem autoapprove)
        receipt: EvidenceReceipt | None = None
        if self.receipt_provider is not None:
            try:
                receipt = self.receipt_provider(ticket_id)
            except Exception as exc:
                logger.error("Error invoking receipt_provider: %s", self._sanitize_text(str(exc), token_hint))

        if receipt is None and self.receipt_map is not None:
            receipt = self.receipt_map.get(ticket_id) or self.receipt_map.get(candidate_sha)

        if receipt is None:
            logger.error("Fail-closed: missing independent review receipt for ticket %s", ticket_id)
            return StageResult(
                outcome="failed",
                cause_code="missing_evidence_receipt",
                output_refs=[],
                evidence_refs=[],
                actual_cost=0.0,
            )

        # 3.1. Verification that reviewer passed
        if receipt.result is not EvidenceResult.PASSED:
            logger.warning("Independent review did not pass for %s: %s", ticket_id, receipt.result)
            return StageResult(
                outcome="failed",
                cause_code="evidence_receipt_not_passed",
                output_refs=[],
                evidence_refs=self._sanitize_refs([receipt.receipt_id], token_hint),
                actual_cost=0.0,
            )

        # 3.2. Candidate digest match against receipt
        receipt_cand = (receipt.candidate_digest or receipt.artifact_hash or "").strip().lower()
        if receipt_cand != candidate_sha:
            logger.error(
                "Evidence mismatch: receipt candidate '%s' != context candidate '%s'",
                receipt_cand,
                candidate_sha,
            )
            return StageResult(
                outcome="failed",
                cause_code="evidence_mismatch",
                output_refs=[],
                evidence_refs=self._sanitize_refs([receipt.receipt_id], token_hint),
                actual_cost=0.0,
            )

        # 3.3. Strict prohibition of self-approval (reviewer != developer/worker)
        if receipt.producer.subject == context.identity:
            logger.error(
                "Self-approval prohibited: reviewer '%s' matches context identity '%s'",
                receipt.producer.subject,
                context.identity,
            )
            return StageResult(
                outcome="failed",
                cause_code="self_approval_prohibited",
                output_refs=[],
                evidence_refs=self._sanitize_refs([receipt.receipt_id], token_hint),
                actual_cost=0.0,
            )

        # 3.4. Role verification: developer cannot act as reviewer
        if receipt.producer.role in ("developer", "integrator"):
            logger.error("Reviewer role '%s' cannot self-certify review", receipt.producer.role)
            return StageResult(
                outcome="failed",
                cause_code="invalid_reviewer_role",
                output_refs=[],
                evidence_refs=self._sanitize_refs([receipt.receipt_id], token_hint),
                actual_cost=0.0,
            )

        # 4. Determine DeliveryRequest
        if self.delivery_request_provider is not None:
            delivery_req = self.delivery_request_provider(context)
            if delivery_req is None:
                return StageResult(
                    outcome="failed",
                    cause_code="delivery_request_provider_returned_none",
                    output_refs=[],
                    evidence_refs=self._sanitize_refs([receipt.receipt_id], token_hint),
                    actual_cost=0.0,
                )
        else:
            pr_num = self._extract_pr_number(context)
            delivery_req = DeliveryRequest(
                task_id=ticket_id,
                repository=self.repository,
                pull_request_number=pr_num,
                base_sha=self.default_base_sha,
                candidate_sha=candidate_sha,
                risk_class=DeliveryRisk.B,
                required_checks=self.required_checks,
                idempotency_key=f"integration:{ticket_id}:{candidate_sha}",
            )

        # 5. Idempotent check: lookup existing queue entry before remote calls
        # Handles scenario: PR was previously enqueued or evaluated, response was lost
        existing_entry = self.reconciler.lookup_by_repo_pr(
            delivery_req.repository, delivery_req.pull_request_number
        )
        if existing_entry is not None:
            if existing_entry.candidate_sha != delivery_req.candidate_sha:
                logger.error(
                    "Idempotency conflict: existing queue entry has candidate SHA '%s', request has '%s'",
                    existing_entry.candidate_sha,
                    delivery_req.candidate_sha,
                )
                return StageResult(
                    outcome="failed",
                    cause_code="idempotency_conflict",
                    output_refs=[],
                    evidence_refs=self._sanitize_refs([receipt.receipt_id], token_hint),
                    actual_cost=0.0,
                )

            logger.info(
                "Idempotent replay: PR %s#%d already reconciled with candidate %s; skipping duplicate enqueue/merge",
                delivery_req.repository,
                delivery_req.pull_request_number,
                delivery_req.candidate_sha,
            )
            op_ref = (
                f"op://github/external_merge/{existing_entry.queue_id}"
                if self.confirm_merged
                else f"op://github/merge_queue/{existing_entry.queue_id}"
            )
            return StageResult(
                outcome="success",
                output_refs=self._sanitize_refs(
                    [
                        f"ref://integration/pr/{delivery_req.repository}/{delivery_req.pull_request_number}",
                        f"ref://integration/queue/{existing_entry.queue_id}",
                        f"ref://candidate/{delivery_req.candidate_sha}",
                    ],
                    token_hint,
                ),
                evidence_refs=self._sanitize_refs(
                    [
                        receipt.receipt_id,
                        f"ref://evidence/integration/{jk.canonical_key()}",
                        f"ref://evidence/replay/{existing_entry.queue_id}",
                    ],
                    token_hint,
                ),
                operation_refs=self._sanitize_refs(
                    [
                        op_ref,
                    ],
                    token_hint,
                ),
                actual_cost=0.0,
            )

        # 6. Reconcile with remote GitHub
        snapshot: PullRequestSnapshot | None = None
        if self.snapshot_provider is not None:
            snapshot = self.snapshot_provider(
                delivery_req.repository, delivery_req.pull_request_number
            )

        reconciler_result: RemoteDeliveryResult = self.reconciler.reconcile_and_evaluate(
            delivery_req,
            snapshot=snapshot,
            confirm_merged=self.confirm_merged,
            remote_main_sha=self.remote_main_sha,
            ancestry_verified=self.ancestry_verified,
        )

        # 7. Evaluate reconciler outcome
        if not reconciler_result.has_remote_ancestry or "ancestry" in reconciler_result.reason:
            logger.warning(
                "Delivery blocked: candidate lacks ancestry on remote main for %s", ticket_id
            )
            return StageResult(
                outcome="failed",
                cause_code=self._sanitize_text(
                    f"delivery_blocked: {reconciler_result.reason}", token_hint
                ),
                output_refs=[],
                evidence_refs=self._sanitize_refs([receipt.receipt_id], token_hint),
                actual_cost=0.0,
            )

        if reconciler_result.status == RemoteDeliveryStatus.DELIVERED:
            queue_id = (
                reconciler_result.queue_entry.queue_id
                if reconciler_result.queue_entry
                else f"mq_{delivery_req.pull_request_number}"
            )
            op_ref = (
                f"op://github/external_merge/{queue_id}"
                if reconciler_result.reason == "external_merge_confirmed"
                else f"op://github/merge_queue/{queue_id}"
            )
            return StageResult(
                outcome="success",
                output_refs=self._sanitize_refs(
                    [
                        f"ref://integration/pr/{delivery_req.repository}/{delivery_req.pull_request_number}",
                        f"ref://integration/queue/{queue_id}",
                        f"ref://candidate/{delivery_req.candidate_sha}",
                    ],
                    token_hint,
                ),
                evidence_refs=self._sanitize_refs(
                    [
                        receipt.receipt_id,
                        f"ref://evidence/integration/{jk.canonical_key()}",
                        f"ref://evidence/github-snapshot/{delivery_req.repository}/{delivery_req.pull_request_number}/{delivery_req.candidate_sha[:8]}",
                    ],
                    token_hint,
                ),
                operation_refs=self._sanitize_refs(
                    [
                        op_ref,
                    ],
                    token_hint,
                ),
                actual_cost=0.0,
            )

        if reconciler_result.status == RemoteDeliveryStatus.WAITING_CHECKS:
            return StageResult(
                outcome="waiting_dependency",
                cause_code=self._sanitize_text(
                    f"waiting_checks: {reconciler_result.reason}", token_hint
                ),
                output_refs=[],
                evidence_refs=self._sanitize_refs([receipt.receipt_id], token_hint),
                actual_cost=0.0,
            )

        if reconciler_result.status == RemoteDeliveryStatus.MANUAL_REVIEW:
            return StageResult(
                outcome="waiting_human",
                cause_code=self._sanitize_text(
                    f"manual_review_required: {reconciler_result.reason}", token_hint
                ),
                output_refs=[],
                evidence_refs=self._sanitize_refs([receipt.receipt_id], token_hint),
                actual_cost=0.0,
            )

        # RemoteDeliveryStatus.BLOCKED or FAILED
        return StageResult(
            outcome="failed",
            cause_code=self._sanitize_text(
                f"delivery_blocked: {reconciler_result.reason}", token_hint
            ),
            output_refs=[],
            evidence_refs=self._sanitize_refs([receipt.receipt_id], token_hint),
            actual_cost=0.0,
        )


__all__ = [
    "IntegrationStageHandler",
    "sanitize_ref",
    "sanitize_text",
]
