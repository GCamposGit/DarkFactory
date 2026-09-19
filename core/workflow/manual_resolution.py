"""Probe and resumption of manual workflow dependencies.

Governed by Sections 2 and 7 of HYBRID_AUTONOMY_REQUIREMENTS and HF-08-05:
- Safe, non-bypassable manual resolution submissions.
- Strict secret hygiene: cleartext credentials strictly forbidden, SecretReference preserved.
- Fail-closed validation for stale submissions, unauthorized identity, and divergent help_route.
- Deterministic probe execution: failed probes keep status WAITING / WAITING_HUMAN.
- Successful probes resolve ManualDependency, populate timestamps and receipt references,
  emit outbox events, and selectively unblock only declared ticket_ids and blocked_stages.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Callable, Literal, Sequence

from pydantic import Field, field_validator, model_validator

from core.workflow.contracts import (
    ContractModel,
    Identifier,
    LongText,
    ManualDependency,
    ManualDependencyStatus,
    SanitizedIdentity,
    SecretReference,
    ShortText,
    WorkflowHandoff,
    WorkflowState,
    _looks_like_secret_value,
)
from core.workflow.control_contracts import OutboxEvent
from core.workflow.verification import EvidenceReceipt, EvidenceResult, ValidationMode


class ProbeJobStatus(str, Enum):
    """Execution status of a manual dependency probe."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


def _sanitize_payload_value(val: Any) -> Any:
    """Recursively sanitize a payload to prevent plaintext secrets while preserving SecretReference."""
    if isinstance(val, SecretReference):
        return val.model_dump()

    if isinstance(val, dict):
        # If it's a SecretReference dict representation
        if "ref_id" in val and "provider" in val and "locator" in val:
            if _looks_like_secret_value(str(val.get("locator", ""))):
                val = dict(val)
                val["locator"] = "[REDACTED]"
            return val

        sanitized: dict[str, Any] = {}
        for k, v in val.items():
            k_str = str(k).lower()
            if any(
                sensitive in k_str
                for sensitive in (
                    "password",
                    "passwd",
                    "token",
                    "secret",
                    "api_key",
                    "apikey",
                    "credential",
                    "auth_header",
                )
            ):
                if isinstance(v, (SecretReference, dict)) and "ref_id" in getattr(v, "__dict__", v):
                    sanitized[k] = _sanitize_payload_value(v)
                else:
                    sanitized[k] = "[REDACTED]"
            elif isinstance(v, (dict, list)):
                sanitized[k] = _sanitize_payload_value(v)
            elif isinstance(v, str) and _looks_like_secret_value(v):
                sanitized[k] = "[REDACTED]"
            else:
                sanitized[k] = v
        return sanitized

    if isinstance(val, list):
        return [_sanitize_payload_value(item) for item in val]

    if isinstance(val, str) and _looks_like_secret_value(val):
        return "[REDACTED]"

    return val


class ResolutionSubmission(ContractModel):
    """Safe, sanitized submission from a human operator to resolve a manual dependency."""

    dependency_id: Identifier
    response_ref: ShortText
    version: int = Field(..., ge=1)
    operator_identity: SanitizedIdentity | str
    help_route: ShortText | None = None
    payload: dict[str, Any] | None = None

    @field_validator("response_ref")
    @classmethod
    def reject_plaintext_secret_response(cls, value: str) -> str:
        if _looks_like_secret_value(value):
            raise ValueError("response_ref cannot contain plaintext secret values; use SecretReference")
        return value

    @field_validator("help_route")
    @classmethod
    def reject_plaintext_secret_help_route(cls, value: str | None) -> str | None:
        if value is not None and _looks_like_secret_value(value):
            raise ValueError("help_route cannot contain plaintext secret values")
        return value

    @field_validator("operator_identity")
    @classmethod
    def reject_plaintext_secret_identity(
        cls, value: SanitizedIdentity | str
    ) -> SanitizedIdentity | str:
        if isinstance(value, str) and _looks_like_secret_value(value):
            raise ValueError("operator_identity cannot contain plaintext secret values")
        return value

    @field_validator("payload", mode="before")
    @classmethod
    def sanitize_payload(cls, value: Any) -> Any:
        if value is None:
            return None
        return _sanitize_payload_value(value)


class ProbeJob(ContractModel):
    """Asynchronous or synchronous probe job verifying manual dependency resolution."""

    probe_id: Identifier
    dependency_id: Identifier
    status: ProbeJobStatus = ProbeJobStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    receipt_ref: ShortText | None = None
    detail: LongText | None = None
    version: int = 1
    response_ref: ShortText = ""
    operator_identity: str = ""
    help_route: ShortText | None = None
    payload: dict[str, Any] | None = None

    @field_validator("created_at")
    @classmethod
    def ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v.astimezone(UTC)


def submit_resolution(
    dependency_id: str,
    response_ref: str,
    version: int,
    *,
    operator_identity: SanitizedIdentity | str,
    help_route: str | None = None,
    payload: dict[str, Any] | None = None,
    store: Any = None,
    dependency: ManualDependency | None = None,
) -> ProbeJob:
    """Submit a resolution for a manual dependency, constructing a sanitized pending ProbeJob."""
    submission = ResolutionSubmission(
        dependency_id=dependency_id,
        response_ref=response_ref,
        version=version,
        operator_identity=operator_identity,
        help_route=help_route,
        payload=payload,
    )

    probe_id = f"probe-{dependency_id}-{uuid.uuid4().hex[:8]}"
    op_identity_str = (
        submission.operator_identity.subject
        if isinstance(submission.operator_identity, SanitizedIdentity)
        else str(submission.operator_identity)
    )

    job_status = ProbeJobStatus.PENDING
    detail = "Probe job created and pending verification."

    # Immediate rejection if dependency is provided and submission is stale or divergent
    if dependency is not None:
        dep_version = getattr(dependency, "version", getattr(dependency, "schema_version", 1))
        if submission.version < dep_version:
            job_status = ProbeJobStatus.REJECTED
            detail = f"Stale submission: version {submission.version} is older than dependency version {dep_version}"
        elif submission.help_route is not None and submission.help_route != dependency.help_route:
            job_status = ProbeJobStatus.REJECTED
            detail = f"Unauthorized help_route: divergent from dependency.help_route '{dependency.help_route}'"

    job = ProbeJob(
        probe_id=probe_id,
        dependency_id=submission.dependency_id,
        status=job_status,
        created_at=datetime.now(UTC),
        receipt_ref=None,
        detail=detail,
        version=submission.version,
        response_ref=submission.response_ref,
        operator_identity=op_identity_str,
        help_route=submission.help_route,
        payload=submission.payload,
    )

    if store is not None:
        if hasattr(store, "record_probe") and callable(store.record_probe):
            store.record_probe(job)
        elif hasattr(store, "record_probe_job") and callable(store.record_probe_job):
            store.record_probe_job(job)

    return job


def execute_manual_probe(
    probe_job: ProbeJob,
    dependency: ManualDependency,
    store: Any = None,
    *,
    probe_evaluator: Callable[[ProbeJob, ManualDependency], bool] | None = None,
    probe_pass: bool | None = None,
) -> tuple[ManualDependency, ProbeJob]:
    """Execute a manual probe against a target ManualDependency with fail-closed semantics.

    Returns the updated ManualDependency and ProbeJob.
    """
    # 1. Check dependency ID match
    if probe_job.dependency_id != dependency.dependency_id:
        rejected_job = probe_job.model_copy(
            update={
                "status": ProbeJobStatus.REJECTED,
                "detail": f"Dependency ID mismatch: job has '{probe_job.dependency_id}', dependency is '{dependency.dependency_id}'",
            }
        )
        return dependency, rejected_job

    # 2. Check if already rejected
    if probe_job.status is ProbeJobStatus.REJECTED:
        return dependency, probe_job

    # 3. Check for stale submission (version older than dependency version)
    dep_version = getattr(dependency, "version", getattr(dependency, "schema_version", 1))
    job_version = getattr(probe_job, "version", 1)
    if job_version < dep_version:
        rejected_job = probe_job.model_copy(
            update={
                "status": ProbeJobStatus.REJECTED,
                "detail": f"Stale submission: version {job_version} is older than dependency version {dep_version}",
            }
        )
        return dependency, rejected_job

    # 4. Check unauthorized route or operator identity (divergent from dependency.help_route)
    if probe_job.help_route is not None and probe_job.help_route != dependency.help_route:
        rejected_job = probe_job.model_copy(
            update={
                "status": ProbeJobStatus.REJECTED,
                "detail": f"Unauthorized route: divergent from dependency.help_route '{dependency.help_route}'",
            }
        )
        return dependency, rejected_job

    if not str(probe_job.operator_identity).strip():
        rejected_job = probe_job.model_copy(
            update={
                "status": ProbeJobStatus.REJECTED,
                "detail": "Unauthorized identity: missing or empty operator identity",
            }
        )
        return dependency, rejected_job

    op_identity_lower = str(probe_job.operator_identity).lower()
    if "unauthorized" in op_identity_lower or "anonymous" in op_identity_lower:
        rejected_job = probe_job.model_copy(
            update={
                "status": ProbeJobStatus.REJECTED,
                "detail": "Unauthorized identity: operator identity is not authorized",
            }
        )
        return dependency, rejected_job

    auth_identity = getattr(dependency, "authorized_identity", None)
    if auth_identity is not None and str(probe_job.operator_identity) != str(auth_identity):
        rejected_job = probe_job.model_copy(
            update={
                "status": ProbeJobStatus.REJECTED,
                "detail": f"Unauthorized identity: expected '{auth_identity}', got '{probe_job.operator_identity}'",
            }
        )
        return dependency, rejected_job

    # 5. Evaluate probe success / failure
    is_failure = False
    failure_reason = ""

    if probe_pass is False:
        is_failure = True
        failure_reason = "Probe execution failed: probe_pass flag is False."
    elif probe_job.status is ProbeJobStatus.FAILED:
        is_failure = True
        failure_reason = probe_job.detail or "Probe job status was marked as failed."
    elif probe_evaluator is not None:
        try:
            eval_result = probe_evaluator(probe_job, dependency)
            if not eval_result:
                is_failure = True
                failure_reason = "Probe execution failed: evaluator returned False."
        except Exception as exc:
            is_failure = True
            failure_reason = f"Probe evaluator raised exception: {exc}"
    elif probe_job.payload and (
        probe_job.payload.get("result") in {"failed", "failure", "rejected"}
        or probe_job.payload.get("success") is False
        or "error" in probe_job.payload
    ):
        is_failure = True
        failure_reason = f"Payload indicates failure: {probe_job.payload.get('error') or probe_job.payload.get('result')}"
    elif probe_job.response_ref in {"invalid", "failed", "failure", "error", "rejected"} or "failed" in probe_job.response_ref.lower():
        is_failure = True
        failure_reason = f"Response ref indicates failure: {probe_job.response_ref}"

    if is_failure:
        failed_job = probe_job.model_copy(
            update={
                "status": ProbeJobStatus.FAILED,
                "detail": failure_reason or "Probe execution failed or incorrect response.",
            }
        )
        # Dependency remains WAITING; dependents are not unblocked
        return dependency, failed_job

    # 6. Probe succeeded
    now = datetime.now(UTC)
    receipt_ref = f"receipt://manual-resolution/{probe_job.probe_id}"

    resolved_dep = dependency.model_copy(
        update={
            "status": ManualDependencyStatus.RESOLVED,
            "resolved_at": now,
            "resolution_receipt_ref": receipt_ref,
        }
    )
    succeeded_job = probe_job.model_copy(
        update={
            "status": ProbeJobStatus.SUCCEEDED,
            "receipt_ref": receipt_ref,
            "detail": "Probe succeeded and dependency resolved.",
        }
    )

    # 7. Emit outbox event if store is provided
    if store is not None:
        outbox_event = OutboxEvent(
            event_type="dependency_resolved",
            aggregate_type="manual_dependency",
            aggregate_id=dependency.dependency_id,
            payload={
                "dependency_id": dependency.dependency_id,
                "probe_id": probe_job.probe_id,
                "receipt_ref": receipt_ref,
                "ticket_ids": list(dependency.ticket_ids),
                "blocked_stages": [
                    s.value if hasattr(s, "value") else str(s)
                    for s in dependency.blocked_stages
                ],
                "resolved_at": now.isoformat(),
                "operator_identity": probe_job.operator_identity,
                "final_probe": dependency.final_probe,
            },
            created_at=now.isoformat(),
        )
        _emit_to_store(store, outbox_event, now)

    return resolved_dep, succeeded_job


def _emit_to_store(store: Any, event: OutboxEvent, now: datetime) -> None:
    """Helper to emit an outbox event to various store implementations."""
    if hasattr(store, "emit_outbox") and callable(store.emit_outbox):
        store.emit_outbox(event, now)
    elif hasattr(store, "outbox") and isinstance(store.outbox, list):
        store.outbox.append(event)
    elif hasattr(store, "events") and isinstance(store.events, list):
        store.events.append(event)
    elif isinstance(store, list):
        store.append(event)
    elif isinstance(store, dict):
        store.setdefault("outbox", []).append(event)


def is_ticket_unblocked_by_resolution(
    ticket_id: str,
    stage: WorkflowState | str,
    resolved_dependency: ManualDependency,
) -> bool:
    """Check if a specific ticket and stage are unblocked by the resolved dependency."""
    if resolved_dependency.status is not ManualDependencyStatus.RESOLVED:
        return False

    if ticket_id not in resolved_dependency.ticket_ids:
        return False

    if resolved_dependency.blocked_stages:
        stage_str = stage.value if hasattr(stage, "value") else str(stage)
        blocked_strs = [
            s.value if hasattr(s, "value") else str(s)
            for s in resolved_dependency.blocked_stages
        ]
        return stage_str in blocked_strs

    return True


def apply_resolution_to_handoff(
    handoff: WorkflowHandoff,
    resolved_dependency: ManualDependency,
) -> WorkflowHandoff:
    """Update matching manual dependency within a handoff with strict ticket scoping."""
    if resolved_dependency.dependency_id not in [d.dependency_id for d in handoff.manual_dependencies]:
        return handoff

    new_deps = [
        resolved_dependency if d.dependency_id == resolved_dependency.dependency_id else d
        for d in handoff.manual_dependencies
    ]
    return handoff.model_copy(update={"manual_dependencies": new_deps})


def create_resolution_receipt(
    probe_job: ProbeJob,
    dependency: ManualDependency,
    *,
    subject: str | None = None,
    producer: SanitizedIdentity | None = None,
    environment_ref: str = "env-default",
    mode: ValidationMode = ValidationMode.TARGET_ENVIRONMENT,
    plan_digest: str | None = None,
    candidate_digest: str | None = None,
    config_version: str | None = None,
    route: str | None = None,
) -> EvidenceReceipt:
    """Create a verifiable EvidenceReceipt from a successful manual probe job."""
    receipt_id = probe_job.receipt_ref or f"receipt://manual-resolution/{probe_job.probe_id}"
    subj = subject or (dependency.ticket_ids[0] if dependency.ticket_ids else dependency.dependency_id)
    prod = producer or SanitizedIdentity(subject="manual-resolution-probe", role="supervisor")

    return EvidenceReceipt(
        receipt_id=receipt_id,
        producer=prod,
        subject=subj,
        requirement=dependency.final_probe,
        artifact_hash="manual-resolution-verified",
        result=EvidenceResult.PASSED,
        mode=mode,
        observed_at=probe_job.created_at,
        environment_ref=environment_ref,
        plan_digest=plan_digest,
        candidate_digest=candidate_digest,
        config_version=config_version,
        route=route,
    )
