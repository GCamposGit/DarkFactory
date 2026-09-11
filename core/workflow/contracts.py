"""Strict, serializable contracts for the HF-04 hybrid workflow boundary."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8192)]


class ContractModel(BaseModel):
    """Shared strict configuration for public workflow contracts."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class WorkflowState(str, Enum):
    NEEDS_SPEC = "needs_spec"
    PLANNING_HIGH = "planning_high"
    READY_FOR_HANDOFF = "ready_for_handoff"
    IMPLEMENTING_ECONOMY = "implementing_economy"
    VALIDATING = "validating"
    INDEPENDENT_REVIEW = "independent_review"
    DELIVERED = "delivered"
    WAITING_BUDGET = "waiting_budget"
    WAITING_DEPENDENCY = "waiting_dependency"
    WAITING_ACCESS = "waiting_access"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYABLE = "retryable"
    WAITING_HUMAN = "waiting_human"
    WAITING_CAPACITY = "waiting_capacity"
    BLOCKED_POLICY = "blocked_policy"
    CANCELLED = "cancelled"
    SKIPPED_BY_POLICY = "skipped_by_policy"
    NEEDS_REPLAN = "needs_replan"
    FAILED_VALIDATION = "failed_validation"


class ReadinessState(str, Enum):
    NOT_READY = "not_ready"
    READY_FOR_SPEC = "ready_for_spec"
    READY_FOR_HANDOFF = "ready_for_handoff"
    VALIDATED_IN_SIMULATION = "validated_in_simulation"
    READY_FOR_RELEASE = "ready_for_release"
    OPERATIONALLY_VERIFIED = "operationally_verified"
    DELIVERED = "delivered"
    WAITING_HUMAN = "waiting_human"
    BLOCKED = "blocked"


class EnvironmentKind(str, Enum):
    REAL_LAB = "real_lab"
    TARGET_ENVIRONMENT = "target_environment"
    MOCK_ONLY = "mock_only"


class EvidenceResult(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    PENDING = "pending"


class EvidenceFreshness(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    INVALID = "invalid"


class ManualDependencyStatus(str, Enum):
    WAITING = "waiting"
    RESOLVED = "resolved"
    BLOCKED = "blocked"


class HandoffOrigin(str, Enum):
    USER_DEMAND = "user-demand"
    CODE_REVIEW = "code-review"
    AGENT_FEATURE = "agent-feature"


class PlannerTier(str, Enum):
    HIGH = "high"
    ECONOMY = "economy"


class Direction(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    BIDIRECTIONAL = "bidirectional"


class SecretReference(ContractModel):
    """A pointer to a secret, never the secret itself."""

    ref_id: Identifier
    provider: ShortText
    locator: ShortText
    variable_name: Identifier | None = None

    @field_validator("provider", "locator")
    @classmethod
    def reject_secret_values(cls, value: str) -> str:
        if _looks_like_secret_value(value):
            raise ValueError("secret references cannot contain secret values")
        return value


class SanitizedIdentity(ContractModel):
    """Identity metadata safe to retain in an evidence record."""

    subject: Identifier
    role: ShortText
    host: Identifier | None = None
    account_ref: Identifier | None = None


class GrillFact(ContractModel):
    fact_id: Identifier
    statement: LongText
    source: ShortText
    locator: ShortText | None = None


class GrillAlternative(ContractModel):
    alternative_id: Identifier
    label: ShortText
    consequence: ShortText


class GrillDecision(ContractModel):
    decision_id: Identifier
    question: LongText
    alternatives: list[GrillAlternative] = Field(..., min_length=2)
    selected_alternative_id: Identifier | None = None
    response: LongText | None = None
    decision_source: ShortText
    is_material: bool = True
    reused_decision_ref: Identifier | None = None

    def is_answered(self) -> bool:
        has_selection = self.selected_alternative_id is not None
        has_response = bool(self.response and self.response.strip())
        has_reuse = bool(self.reused_decision_ref and self.reused_decision_ref.strip())
        has_source = bool(self.decision_source and self.decision_source.strip())
        return (has_selection or has_response or has_reuse) and has_source

    @model_validator(mode="after")
    def selected_alternative_exists(self) -> "GrillDecision":
        alternative_ids = [item.alternative_id for item in self.alternatives]
        _ensure_unique(alternative_ids, "alternative IDs")
        if self.selected_alternative_id and self.selected_alternative_id not in alternative_ids:
            raise ValueError("selected alternative must belong to alternatives")
        return self


class GrillPendingQuestion(ContractModel):
    question_id: Identifier
    question: LongText
    impact: ShortText
    depends_on_human: bool = True


class GrillRecord(ContractModel):
    """Auditable outcome of the intention/ambiguity Grill stage."""

    schema_version: Literal[1] = 1
    demand_id: Identifier
    demand_version: int = Field(default=1, ge=1)
    intent_summary: LongText
    known_facts: list[GrillFact] = Field(default_factory=list)
    decisions: list[GrillDecision] = Field(default_factory=list)
    assumptions: list[ShortText] = Field(default_factory=list)
    pending_questions: list[GrillPendingQuestion] = Field(default_factory=list)
    example_criteria: list[ShortText] = Field(default_factory=list)
    ready_for_spec: bool = False
    readiness_justification: LongText
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_readiness(self) -> "GrillRecord":
        _ensure_unique([item.fact_id for item in self.known_facts], "fact IDs")
        _ensure_unique([item.decision_id for item in self.decisions], "decision IDs")
        _ensure_unique([item.question_id for item in self.pending_questions], "pending question IDs")
        if self.ready_for_spec:
            if self.pending_questions:
                raise ValueError("a Grill ready for specification cannot have pending questions")
            if not self.example_criteria:
                raise ValueError("a Grill ready for specification requires example criteria")
            unanswered_materials = [
                d.decision_id for d in self.decisions if d.is_material and not d.is_answered()
            ]
            if unanswered_materials:
                raise ValueError(
                    f"a Grill ready for specification cannot have unanswered material decisions: {unanswered_materials}"
                )
        return self


class EnvironmentTool(ContractModel):
    name: Identifier
    version: ShortText
    source: ShortText


class EnvironmentEndpoint(ContractModel):
    endpoint_id: Identifier
    url: ShortText
    protocol: Literal["http", "https", "postgres", "postgresql", "sqlite", "file", "other"] = "https"

    @field_validator("url")
    @classmethod
    def reject_embedded_credentials(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.username or parsed.password:
            raise ValueError("environment endpoints cannot contain embedded credentials")
        if parsed.scheme in {"http", "https", "postgres", "postgresql"} and not parsed.hostname:
            raise ValueError("network endpoints require a host")

        if parsed.query:
            if _looks_like_secret_value(parsed.query):
                raise ValueError("environment endpoints cannot contain credential parameters in query")
            for qp in parsed.query.split("&"):
                k = qp.split("=")[0].lower().strip()
                if k in {"token", "access_token", "api_key", "apikey", "key", "secret", "password", "passwd", "auth", "credential"}:
                    raise ValueError("environment endpoints cannot contain credential parameters in query")

        if parsed.fragment:
            if _looks_like_secret_value(parsed.fragment):
                raise ValueError("environment endpoints cannot contain credential parameters in fragment")
            for fp in parsed.fragment.split("&"):
                k = fp.split("=")[0].lower().strip()
                if k in {"token", "access_token", "api_key", "apikey", "key", "secret", "password", "passwd", "auth", "credential"}:
                    raise ValueError("environment endpoints cannot contain credential parameters in fragment")

        if _looks_like_secret_value(value):
            raise ValueError("environment endpoints cannot contain credentials")

        return value


class EnvironmentPort(ContractModel):
    port: int = Field(..., ge=1, le=65535)
    direction: Direction
    purpose: ShortText


class EnvironmentManifest(ContractModel):
    """Reproducible environment declaration without secret material."""

    schema_version: Literal[1] = 1
    environment_ref: Identifier
    ticket_id: Identifier
    kind: EnvironmentKind
    tools: list[EnvironmentTool] = Field(default_factory=list)
    system: ShortText
    architecture: ShortText
    services: list[ShortText] = Field(default_factory=list)
    accounts: list[ShortText] = Field(default_factory=list)
    secret_refs: list[SecretReference] = Field(default_factory=list)
    permission_scopes: list[ShortText] = Field(default_factory=list)
    required_env_vars: list[Identifier] = Field(default_factory=list)
    endpoints: list[EnvironmentEndpoint] = Field(default_factory=list)
    ports: list[EnvironmentPort] = Field(default_factory=list)
    connection_origins: list[ShortText] = Field(default_factory=list)
    network_policy: ShortText
    proxy: ShortText | None = None
    tailscale: ShortText | None = None
    volumes: list[ShortText] = Field(default_factory=list)
    quotas: list[ShortText] = Field(default_factory=list)
    worker_identity: SanitizedIdentity
    installation: list[ShortText] = Field(default_factory=list)
    probes: list[ShortText] = Field(default_factory=list)
    rollback: list[ShortText] = Field(default_factory=list)
    cleanup: list[ShortText] = Field(default_factory=list)
    target_differences: list[ShortText] = Field(default_factory=list)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("services")
    @classmethod
    def validate_services(cls, services: list[str]) -> list[str]:
        for s in services:
            if "://" in s:
                raise ValueError("services must be identifiers or references, not connection strings or DSNs")
            if "=" in s or _looks_like_secret_value(s):
                raise ValueError("services must be identifiers or references, not credentials or assignments")
        return services

    @model_validator(mode="after")
    def validate_manifest(self) -> "EnvironmentManifest":
        _ensure_unique([item.name for item in self.tools], "tool names")
        _ensure_unique([item.ref_id for item in self.secret_refs], "secret reference IDs")
        _ensure_unique([item.endpoint_id for item in self.endpoints], "endpoint IDs")
        if self.kind is EnvironmentKind.MOCK_ONLY and not self.target_differences:
            raise ValueError("mock_only manifests must document target differences")
        return self


class EnvironmentEvidence(ContractModel):
    """One independently auditable probe performed in a declared environment."""

    schema_version: Literal[1] = 1
    evidence_id: Identifier
    requirement: ShortText
    environment_ref: Identifier
    identity: SanitizedIdentity
    origin: ShortText
    build_digest: ShortText
    config_version: Identifier
    test_name: Identifier
    expected: LongText
    observed: LongText
    result: EvidenceResult
    freshness: EvidenceFreshness = EvidenceFreshness.CURRENT
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    evidence_ref: ShortText

    @field_validator("build_digest", "config_version", "origin", "expected", "observed", "evidence_ref")
    @classmethod
    def reject_secret_material(cls, value: str) -> str:
        if _looks_like_secret_value(value):
            raise ValueError("environment evidence cannot contain secret values")
        return value


class AlternativeAttempt(ContractModel):
    alternative_id: Identifier
    description: ShortText
    tested: bool
    equivalent: bool
    reason_unusable: ShortText | None = None

    @model_validator(mode="after")
    def explain_failed_alternative(self) -> "AlternativeAttempt":
        if self.tested and not self.equivalent and not self.reason_unusable:
            raise ValueError("a tested non-equivalent alternative needs a rejection reason")
        return self


class ManualStep(ContractModel):
    number: int = Field(..., ge=1)
    instruction: ShortText
    expected_result: ShortText


class ManualDependency(ContractModel):
    """Human-only action with a safe, deterministic resumption probe."""

    schema_version: Literal[1] = 1
    dependency_id: Identifier
    ticket_ids: list[Identifier] = Field(..., min_length=1)
    status: ManualDependencyStatus = ManualDependencyStatus.WAITING
    reason: LongText
    alternatives_attempted: list[AlternativeAttempt] = Field(..., min_length=1)
    configuration_location: ShortText
    prerequisites: list[ShortText] = Field(default_factory=list)
    steps: list[ManualStep] = Field(..., min_length=1)
    secret_ref: SecretReference | None = None
    final_probe: ShortText
    resume_criteria: ShortText
    help_route: ShortText
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None
    resolution_receipt_ref: ShortText | None = None
    blocked_stages: list[WorkflowState] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_dependency(self) -> "ManualDependency":
        _ensure_unique(self.ticket_ids, "ticket IDs")
        numbers = [step.number for step in self.steps]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("manual steps must be numbered consecutively from 1")
        if self.status is ManualDependencyStatus.RESOLVED:
            if self.resolved_at is None:
                raise ValueError("resolved manual dependency requires resolved_at")
            if not self.resolution_receipt_ref:
                raise ValueError("resolved manual dependency requires resolution_receipt_ref")
        if self.status is not ManualDependencyStatus.RESOLVED:
            if self.resolved_at is not None:
                raise ValueError("only resolved manual dependencies may have resolved_at")
            if self.resolution_receipt_ref is not None:
                raise ValueError("only resolved manual dependencies may have resolution_receipt_ref")
        return self


class EvidenceRequirement(ContractModel):
    evidence_id: Identifier
    description: ShortText
    required_for: ReadinessState


class WorkflowHandoff(ContractModel):
    """Complete, immutable-in-practice input to the HF-04 readiness gate."""

    schema_version: Literal[1] = 1
    ticket_id: Identifier
    parent_id: Identifier
    objective: LongText
    origin: HandoffOrigin
    plan_version: Identifier
    planner_id: Identifier
    planner_tier: PlannerTier
    approval_reference: ShortText
    baseline_sha: Identifier
    grill: GrillRecord
    environment: EnvironmentManifest
    environment_evidence: list[EnvironmentEvidence] = Field(default_factory=list)
    manual_dependencies: list[ManualDependency] = Field(default_factory=list)
    required_evidence: list[EvidenceRequirement] = Field(default_factory=list)
    state: WorkflowState = WorkflowState.READY_FOR_HANDOFF
    allowed_paths: list[ShortText] = Field(default_factory=list)
    read_only_paths: list[ShortText] = Field(default_factory=list)
    non_goals: list[ShortText] = Field(default_factory=list)
    acceptance_criteria: list[ShortText] = Field(default_factory=list)
    validate_commands: list[ShortText] = Field(default_factory=list)
    trigger_events: list[ShortText] = Field(default_factory=list)
    successor_event: ShortText
    conflict_keys: list[ShortText] = Field(default_factory=list)
    resource_requirements: list[ShortText] = Field(default_factory=list)
    retry_policy: ShortText
    resume_strategy: ShortText
    rollback_plan: ShortText

    @model_validator(mode="after")
    def validate_handoff(self) -> "WorkflowHandoff":
        if self.grill.demand_id != self.ticket_id:
            raise ValueError("Grill demand_id must match handoff ticket_id")
        if self.environment.ticket_id != self.ticket_id:
            raise ValueError("environment ticket_id must match handoff ticket_id")
        _ensure_unique([item.evidence_id for item in self.environment_evidence], "evidence IDs")
        _ensure_unique([item.dependency_id for item in self.manual_dependencies], "dependency IDs")
        _ensure_unique([item.evidence_id for item in self.required_evidence], "required evidence IDs")
        evidence_ids = {item.evidence_id for item in self.environment_evidence}
        required_ids = {item.evidence_id for item in self.required_evidence}
        if required_ids - evidence_ids and self.state is WorkflowState.DELIVERED:
            raise ValueError("delivered handoff must include every required evidence record")
        return self


class ReadinessReport(ContractModel):
    ticket_id: Identifier
    state: WorkflowState
    readiness: ReadinessState
    eligible: bool
    reasons: list[ShortText] = Field(default_factory=list)
    missing_evidence: list[Identifier] = Field(default_factory=list)
    blocking_dependency_ids: list[Identifier] = Field(default_factory=list)
    verified_evidence_ids: list[Identifier] = Field(default_factory=list)


_SECRET_PATTERN = re.compile(
    r"(?:postgres(?:ql)?://[^\s]+|(?:password|passwd|token|api[_-]?key|secret|credential|access[_-]?token)\s*[:=]\s*[^\s]+)",
    re.IGNORECASE,
)


def _looks_like_secret_value(value: str) -> bool:
    return bool(_SECRET_PATTERN.search(value))


def _ensure_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label} are not allowed")
