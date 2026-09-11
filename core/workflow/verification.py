"""Deterministic verification context, gate policy, and trusted receipts for HF-04."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.workflow.contracts import (
    EvidenceResult,
    PlannerTier,
    SanitizedIdentity,
    WorkflowState,
    _looks_like_secret_value,
)


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include an explicit timezone")
    return value.astimezone(UTC)


class ValidationMode(str, Enum):
    DOCUMENTARY = "documentary"
    SIMULATION = "simulation"
    TARGET_ENVIRONMENT = "target_environment"
    NOT_APPLICABLE = "not_applicable"


class _ImmutableVerificationModel(BaseModel):
    """Defensively immutable base model for verification structures."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_assignment=True,
        arbitrary_types_allowed=True,
    )


class PlanApproval(_ImmutableVerificationModel):
    """Supervisor approval of a plan with enabled capabilities."""

    approval_ref: str = Field(..., min_length=1)
    planner_tier: PlannerTier
    plan_digest: str = Field(..., min_length=1)
    approved_by: SanitizedIdentity
    approved_at: datetime
    enabled_capabilities: tuple[str, ...] = Field(default_factory=tuple)

    _validate_time = field_validator("approved_at")(_aware_utc)

    @field_validator("enabled_capabilities", mode="before")
    @classmethod
    def _coerce_capabilities(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, (list, set, frozenset, tuple)):
            return tuple(str(v).strip() for v in value if str(v).strip())
        raise ValueError("enabled_capabilities must be a collection of strings")


class EvidenceReceipt(_ImmutableVerificationModel):
    """Proof produced by an authorized supervisor or test runner, distinct from candidate self-declaration."""

    receipt_id: str = Field(..., min_length=1)
    producer: SanitizedIdentity
    subject: str = Field(..., min_length=1)
    requirement: str = Field(..., min_length=1)
    artifact_hash: str = Field(..., min_length=1)
    result: EvidenceResult
    mode: ValidationMode
    observed_at: datetime
    plan_digest: str | None = None
    candidate_digest: str | None = None
    build_digest: str | None = None
    config_version: str | None = None
    environment_ref: str | None = None
    route: str | None = None
    capabilities: tuple[str, ...] = Field(default_factory=tuple)

    _validate_time = field_validator("observed_at")(_aware_utc)

    @model_validator(mode="before")
    @classmethod
    def _normalize_receipt(cls, data: Any) -> Any:
        if isinstance(data, dict):
            cd = data.get("candidate_digest")
            bd = data.get("build_digest")
            if bd and not cd:
                data["candidate_digest"] = bd
            elif cd and not bd:
                data["build_digest"] = cd
            caps = data.get("capabilities")
            if isinstance(caps, (list, set, frozenset)):
                data["capabilities"] = tuple(str(c).strip() for c in caps if str(c).strip())
        return data

    @field_validator("artifact_hash", "candidate_digest", "build_digest", "plan_digest", "route")
    @classmethod
    def _reject_secrets(cls, value: str | None) -> str | None:
        if value is not None and _looks_like_secret_value(value):
            raise ValueError("hash/digest/route cannot contain embedded secret values")
        return value


class PolicyExemption(_ImmutableVerificationModel):
    """Typed exemption per ticket and version, with authorized origin."""

    exemption_id: str = Field(..., min_length=1)
    ticket_id: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    requirement: str = Field(..., min_length=1)
    authorized_by: SanitizedIdentity
    reason: str = Field(..., min_length=1)
    is_operational: bool = False
    expires_at: datetime | None = None

    _validate_time = field_validator("expires_at")(_aware_utc)

    def is_valid_at(self, now: datetime) -> bool:
        if self.expires_at is None:
            return True
        aware_now = _aware_utc(now)
        assert aware_now is not None
        return aware_now <= self.expires_at


class GatePolicy(_ImmutableVerificationModel):
    """Authoritative gate policy defining requirements and validity per stage."""

    policy_version: str = Field(default="1", min_length=1)
    stage_requirements: dict[WorkflowState, tuple[str, ...]] = Field(default_factory=dict)
    enabled_roles: frozenset[str] = Field(default_factory=frozenset)
    max_age_seconds: dict[str, int] = Field(default_factory=dict)
    default_max_age_seconds: int = Field(default=86400, ge=1)
    exemptions: tuple[PolicyExemption, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _validate_policy(self) -> GatePolicy:
        # Delivery must never have a generic operational exemption in policy
        for ex in self.exemptions:
            if ex.is_operational and ex.requirement in self.stage_requirements.get(WorkflowState.DELIVERED, ()):
                raise ValueError(f"delivery policy cannot permit operational exemption for '{ex.requirement}'")
        return self

    def requirements_for_stage(self, stage: WorkflowState) -> tuple[str, ...]:
        return self.stage_requirements.get(stage, ())

    def is_role_enabled(self, role: str) -> bool:
        return role in self.enabled_roles

    def get_max_age(self, requirement: str) -> int:
        return self.max_age_seconds.get(requirement, self.default_max_age_seconds)

    def find_exemption(self, ticket_id: str, requirement: str, version: str) -> PolicyExemption | None:
        for ex in self.exemptions:
            if ex.ticket_id == ticket_id and ex.requirement == requirement and ex.version == version:
                return ex
        return None


class VerificationContext:
    """Immutable context supplied by the trusted supervisor/gate controller."""

    __slots__ = (
        "_now",
        "_policy_version",
        "_plan_digest",
        "_candidate_digest",
        "_config_version",
        "_expected_environment_ref",
        "_expected_identity",
        "_expected_route",
        "_approvals",
        "_receipts",
        "_exemptions",
        "_policy",
        "_initialized",
    )

    def __init__(
        self,
        *,
        now: datetime,
        policy_version: str = "1",
        plan_digest: str,
        candidate_digest: str,
        config_version: str,
        expected_environment_ref: str,
        expected_identity: SanitizedIdentity,
        expected_route: str,
        approvals: Mapping[str, PlanApproval] | None = None,
        receipts: Mapping[str, EvidenceReceipt] | None = None,
        exemptions: Mapping[str, PolicyExemption] | None = None,
        policy: GatePolicy | None = None,
    ) -> None:
        aware_now = _aware_utc(now)
        if aware_now is None:
            raise ValueError("now timestamp must include an explicit timezone")
        self._now = aware_now

        if not str(policy_version).strip():
            raise ValueError("policy_version cannot be blank")
        self._policy_version = policy_version.strip()

        if not str(plan_digest).strip() or _looks_like_secret_value(plan_digest):
            raise ValueError("plan_digest must be a valid non-secret string")
        self._plan_digest = plan_digest.strip()

        if not str(candidate_digest).strip() or _looks_like_secret_value(candidate_digest):
            raise ValueError("candidate_digest must be a valid non-secret string")
        self._candidate_digest = candidate_digest.strip()

        if not str(config_version).strip() or _looks_like_secret_value(config_version):
            raise ValueError("config_version must be a valid non-secret string")
        self._config_version = config_version.strip()

        if not str(expected_environment_ref).strip():
            raise ValueError("expected_environment_ref cannot be blank")
        self._expected_environment_ref = expected_environment_ref.strip()

        if not isinstance(expected_identity, SanitizedIdentity):
            raise TypeError("expected_identity must be a SanitizedIdentity instance")
        self._expected_identity = expected_identity

        if not str(expected_route).strip():
            raise ValueError("expected_route cannot be blank")
        self._expected_route = expected_route.strip()

        # Defensively copy and wrap in immutable MappingProxyType
        cleaned_approvals: dict[str, PlanApproval] = {}
        if approvals:
            for k, v in approvals.items():
                if not isinstance(v, PlanApproval):
                    raise TypeError(f"approval '{k}' must be a PlanApproval instance")
                if k in cleaned_approvals and cleaned_approvals[k] != v:
                    raise ValueError(f"conflicting approval reference '{k}'")
                cleaned_approvals[k] = v
        self._approvals = MappingProxyType(cleaned_approvals)

        cleaned_receipts: dict[str, EvidenceReceipt] = {}
        seen_requirements: dict[str, EvidenceReceipt] = {}
        if receipts:
            for k, v in receipts.items():
                if not isinstance(v, EvidenceReceipt):
                    raise TypeError(f"receipt '{k}' must be an EvidenceReceipt instance")
                if k in cleaned_receipts and cleaned_receipts[k] != v:
                    raise ValueError(f"conflicting receipt reference '{k}'")
                if v.requirement in seen_requirements:
                    existing = seen_requirements[v.requirement]
                    if existing.result != v.result or existing.artifact_hash != v.artifact_hash:
                        raise ValueError(f"conflicting receipts detected for requirement '{v.requirement}'")
                else:
                    seen_requirements[v.requirement] = v
                cleaned_receipts[k] = v
        self._receipts = MappingProxyType(cleaned_receipts)

        cleaned_exemptions: dict[str, PolicyExemption] = {}
        if exemptions:
            for k, v in exemptions.items():
                if not isinstance(v, PolicyExemption):
                    raise TypeError(f"exemption '{k}' must be a PolicyExemption instance")
                if k in cleaned_exemptions and cleaned_exemptions[k] != v:
                    raise ValueError(f"conflicting exemption reference '{k}'")
                cleaned_exemptions[k] = v
        self._exemptions = MappingProxyType(cleaned_exemptions)

        self._policy = policy
        super().__setattr__("_initialized", True)

    @property
    def now(self) -> datetime:
        return self._now

    @property
    def policy_version(self) -> str:
        return self._policy_version

    @property
    def plan_digest(self) -> str:
        return self._plan_digest

    @property
    def candidate_digest(self) -> str:
        return self._candidate_digest

    @property
    def config_version(self) -> str:
        return self._config_version

    @property
    def expected_environment_ref(self) -> str:
        return self._expected_environment_ref

    @property
    def expected_identity(self) -> SanitizedIdentity:
        return self._expected_identity

    @property
    def expected_route(self) -> str:
        return self._expected_route

    @property
    def approvals(self) -> Mapping[str, PlanApproval]:
        return self._approvals

    @property
    def receipts(self) -> Mapping[str, EvidenceReceipt]:
        return self._receipts

    @property
    def exemptions(self) -> Mapping[str, PolicyExemption]:
        return self._exemptions

    @property
    def policy(self) -> GatePolicy | None:
        return self._policy

    def resolve_approval(self, approval_ref: str) -> PlanApproval | None:
        return self._approvals.get(approval_ref)

    def resolve_receipt(self, requirement_or_ref: str) -> EvidenceReceipt | None:
        if requirement_or_ref in self._receipts:
            return self._receipts[requirement_or_ref]
        for receipt in self._receipts.values():
            if receipt.requirement == requirement_or_ref:
                return receipt
        return None

    def resolve_exemption(self, requirement: str) -> PolicyExemption | None:
        if requirement in self._exemptions:
            return self._exemptions[requirement]
        for ex in self._exemptions.values():
            if ex.requirement == requirement:
                return ex
        return None

    def diagnostic_summary(self) -> dict[str, Any]:
        """Produce safe, diagnostic metadata free of secret credentials."""
        route = self._expected_route
        if _looks_like_secret_value(route):
            route = "[REDACTED]"
        env_ref = self._expected_environment_ref
        if _looks_like_secret_value(env_ref):
            env_ref = "[REDACTED]"

        return {
            "now": self._now.isoformat(),
            "policy_version": self._policy_version,
            "plan_digest": self._plan_digest[:16] + "...",
            "candidate_digest": self._candidate_digest[:16] + "...",
            "config_version": self._config_version,
            "expected_environment_ref": env_ref,
            "expected_identity": {
                "subject": self._expected_identity.subject,
                "role": self._expected_identity.role,
                "host": self._expected_identity.host,
            },
            "expected_route": route,
            "approval_count": len(self._approvals),
            "approval_refs": sorted(self._approvals.keys()),
            "receipt_count": len(self._receipts),
            "receipt_requirements": sorted({r.requirement for r in self._receipts.values()}),
            "exemption_count": len(self._exemptions),
            "has_policy": self._policy is not None,
        }

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_initialized", False):
            raise AttributeError(f"VerificationContext is immutable; cannot set '{name}'")
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if getattr(self, "_initialized", False):
            raise AttributeError(f"VerificationContext is immutable; cannot delete '{name}'")
        super().__delattr__(name)


__all__ = [
    "EvidenceReceipt",
    "GatePolicy",
    "PlanApproval",
    "PolicyExemption",
    "ValidationMode",
    "VerificationContext",
]
