"""Canonical persistence and control contracts for the HF-05 hybrid workflow boundary.

Normative implementation of CONTRACTS.md, HF-05-02 binding, and control.json.
Governed by ADR-HF-001.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from core.workflow.contracts import ContractModel


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ControlError(RuntimeError):
    """Base error for control contracts and store operations."""


class IdempotencyConflict(ControlError):
    """A command or operation was resubmitted with a conflicting payload."""


class StaleLeaseError(ControlError):
    """A worker attempted to modify state with an expired or superseded lease/fencing token."""


class StoreUnavailableError(ControlError):
    """The underlying storage engine is unavailable or timed out."""


class OutboxNotFoundError(ControlError):
    """An outbox event was not found for publication or materialization."""


class InvalidResultError(ControlError):
    """A stage result violates acceptance invariants."""


class InvalidPayloadError(ControlError):
    """An intake payload is malformed or missing mandatory keys."""


class UnknownProjectError(ControlError):
    """The specified project identifier is unknown or disallowed."""


# ---------------------------------------------------------------------------
# Enums and Type Aliases
# ---------------------------------------------------------------------------


class RuntimeOwner(str, Enum):
    """Canonical execution runtime owners as defined by ADR-HF-001."""

    HF05_SQLITE = "hf05_sqlite"
    DF11_LEGACY = "df11_legacy"
    CLOUD_DBOS_POSTGRES = "cloud_dbos_postgres"


RuntimeOwnerLiteral = Literal["hf05_sqlite", "df11_legacy", "cloud_dbos_postgres"]

REQUIRED_PAYLOAD_KEYS: tuple[str, ...] = (
    "title",
    "problem",
    "journey",
    "non_goals",
    "criteria",
)


def canonical_payload_digest(payload: dict[str, Any]) -> str:
    """Compute the deterministic SHA-256 digest of canonical UTF-8 JSON payload.

    Invariants:
    - Sorted keys (sort_keys=True)
    - Separators (',', ':')
    - No NaN/Infinity
    - Preserves array ordering
    - Preserves raw intent text
    """
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


# ---------------------------------------------------------------------------
# Canonical Contract Models
# ---------------------------------------------------------------------------


class JobKey(ContractModel):
    """Strict composite key identifying an atomic stage execution unit.

    Never represented as an ambiguous delimited string in the database.
    Mapped to the 5-tuple: (run_id, ticket_id, plan_version, stage, iteration).
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        frozen=True,
    )

    run_id: Annotated[str, Field(min_length=1, max_length=160)]
    ticket_id: Annotated[str, Field(min_length=1, max_length=64)]
    plan_version: Annotated[str, Field(min_length=1, max_length=64)]
    stage: Annotated[str, Field(min_length=1, max_length=64)]
    iteration: Annotated[int, Field(ge=0)]

    def canonical_key(self) -> str:
        """Return canonical string representation."""
        return f"{self.run_id}:{self.ticket_id}:{self.plan_version}:{self.stage}:{self.iteration}"

    def to_tuple(self) -> tuple[str, str, str, str, int]:
        """Return 5-tuple representation for database keys and comparisons."""
        return (self.run_id, self.ticket_id, self.plan_version, self.stage, self.iteration)

    @classmethod
    def from_string(cls, key_str: str) -> JobKey:
        """Parse a canonical key string into a JobKey."""
        parts = key_str.split(":")
        if len(parts) != 5:
            raise ValueError(
                f"Invalid canonical JobKey string format: '{key_str}' (expected 5 colon-separated parts)"
            )
        try:
            iteration = int(parts[4])
        except ValueError as exc:
            raise ValueError(f"JobKey iteration must be an integer, got '{parts[4]}'") from exc
        return cls(
            run_id=parts[0],
            ticket_id=parts[1],
            plan_version=parts[2],
            stage=parts[3],
            iteration=iteration,
        )

    parse = from_string

    def __hash__(self) -> int:
        return hash(self.to_tuple())

    def __eq__(self, other: object) -> bool:
        if isinstance(other, JobKey):
            return self.to_tuple() == other.to_tuple()
        return False

    def __lt__(self, other: JobKey) -> bool:
        if not isinstance(other, JobKey):
            return NotImplemented
        return self.to_tuple() < other.to_tuple()

    def __le__(self, other: JobKey) -> bool:
        if not isinstance(other, JobKey):
            return NotImplemented
        return self.to_tuple() <= other.to_tuple()

    def __gt__(self, other: JobKey) -> bool:
        if not isinstance(other, JobKey):
            return NotImplemented
        return self.to_tuple() > other.to_tuple()

    def __ge__(self, other: JobKey) -> bool:
        if not isinstance(other, JobKey):
            return NotImplemented
        return self.to_tuple() >= other.to_tuple()


class IntakeCommand(ContractModel):
    """Canonical intake command submitted via external channels."""

    project_id: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=160,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
        ),
    ]
    channel: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    external_id: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    payload: dict[str, Any]
    mode: Literal["autonomous", "documentary"]
    policy_ref: Annotated[str, StringConstraints(min_length=1, max_length=160)]

    @field_validator("payload")
    @classmethod
    def validate_payload_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        missing = [k for k in REQUIRED_PAYLOAD_KEYS if k not in v]
        if missing:
            raise ValueError(f"Intake payload missing required keys: {missing}")
        return v

    @property
    def payload_digest(self) -> str:
        """Deterministically computed digest of payload."""
        return canonical_payload_digest(self.payload)


class IntakeReceipt(ContractModel):
    """Deterministic receipt issued upon intake acceptance."""

    demand_id: str
    demand_version: str
    run_id: str | None = None
    initial_job_id: str | None = None
    mode: Literal["autonomous", "documentary"]
    committed_at: str  # ISO 8601 UTC

    @model_validator(mode="after")
    def validate_mode_invariants(self) -> IntakeReceipt:
        if self.mode == "autonomous":
            if not self.run_id or not self.initial_job_id:
                raise ValueError(
                    "Autonomous IntakeReceipt requires non-empty run_id and initial_job_id"
                )
        elif self.mode == "documentary":
            if self.run_id is not None or self.initial_job_id is not None:
                raise ValueError(
                    "Documentary IntakeReceipt must have null run_id and initial_job_id"
                )
        return self


class Claim(ContractModel):
    """Exclusive distributed lease awarded to an identified worker for a specific JobKey."""

    job_key: JobKey
    lease_id: Annotated[str, Field(min_length=1, max_length=160)]
    owner: Annotated[str, Field(min_length=1, max_length=160)]
    fencing_token: Annotated[int, Field(gt=0)]
    expires_at: str  # ISO 8601 UTC
    reservation_id: Annotated[str, Field(default="", max_length=160)]
    route_ref: Annotated[str, Field(default="", max_length=160)]
    acquired_at: str | None = None

    @model_validator(mode="before")
    @classmethod
    def handle_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "lease_token" in data and "lease_id" not in data:
                data["lease_id"] = data.pop("lease_token")
            if "owner_worker" in data and "owner" not in data:
                data["owner"] = data.pop("owner_worker")
        return data

    @property
    def lease_token(self) -> str:
        return self.lease_id

    @property
    def owner_worker(self) -> str:
        return self.owner


class StageContext(ContractModel):
    """Immutable context provided to StageHandler for execution."""

    claim: Claim
    plan_ref: str
    plan_digest: str
    candidate_digest: str | None = None
    config_version: str
    environment_ref: str
    identity: str
    route_ref: str
    memory_version: str
    input_refs: list[str] = Field(default_factory=list)


class StageResult(ContractModel):
    """Terminal or intermediate outcome emitted by StageHandler."""

    outcome: Literal[
        "success",
        "retry",
        "replan",
        "waiting_dependency",
        "waiting_human",
        "cancelled",
        "failed",
    ]
    output_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    operation_refs: list[str] = Field(default_factory=list)
    actual_cost: Annotated[float, Field(default=0.0, ge=0.0)]
    cause_code: str | None = None

    @model_validator(mode="after")
    def validate_success_outputs(self) -> StageResult:
        if self.outcome == "success" and not self.output_refs:
            raise ValueError("outcome == 'success' requires output_refs to be non-empty")
        return self


class HandlerDescriptor(ContractModel):
    """Metadata descriptor describing stage requirements and constraints."""

    stage: Annotated[str, Field(min_length=1, max_length=64)]
    version: Annotated[str, Field(min_length=1, max_length=64)]
    input_schema_ref: str
    output_schema_ref: str
    role: Annotated[str, Field(min_length=1, max_length=64)]
    required_capabilities: list[str] = Field(default_factory=list)
    timeout_seconds: int = 1800
    conflict_scope: str = "job"


class ExternalOperation(ContractModel):
    """Idempotent record of side-effecting external operations."""

    operation_key: Annotated[str, Field(min_length=1, max_length=256)]
    request_digest: Annotated[str, Field(min_length=1, max_length=64)]
    provider: Annotated[str, Field(min_length=1, max_length=64)]
    external_id: str | None = None
    status: Literal["prepared", "sent", "unknown", "succeeded", "failed"]
    observed_at: str  # ISO 8601 UTC
    claim_lease_id: str | None = None
    fencing_token: int = 0
    response_digest: str | None = None
    error_details: str | None = None
    payload: dict[str, Any] | None = None
    result: dict[str, Any] | str | None = None
    run_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def handle_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "operation_id" in data and "operation_key" not in data:
                data["operation_key"] = data.pop("operation_id")
            if "idempotency_key" in data and "operation_key" not in data:
                data["operation_key"] = data.pop("idempotency_key")
            if "service" in data and "provider" not in data:
                data["provider"] = data.pop("service")
        return data

    @property
    def operation_id(self) -> str:
        return self.operation_key

    @property
    def idempotency_key(self) -> str:
        return self.operation_key

    @property
    def service(self) -> str:
        return self.provider


class ReconcilePage(ContractModel):
    """Paginated report generated by reconciliation sweeper."""

    cursor: str | None = None
    visited_projects: list[str] = Field(default_factory=list)
    repaired_keys: list[dict[str, Any]] = Field(default_factory=list)
    next_cursor: str | None = None
    cycle_id: Annotated[str, Field(min_length=1, max_length=160)]


class OutboxEvent(ContractModel):
    """Transactional outbox event for reliable at-least-once dispatch."""

    outbox_id: int | None = None
    event_id: str | None = None
    event_type: Annotated[str, Field(default="job_finished", min_length=1, max_length=64)]
    aggregate_type: Annotated[str, Field(default="run", min_length=1, max_length=64)]
    aggregate_id: Annotated[str, Field(default="", max_length=160)]
    run_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    target_system: Annotated[str, Field(default="cloud_dbos", max_length=64)]
    status: Literal["pending", "published", "failed", "dead_letter"] = "pending"
    retry_count: Annotated[int, Field(default=0, ge=0)]
    created_at: str = ""
    published_at: str | None = None
    error_message: str | None = None

    @model_validator(mode="before")
    @classmethod
    def handle_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "topic" in data and "event_type" not in data:
                data["event_type"] = data.pop("topic")
            if "dispatched_at" in data and "published_at" not in data:
                data["published_at"] = data.pop("dispatched_at")
            if "event_id" in data and not data.get("aggregate_id"):
                data["aggregate_id"] = str(data["event_id"])
            if "run_id" in data and not data.get("aggregate_id"):
                data["aggregate_id"] = str(data["run_id"])
        return data

    @property
    def topic(self) -> str:
        return self.event_type

    @property
    def dispatched_at(self) -> str | None:
        return self.published_at
