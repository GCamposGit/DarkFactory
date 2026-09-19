"""
Pure effective policy resolver for DarkFac autonomous composition (HF-26-01).

Implements deterministic precedence rules for operational actions without
side effects, credentials, network I/O or global mutable state.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class Operation(str, Enum):
    PLAN = "plan"
    IMPLEMENT = "implement"
    VALIDATE = "validate"
    MERGE = "merge"
    DEPLOY_STAGING = "deploy_staging"
    DEPLOY_PRODUCTION = "deploy_production"
    READ_SECRET = "read_secret"
    SPEND = "spend"


def _check_non_empty_str(val: str, field_name: str) -> str:
    if not isinstance(val, str) or not val.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return val


def _check_utc_tz(dt: datetime, field_name: str) -> datetime:
    if not isinstance(dt, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware (UTC)")
    return dt


def _check_finite_non_negative_decimal(d: Decimal, field_name: str) -> Decimal:
    if not isinstance(d, Decimal):
        try:
            d = Decimal(str(d))
        except Exception as exc:
            raise ValueError(f"{field_name} must be a valid Decimal") from exc
    if d.is_nan() or d.is_infinite():
        raise ValueError(f"{field_name} must be a finite Decimal (cannot be NaN or Infinity)")
    if d < Decimal("0"):
        raise ValueError(f"{field_name} cannot be negative")
    return d


class PolicyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str
    operation: Operation
    scope_ref: str
    candidate_digest: str
    policy_version: str
    estimated_cost_usd: Decimal

    @field_validator("project_id", "scope_ref", "candidate_digest", "policy_version")
    @classmethod
    def _validate_strings(cls, v: str, info) -> str:
        return _check_non_empty_str(v, info.field_name)

    @field_validator("estimated_cost_usd")
    @classmethod
    def _validate_cost(cls, v: Decimal) -> Decimal:
        return _check_finite_non_negative_decimal(v, "estimated_cost_usd")


class PolicyGrant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: str
    project_id: str
    operation: Operation
    scope_ref: str
    policy_version: str
    expires_at: datetime

    @field_validator("grant_id", "project_id", "scope_ref", "policy_version")
    @classmethod
    def _validate_strings(cls, v: str, info) -> str:
        return _check_non_empty_str(v, info.field_name)

    @field_validator("expires_at")
    @classmethod
    def _validate_tz(cls, v: datetime) -> datetime:
        return _check_utc_tz(v, "expires_at")


class AcceptanceProof(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str
    candidate_digest: str
    receipt_ref: str
    expires_at: datetime

    @field_validator("project_id", "candidate_digest", "receipt_ref")
    @classmethod
    def _validate_strings(cls, v: str, info) -> str:
        return _check_non_empty_str(v, info.field_name)

    @field_validator("expires_at")
    @classmethod
    def _validate_tz(cls, v: datetime) -> datetime:
        return _check_utc_tz(v, "expires_at")


class PolicyContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str
    policy_version: str
    now: datetime
    paused: bool
    cancelled: bool
    commercial_paid: bool
    grants: tuple[PolicyGrant, ...] = ()
    trusted_grant_ids: tuple[str, ...] = ()
    explicit_denials: tuple[Operation, ...] = ()
    failed_gate_refs: tuple[str, ...] = ()
    remaining_budget_usd: Decimal
    acceptance: AcceptanceProof | None = None
    trusted_receipt_refs: tuple[str, ...] = ()

    @field_validator("project_id", "policy_version")
    @classmethod
    def _validate_strings(cls, v: str, info) -> str:
        return _check_non_empty_str(v, info.field_name)

    @field_validator("now")
    @classmethod
    def _validate_now_tz(cls, v: datetime) -> datetime:
        return _check_utc_tz(v, "now")

    @field_validator("remaining_budget_usd")
    @classmethod
    def _validate_budget(cls, v: Decimal) -> Decimal:
        return _check_finite_non_negative_decimal(v, "remaining_budget_usd")

    @field_validator("trusted_receipt_refs")
    @classmethod
    def _validate_trusted_receipts(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for ref in v:
            _check_non_empty_str(ref, "trusted_receipt_ref")
        if len(v) != len(set(v)):
            raise ValueError("trusted_receipt_refs contains duplicate references")
        return v

    @model_validator(mode="after")
    def _validate_grants_and_trusted(self) -> PolicyContext:
        grant_ids = [g.grant_id for g in self.grants]
        if len(grant_ids) != len(set(grant_ids)):
            raise ValueError("grants contains duplicate grant_id values")

        grant_id_set = set(grant_ids)
        for trusted_id in self.trusted_grant_ids:
            _check_non_empty_str(trusted_id, "trusted_grant_id")
            if trusted_id not in grant_id_set:
                raise ValueError(f"trusted_grant_id '{trusted_id}' does not exist in grants")

        if len(self.trusted_grant_ids) != len(set(self.trusted_grant_ids)):
            raise ValueError("trusted_grant_ids contains duplicate entries")

        for gate in self.failed_gate_refs:
            _check_non_empty_str(gate, "failed_gate_ref")

        return self


class PolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal[
        "allowed",
        "blocked_policy",
        "waiting_dependency",
        "waiting_budget",
        "paused",
        "cancelled",
    ]
    reason_code: str
    matched_grant_id: str | None
    blocking_refs: tuple[str, ...]
    project_id: str
    operation: Operation
    scope_ref: str
    candidate_digest: str
    policy_version: str


def resolve_effective_policy(request: PolicyRequest, context: PolicyContext) -> PolicyDecision:
    """
    Pure, deterministic evaluation of policy requests against context.

    Precedence order:
    1. project_id / policy_version divergence -> blocked_policy / context_mismatch
    2. cancelled -> cancelled / explicit_cancel; paused -> paused / explicit_pause
    3. operation in explicit_denials -> blocked_policy / explicit_deny
    4. failed_gate_refs non-empty -> waiting_dependency / gate_failed (preserving sorted refs)
    5. estimated_cost_usd > remaining_budget_usd -> waiting_budget / ceiling_exceeded
    6. commercial_paid and deploy_production: valid acceptance proof required ->
       waiting_dependency / client_acceptance_required
    7. Select matching grant (exact project, op, scope, version, trusted, not expired).
       If multiple, lowest lexicographical grant_id. If none ->
       blocked_policy / authorization_missing
    8. Allowed -> allowed / authorized (with matched_grant_id)
    """
    # 1. Project & version check
    if (
        request.project_id != context.project_id
        or request.policy_version != context.policy_version
    ):
        return PolicyDecision(
            status="blocked_policy",
            reason_code="context_mismatch",
            matched_grant_id=None,
            blocking_refs=(),
            project_id=request.project_id,
            operation=request.operation,
            scope_ref=request.scope_ref,
            candidate_digest=request.candidate_digest,
            policy_version=request.policy_version,
        )

    # 2. Cancelled / Paused
    if context.cancelled:
        return PolicyDecision(
            status="cancelled",
            reason_code="explicit_cancel",
            matched_grant_id=None,
            blocking_refs=(),
            project_id=request.project_id,
            operation=request.operation,
            scope_ref=request.scope_ref,
            candidate_digest=request.candidate_digest,
            policy_version=request.policy_version,
        )
    if context.paused:
        return PolicyDecision(
            status="paused",
            reason_code="explicit_pause",
            matched_grant_id=None,
            blocking_refs=(),
            project_id=request.project_id,
            operation=request.operation,
            scope_ref=request.scope_ref,
            candidate_digest=request.candidate_digest,
            policy_version=request.policy_version,
        )

    # 3. Explicit denials
    if request.operation in context.explicit_denials:
        return PolicyDecision(
            status="blocked_policy",
            reason_code="explicit_deny",
            matched_grant_id=None,
            blocking_refs=(),
            project_id=request.project_id,
            operation=request.operation,
            scope_ref=request.scope_ref,
            candidate_digest=request.candidate_digest,
            policy_version=request.policy_version,
        )

    # 4. Failed gates (preserving sorted refs)
    if context.failed_gate_refs:
        return PolicyDecision(
            status="waiting_dependency",
            reason_code="gate_failed",
            matched_grant_id=None,
            blocking_refs=tuple(sorted(context.failed_gate_refs)),
            project_id=request.project_id,
            operation=request.operation,
            scope_ref=request.scope_ref,
            candidate_digest=request.candidate_digest,
            policy_version=request.policy_version,
        )

    # 5. Budget ceiling
    if request.estimated_cost_usd > context.remaining_budget_usd:
        return PolicyDecision(
            status="waiting_budget",
            reason_code="ceiling_exceeded",
            matched_grant_id=None,
            blocking_refs=(),
            project_id=request.project_id,
            operation=request.operation,
            scope_ref=request.scope_ref,
            candidate_digest=request.candidate_digest,
            policy_version=request.policy_version,
        )

    # 6. Commercial paid deploy_production check
    if context.commercial_paid and request.operation == Operation.DEPLOY_PRODUCTION:
        acc = context.acceptance
        valid_acceptance = (
            acc is not None
            and acc.project_id == request.project_id
            and acc.candidate_digest == request.candidate_digest
            and acc.receipt_ref in context.trusted_receipt_refs
            and acc.expires_at > context.now
        )
        if not valid_acceptance:
            blocking = (acc.receipt_ref,) if (acc and acc.receipt_ref) else ()
            return PolicyDecision(
                status="waiting_dependency",
                reason_code="client_acceptance_required",
                matched_grant_id=None,
                blocking_refs=blocking,
                project_id=request.project_id,
                operation=request.operation,
                scope_ref=request.scope_ref,
                candidate_digest=request.candidate_digest,
                policy_version=request.policy_version,
            )

    # 7. Grant selection
    trusted_ids = set(context.trusted_grant_ids)
    eligible_grants = [
        g
        for g in context.grants
        if g.project_id == request.project_id
        and g.operation == request.operation
        and g.scope_ref == request.scope_ref
        and g.policy_version == request.policy_version
        and g.grant_id in trusted_ids
        and g.expires_at > context.now
    ]

    if not eligible_grants:
        return PolicyDecision(
            status="blocked_policy",
            reason_code="authorization_missing",
            matched_grant_id=None,
            blocking_refs=(),
            project_id=request.project_id,
            operation=request.operation,
            scope_ref=request.scope_ref,
            candidate_digest=request.candidate_digest,
            policy_version=request.policy_version,
        )

    # Lowest lexicographical grant_id for determinism
    chosen_grant = min(eligible_grants, key=lambda g: g.grant_id)

    # 8. Allowed
    return PolicyDecision(
        status="allowed",
        reason_code="authorized",
        matched_grant_id=chosen_grant.grant_id,
        blocking_refs=(),
        project_id=request.project_id,
        operation=request.operation,
        scope_ref=request.scope_ref,
        candidate_digest=request.candidate_digest,
        policy_version=request.policy_version,
    )
