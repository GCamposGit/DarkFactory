"""Pydantic contracts used by the HF-01 baseline collector.

The models deliberately keep declarations, observations and claims separate.
In particular, a readable file is not evidence that the capability it names is
implemented, integrated or operational.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    """Return an aware UTC timestamp for portable observations."""

    return datetime.now(timezone.utc)


def _non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("value must not be blank")
    return value


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _relative_path(value: Path | str) -> Path:
    path = Path(value)
    windows_path = PureWindowsPath(str(value))
    if (
        not str(value).strip()
        or path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or path == Path(".")
        or ".." in path.parts
        or any(char in str(value) for char in "*?[]")
    ):
        raise ValueError("relative_path must be a non-empty repository-relative file path")
    return path


class BaselineSourceKind(str, Enum):
    JSON_ITEMS = "json_items"
    MARKDOWN_TABLE = "markdown_table"
    REPORT = "report"
    INVENTORY = "inventory"
    ADR = "adr"
    OWNER_STATEMENT = "owner_statement"
    SOURCE_FILE = "source_file"


class SourceStatus(str, Enum):
    READ = "read"
    MISSING = "missing"
    INVALID = "invalid"
    UNSTABLE = "unstable"
    ACCESS_DENIED = "access_denied"


class ClaimDimension(str, Enum):
    IMPLEMENTATION = "implementation"
    INTEGRATION = "integration"
    OPERATION = "operation"


class ClaimAssertion(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    PARTIAL = "partial"


class EvidenceKind(str, Enum):
    DOCUMENT = "document"
    OWNER_STATEMENT = "owner_statement"
    REMOTE_GIT = "remote_git"
    SERVICE_PROBE = "service_probe"


class AssessmentDimension(str, Enum):
    UNKNOWN = "unknown"
    REPORTED = "reported"
    VERIFIED = "verified"
    PARTIAL = "partial"
    CONTRADICTED = "contradicted"


class IssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class CompletenessStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INVALID = "invalid"


class HF02Readiness(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BaselineSourceSpec(_StrictModel):
    """One explicitly authorized, repository-relative source file."""

    source_id: str = Field(..., min_length=1)
    kind: BaselineSourceKind
    relative_path: Path
    required: bool
    section_heading: str | None = None
    table_header: list[str] | None = None
    item_id_column: str | None = None

    @field_validator("source_id", "section_heading", "item_id_column")
    @classmethod
    def validate_non_blank_text(cls, value: str | None) -> str | None:
        return _non_blank(value) if value is not None else None

    @field_validator("relative_path", mode="before")
    @classmethod
    def validate_relative_path(cls, value: Path | str) -> Path:
        return _relative_path(value)

    @field_validator("table_header")
    @classmethod
    def validate_table_header(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value or any(not isinstance(cell, str) or not cell.strip() for cell in value):
            raise ValueError("table_header must contain non-empty column names")
        return [cell.strip() for cell in value]

    @model_validator(mode="after")
    def validate_table_contract(self) -> BaselineSourceSpec:
        if self.kind == BaselineSourceKind.MARKDOWN_TABLE:
            if not self.table_header or not self.item_id_column:
                raise ValueError("markdown_table sources require table_header and item_id_column")
            if self.item_id_column not in self.table_header:
                raise ValueError("item_id_column must be present in table_header")
        elif self.table_header is not None:
            raise ValueError("table_header is only valid for markdown_table sources")
        return self


class PlannedItem(_StrictModel):
    """A declaration extracted from one source, before reconciliation."""

    item_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    declared_status: str = Field(..., min_length=1)
    dependencies: list[str] = Field(default_factory=list)
    source_id: str = Field(..., min_length=1)
    locator: str = Field(..., min_length=1)

    @field_validator("item_id", "title", "declared_status", "source_id", "locator")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _non_blank(value)

    @field_validator("dependencies")
    @classmethod
    def validate_dependencies(cls, value: list[str]) -> list[str]:
        if any(not dependency.strip() for dependency in value):
            raise ValueError("dependencies must not contain blank IDs")
        return value


class SourceObservation(_StrictModel):
    source_id: str = Field(..., min_length=1)
    relative_path: Path
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: SourceStatus
    observed_at: datetime = Field(default_factory=utc_now)
    error_code: str | None = None

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        return _non_blank(value)

    @field_validator("relative_path", mode="before")
    @classmethod
    def validate_observation_path(cls, value: Path | str) -> Path:
        return _relative_path(value)

    _validate_time = field_validator("observed_at")(_aware_utc)


class EvidenceClaim(_StrictModel):
    claim_id: str = Field(..., min_length=1)
    item_id: str = Field(..., min_length=1)
    dimension: ClaimDimension
    assertion: ClaimAssertion
    evidence_kind: EvidenceKind
    source_id: str = Field(..., min_length=1)
    locator: str = Field(..., min_length=1)
    source_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    candidate_sha: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    observed_at: datetime | None = None
    scope: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1, max_length=400)

    @field_validator("claim_id", "item_id", "source_id", "locator", "scope", "summary")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _non_blank(value)

    _validate_time = field_validator("observed_at")(_aware_utc)


class CapabilityAssessment(_StrictModel):
    item_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    declared_status: str = Field(..., min_length=1)
    implementation: AssessmentDimension = AssessmentDimension.UNKNOWN
    integration: AssessmentDimension = AssessmentDimension.UNKNOWN
    operation: AssessmentDimension = AssessmentDimension.UNKNOWN
    evidence_ids: list[str] = Field(default_factory=list)
    issue_ids: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)


class BaselineIssue(_StrictModel):
    issue_id: str = Field(..., min_length=1)
    code: str = Field(..., min_length=1)
    severity: IssueSeverity
    item_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    required_action: str = Field(..., min_length=1)
    target_package: str | None = None

    @field_validator("issue_id", "code", "required_action")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _non_blank(value)

    @field_validator("target_package")
    @classmethod
    def validate_target_package(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = _non_blank(value)
        if value not in {"HF-01", "HF-02", "HF-03", "HF-07", "HF-13"}:
            raise ValueError("target_package is not an HF package in the contract")
        return value


class CollectedBaseline(_StrictModel):
    observations: list[SourceObservation] = Field(default_factory=list)
    planned_items: list[PlannedItem] = Field(default_factory=list)
    claims: list[EvidenceClaim] = Field(default_factory=list)
    issues: list[BaselineIssue] = Field(default_factory=list)


class BaselineSnapshot(_StrictModel):
    schema_version: str = Field(default="1", pattern=r"^1$")
    snapshot_id: str = Field(..., min_length=1)
    observed_at: datetime
    base_sha: str = Field(..., min_length=1)
    source_fingerprint: str = Field(..., min_length=1)
    source_observations: list[SourceObservation] = Field(default_factory=list)
    items: list[CapabilityAssessment] = Field(default_factory=list)
    claims: list[EvidenceClaim] = Field(default_factory=list)
    issues: list[BaselineIssue] = Field(default_factory=list)
    completeness: CompletenessStatus
    hf02_readiness: HF02Readiness
    blocker_codes: list[str] = Field(default_factory=list)

    _validate_time = field_validator("observed_at")(_aware_utc)
