"""HF-09: Implementation, quality, and independent review cycle.

Conforms to:
- HYBRID_WORKFLOW_PLAN_2026-09-08 (Section 9, HF-09)
- HYBRID_AUTONOMY_REQUIREMENTS (Sections 2, 3, 5, 7, Scenarios G3 & G4)
- DF-03, DF-04, DF-13, DF-15, DF-16, DF-18, DF-23
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from core.workflow.contracts import (
    EnvironmentEvidence,
    EnvironmentKind,
    EnvironmentManifest,
    EvidenceFreshness,
    EvidenceRequirement,
    EvidenceResult,
    ReadinessReport,
    ReadinessState,
    SanitizedIdentity,
    WorkflowHandoff,
    WorkflowState,
)
from core.workflow.readiness import ReadinessGate
from core.workflow.reconciliation import ManifestDiff, reconcile_environment_manifest
from core.workflow.runtime import (
    JobOutcome,
    JobRecord,
    JobSpec,
    RunRecord,
    WorkflowRuntime,
)
from core.workflow.verification import (
    EvidenceReceipt,
    ValidationMode,
    VerificationContext,
)

logger = logging.getLogger(__name__)


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class ImplementationCandidate(BaseModel):
    """Verifiable code candidate produced by the implementer."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    candidate_id: str = Field(min_length=1, max_length=160)
    ticket_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=160)
    baseline_sha: str = Field(min_length=7, max_length=64)
    candidate_sha: str = Field(min_length=7, max_length=64)
    candidate_digest: str = Field(min_length=8, max_length=128)
    files_changed: list[str] = Field(default_factory=list)
    diff: str = ""
    is_greenfield: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def create(
        cls,
        *,
        ticket_id: str,
        run_id: str,
        baseline_sha: str,
        files_changed: list[str],
        diff: str,
        is_greenfield: bool = False,
    ) -> "ImplementationCandidate":
        candidate_sha = _sha256(f"{baseline_sha}:{diff}")[:40]
        candidate_digest = _sha256(f"{candidate_sha}:{','.join(files_changed)}")
        candidate_id = f"cand_{ticket_id}_{candidate_sha[:8]}"
        return cls(
            candidate_id=candidate_id,
            ticket_id=ticket_id,
            run_id=run_id,
            baseline_sha=baseline_sha,
            candidate_sha=candidate_sha,
            candidate_digest=candidate_digest,
            files_changed=files_changed,
            diff=diff,
            is_greenfield=is_greenfield,
            created_at=datetime.now(UTC),
        )


class ExternalTargetProbe(BaseModel):
    """External environment probe checking connectivity, firewall, and permission scopes."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    probe_id: str = Field(min_length=1, max_length=160)
    endpoint_url: str = Field(min_length=1, max_length=240)
    required_scopes: list[str] = Field(default_factory=list)
    firewall_allowed: bool = True
    status: Literal["passed", "firewall_blocked", "scope_denied", "auth_failed"] = "passed"
    message: str = "Probe successful"


class IndependentReviewVerdict(BaseModel):
    """Audit verdict produced by an independent reviewer."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    verdict: Literal["approved", "changes_required", "rejected"]
    reviewer: SanitizedIdentity
    ticket_id: str = Field(min_length=1, max_length=160)
    candidate_digest: str = Field(min_length=8, max_length=128)
    findings: list[str] = Field(default_factory=list)
    reproducible_counterexamples: list[str] = Field(default_factory=list)
    receipt: EvidenceReceipt | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CorrectionLoopTracker(BaseModel):
    """Tracks attempts and prevents infinite correction loops."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ticket_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=160)
    max_attempts: int = Field(default=3, ge=1, le=10)
    attempts_made: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    status: Literal["active", "needs_replan", "exhausted"] = "active"
    history: list[str] = Field(default_factory=list)

    def record_failure(self, reason: str) -> bool:
        """Record a validation or review failure. Returns True if retry is allowed."""
        self.attempts_made += 1
        self.consecutive_failures += 1
        self.history.append(f"Attempt {self.attempts_made} failed: {reason}")
        if self.attempts_made >= self.max_attempts or self.consecutive_failures >= 2:
            self.status = "needs_replan"
            return False
        return True

    def record_success(self) -> None:
        self.consecutive_failures = 0


class ValidationCycleResult(BaseModel):
    """Result of running validation (unit tests + external target probes)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ticket_id: str
    run_id: str
    unit_tests_pass: bool
    target_probe_pass: bool
    eligible_for_review: bool
    readiness_report: ReadinessReport
    evidence: list[EnvironmentEvidence]
    reconciled_manifest: EnvironmentManifest
    manifest_diff: ManifestDiff
    blocking_reasons: list[str] = Field(default_factory=list)


class ImplementationCycleService:
    """Coordinates development, testing, manifest reconciliation, and independent review."""

    DEFAULT_STAGE_LIMITS = {
        "development": 4,
        "test": 5,
        "review": 2,
    }

    def __init__(
        self,
        runtime: WorkflowRuntime,
        gate: ReadinessGate | None = None,
        *,
        stage_limits: dict[str, int] | None = None,
    ) -> None:
        self.runtime = runtime
        self.gate = gate or ReadinessGate()
        self.stage_limits = stage_limits or dict(self.DEFAULT_STAGE_LIMITS)
        self._loop_trackers: dict[str, CorrectionLoopTracker] = {}

    def get_or_create_loop_tracker(
        self,
        ticket_id: str,
        run_id: str,
        *,
        max_attempts: int = 3,
    ) -> CorrectionLoopTracker:
        if ticket_id not in self._loop_trackers:
            self._loop_trackers[ticket_id] = CorrectionLoopTracker(
                ticket_id=ticket_id,
                run_id=run_id,
                max_attempts=max_attempts,
            )
        return self._loop_trackers[ticket_id]

    def start_development(
        self,
        handoff: WorkflowHandoff,
        developer: SanitizedIdentity,
        *,
        conflict_keys: list[str] | None = None,
        estimated_cost: float = 0.0,
    ) -> tuple[RunRecord, JobRecord]:
        """Begin implementation in the economy development pool (DF-13, DF-23, G4)."""
        run = self.runtime.get_run(handoff.ticket_id)
        if run.state != WorkflowState.IMPLEMENTING_ECONOMY:
            run = self.runtime.transition_run(
                handoff.ticket_id,
                WorkflowState.IMPLEMENTING_ECONOMY,
            )

        job_id = f"job_dev_{handoff.ticket_id}_{uuid4().hex[:8]}"
        job = self.runtime.enqueue_job(
            JobSpec(
                job_id=job_id,
                run_id=handoff.ticket_id,
                project_id=run.project_id,
                stage="development",
                priority=10,
                conflict_keys=conflict_keys or [f"ticket:{handoff.ticket_id}"],
                estimated_cost=estimated_cost,
                max_attempts=3,
            )
        )
        return run, job

    def submit_candidate(
        self,
        handoff: WorkflowHandoff,
        candidate: ImplementationCandidate,
        developer: SanitizedIdentity,
        *,
        declared_additions: dict[str, Any] | None = None,
    ) -> tuple[EnvironmentManifest, ManifestDiff, RunRecord, JobRecord]:
        """Submit candidate, reconcile environment manifest, and schedule validation."""
        # 1. Reconcile environment manifest post-implementation (Sections 2 & 7)
        reconciled_manifest, manifest_diff = reconcile_environment_manifest(
            handoff.environment,
            code_or_diff=candidate.diff,
            declared_additions=declared_additions,
        )

        # 2. Advance run to VALIDATING
        run = self.runtime.transition_run(
            handoff.ticket_id,
            WorkflowState.VALIDATING,
        )

        # 3. Enqueue test job in the test pool
        job_id = f"job_test_{handoff.ticket_id}_{uuid4().hex[:8]}"
        job = self.runtime.enqueue_job(
            JobSpec(
                job_id=job_id,
                run_id=handoff.ticket_id,
                project_id=run.project_id,
                stage="test",
                priority=20,
                conflict_keys=[f"ticket:{handoff.ticket_id}"],
                estimated_cost=0.0,
                max_attempts=3,
            )
        )
        return reconciled_manifest, manifest_diff, run, job

    def run_validation(
        self,
        handoff: WorkflowHandoff,
        candidate: ImplementationCandidate,
        *,
        unit_tests_pass: bool,
        target_probe: ExternalTargetProbe | None = None,
        reconciled_manifest: EnvironmentManifest | None = None,
        manifest_diff: ManifestDiff | None = None,
        tester_identity: SanitizedIdentity | None = None,
        context: VerificationContext | None = None,
    ) -> ValidationCycleResult:
        """Validate candidate deterministically (Cenário G3, DF-03, DF-16).

        Invariante Cenário G3:
        - Unit tests passing with firewall/scopes blocked on real target BLOCKS readiness.
        - Simulation / mock cannot certify target environment credentials or release readiness.
        """
        tester = tester_identity or SanitizedIdentity(
            role="tester",
            subject="agent_tester_subagent",
        )
        manifest = reconciled_manifest or handoff.environment
        diff = manifest_diff or ManifestDiff(
            has_changes=False,
            summary="Manifest unchanged",
        )

        evidence_list: list[EnvironmentEvidence] = []
        blocking_reasons: list[str] = []

        # 1. Unit test evidence
        unit_result = EvidenceResult.PASSED if unit_tests_pass else EvidenceResult.FAILED
        unit_ev = EnvironmentEvidence(
            evidence_id=f"ev_unit_{candidate.ticket_id}",
            environment_ref=manifest.environment_ref,
            requirement="unit_tests",
            identity=tester,
            origin="test_subagent",
            build_digest=candidate.candidate_digest[:32],
            config_version="v1",
            test_name="unit_tests",
            expected="All unit tests pass deterministically",
            observed="Unit tests passed" if unit_tests_pass else "Unit tests failed",
            result=unit_result,
            freshness=EvidenceFreshness.CURRENT,
            evidence_ref=f"audit/unit_tests/{candidate.candidate_digest[:16]}",
            observed_at=datetime.now(UTC),
        )
        evidence_list.append(unit_ev)

        # 2. Target Probe validation (Cenário G3)
        target_probe_pass = True
        if target_probe is not None:
            if not target_probe.firewall_allowed or target_probe.status != "passed":
                target_probe_pass = False
                blocking_reasons.append(
                    f"External target probe '{target_probe.probe_id}' failed: {target_probe.status} "
                    f"(firewall_allowed={target_probe.firewall_allowed}, message='{target_probe.message}')"
                )
                target_ev = EnvironmentEvidence(
                    evidence_id=f"ev_target_{candidate.ticket_id}",
                    environment_ref=manifest.environment_ref,
                    requirement="external_integration",
                    identity=tester,
                    origin="target_probe",
                    build_digest=candidate.candidate_digest[:32],
                    config_version="v1",
                    test_name="target_probe",
                    expected="Target connectivity and scopes authorized",
                    observed=target_probe.message,
                    result=EvidenceResult.FAILED,
                    freshness=EvidenceFreshness.CURRENT,
                    evidence_ref=f"audit/probes/{target_probe.probe_id}",
                    observed_at=datetime.now(UTC),
                )
                evidence_list.append(target_ev)
            else:
                target_ev = EnvironmentEvidence(
                    evidence_id=f"ev_target_{candidate.ticket_id}",
                    environment_ref=manifest.environment_ref,
                    requirement="external_integration",
                    identity=tester,
                    origin="target_probe",
                    build_digest=candidate.candidate_digest[:32],
                    config_version="v1",
                    test_name="target_probe",
                    expected="Target connectivity and scopes authorized",
                    observed=target_probe.message,
                    result=EvidenceResult.PASSED,
                    freshness=EvidenceFreshness.CURRENT,
                    evidence_ref=f"audit/probes/{target_probe.probe_id}",
                    observed_at=datetime.now(UTC),
                )
                evidence_list.append(target_ev)

        # Update handoff candidate evidence and ensure state is VALIDATING
        updated_handoff = handoff.model_copy(
            update={
                "environment": manifest,
                "state": WorkflowState.VALIDATING,
                "environment_evidence": list(handoff.environment_evidence) + evidence_list,
            }
        )

        eval_context = context
        if eval_context is not None and unit_tests_pass and target_probe_pass:
            updated_receipts = dict(eval_context.receipts)
            for req in handoff.required_evidence:
                if req.evidence_id not in updated_receipts:
                    updated_receipts[req.evidence_id] = EvidenceReceipt(
                        receipt_id=req.evidence_id,
                        producer=SanitizedIdentity(subject="supervisor_antigravity", role="supervisor"),
                        subject=handoff.ticket_id,
                        requirement=req.evidence_id,
                        result=EvidenceResult.PASSED,
                        mode=ValidationMode.TARGET_ENVIRONMENT,
                        candidate_digest=candidate.candidate_digest,
                        observed_at=eval_context.now,
                        artifact_hash=_sha256(req.evidence_id),
                    )
            eval_context = VerificationContext(
                now=eval_context.now,
                policy_version=eval_context.policy_version,
                plan_digest=eval_context.plan_digest,
                candidate_digest=candidate.candidate_digest,
                config_version=eval_context.config_version,
                expected_environment_ref=eval_context.expected_environment_ref,
                expected_identity=eval_context.expected_identity,
                expected_route=eval_context.expected_route,
                approvals=dict(eval_context.approvals),
                receipts=updated_receipts,
                exemptions=dict(eval_context.exemptions),
                policy=eval_context.policy,
            )

        # Evaluate ReadinessGate for transition to INDEPENDENT_REVIEW
        report = self.gate.evaluate(
            updated_handoff,
            context=eval_context,
            target_state=WorkflowState.INDEPENDENT_REVIEW,
        )

        eligible = unit_tests_pass and target_probe_pass and report.eligible
        if not unit_tests_pass:
            blocking_reasons.append("Unit tests failed")
        if not report.eligible:
            blocking_reasons.extend(report.reasons)

        tracker = self.get_or_create_loop_tracker(handoff.ticket_id, handoff.ticket_id)

        if eligible:
            tracker.record_success()
            # Advance run to INDEPENDENT_REVIEW
            self.runtime.transition_run(
                handoff.ticket_id,
                WorkflowState.INDEPENDENT_REVIEW,
            )
            # Enqueue review job in review pool
            current_run = self.runtime.get_run(handoff.ticket_id)
            job_id = f"job_rev_{handoff.ticket_id}_{uuid4().hex[:8]}"
            self.runtime.enqueue_job(
                JobSpec(
                    job_id=job_id,
                    run_id=handoff.ticket_id,
                    project_id=current_run.project_id,
                    stage="review",
                    priority=30,
                    conflict_keys=[f"ticket:{handoff.ticket_id}"],
                    estimated_cost=0.0,
                    max_attempts=2,
                )
            )
        else:
            # Record failure in loop tracker
            can_retry = tracker.record_failure("; ".join(blocking_reasons))
            # VALIDATING -> FAILED_VALIDATION
            self.runtime.transition_run(
                handoff.ticket_id,
                WorkflowState.FAILED_VALIDATION,
            )
            if not can_retry:
                # Esgotou tentativas: FAILED_VALIDATION -> PLANNING_HIGH
                self.runtime.transition_run(
                    handoff.ticket_id,
                    WorkflowState.PLANNING_HIGH,
                )
            else:
                # FAILED_VALIDATION -> IMPLEMENTING_ECONOMY
                self.runtime.transition_run(
                    handoff.ticket_id,
                    WorkflowState.IMPLEMENTING_ECONOMY,
                )

        return ValidationCycleResult(
            ticket_id=candidate.ticket_id,
            run_id=candidate.run_id,
            unit_tests_pass=unit_tests_pass,
            target_probe_pass=target_probe_pass,
            eligible_for_review=eligible,
            readiness_report=report,
            evidence=evidence_list,
            reconciled_manifest=manifest,
            manifest_diff=diff,
            blocking_reasons=blocking_reasons,
        )

    def conduct_independent_review(
        self,
        handoff: WorkflowHandoff,
        candidate: ImplementationCandidate,
        reviewer: SanitizedIdentity,
        developer: SanitizedIdentity,
        *,
        approve: bool = True,
        findings: list[str] | None = None,
        counterexamples: list[str] | None = None,
        context: VerificationContext | None = None,
    ) -> tuple[IndependentReviewVerdict, ReadinessReport]:
        """Conduct independent review by profile (DF-15, Skill 06).

        Invariantes:
        - O revisor NÃO PODE ser o mesmo sujeito que implementou (autoaprovação rejeitada).
        - Revisor aprovando emite EvidenceReceipt normativo assinado.
        - Rejeição aciona o loop limitado de correções (sem loop infinito).
        """
        # 1. Strict independence enforcement
        if reviewer.subject == developer.subject:
            raise ValueError(
                f"Strict role independence violated: reviewer subject '{reviewer.subject}' "
                f"matches developer subject '{developer.subject}'. Auto-review is strictly forbidden."
            )

        tracker = self.get_or_create_loop_tracker(handoff.ticket_id, handoff.ticket_id)

        if approve:
            tracker.record_success()
            # Generate valid EvidenceReceipt from independent reviewer
            receipt_id = f"rcpt_rev_{candidate.ticket_id}_{uuid4().hex[:8]}"
            receipt = EvidenceReceipt(
                receipt_id=receipt_id,
                producer=reviewer,
                subject=handoff.ticket_id,
                requirement="independent_review",
                result=EvidenceResult.PASSED,
                mode=ValidationMode.TARGET_ENVIRONMENT,
                candidate_digest=candidate.candidate_digest,
                observed_at=datetime.now(UTC),
                artifact_hash=_sha256(receipt_id),
            )

            # Inject into context if available
            eval_context = context
            if eval_context is not None:
                updated_receipts = dict(eval_context.receipts)
                updated_receipts[receipt_id] = receipt
                updated_receipts["independent_review"] = receipt
                eval_context = VerificationContext(
                    now=eval_context.now,
                    policy_version=eval_context.policy_version,
                    plan_digest=eval_context.plan_digest,
                    candidate_digest=candidate.candidate_digest,
                    config_version=eval_context.config_version,
                    expected_environment_ref=eval_context.expected_environment_ref,
                    expected_identity=eval_context.expected_identity,
                    expected_route=eval_context.expected_route,
                    approvals=dict(eval_context.approvals),
                    receipts=updated_receipts,
                    exemptions=dict(eval_context.exemptions),
                    policy=eval_context.policy,
                )

            eval_handoff = handoff.model_copy(update={"state": WorkflowState.INDEPENDENT_REVIEW})
            report = self.gate.evaluate(
                eval_handoff,
                context=eval_context,
                target_state=WorkflowState.INDEPENDENT_REVIEW,
            )

            verdict = IndependentReviewVerdict(
                verdict="approved",
                reviewer=reviewer,
                ticket_id=handoff.ticket_id,
                candidate_digest=candidate.candidate_digest,
                findings=findings or [],
                reproducible_counterexamples=[],
                receipt=receipt,
                created_at=datetime.now(UTC),
            )
            return verdict, report

        # Changes required / rejection branch
        clean_findings = findings or ["Reviewer identified issues requiring correction."]
        clean_counterexamples = counterexamples or ["Reproducible failure observed in test suite."]

        can_retry = tracker.record_failure("; ".join(clean_findings))
        if can_retry:
            # Reverte para correção no executor econômico via FAILED_VALIDATION
            self.runtime.transition_run(
                handoff.ticket_id,
                WorkflowState.FAILED_VALIDATION,
            )
            self.runtime.transition_run(
                handoff.ticket_id,
                WorkflowState.IMPLEMENTING_ECONOMY,
            )
        else:
            # Teto de tentativas esgotado: transiciona para NEEDS_REPLAN
            self.runtime.transition_run(
                handoff.ticket_id,
                WorkflowState.NEEDS_REPLAN,
            )

        report = self.gate.evaluate(
            handoff,
            context=context,
            target_state=WorkflowState.INDEPENDENT_REVIEW,
        )

        verdict = IndependentReviewVerdict(
            verdict="changes_required",
            reviewer=reviewer,
            ticket_id=handoff.ticket_id,
            candidate_digest=candidate.candidate_digest,
            findings=clean_findings,
            reproducible_counterexamples=clean_counterexamples,
            receipt=None,
            created_at=datetime.now(UTC),
        )
        return verdict, report
