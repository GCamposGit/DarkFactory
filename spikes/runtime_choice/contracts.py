"""Strict, dependency-light contracts for the HF-02 laboratory.

The contracts are transport objects for the experiment only.  They deliberately
do not import DBOS, PostgreSQL drivers, or any production runtime.  Optional
adapters can therefore be loaded lazily while the common test suite remains
offline and deterministic.
"""

from __future__ import annotations

import math
import re
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Final
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainValidator,
    field_validator,
    model_validator,
)


_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_LAB_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ENV_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_SCENARIO_ID_RE: Final[re.Pattern[str]] = re.compile(r"^R(?:0[1-9]|1[0-2])$")
_DATABASE_ALIAS_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_SECRET_MARKERS: Final[tuple[str, ...]] = (
    "postgresql://",
    "postgres://",
    "mysql://",
    "mongodb://",
    "password=",
    "passwd=",
    "sslpassword=",
    "secret=",
    "token=",
    "authorization:",
    "bearer ",
)
_SECRET_KEYS: Final[frozenset[str]] = frozenset(
    {"password", "passwd", "secret", "token", "dsn", "database_url", "credential"}
)


class RuntimeKind(StrEnum):
    """Runtime variants that the experiment is allowed to compare."""

    NATIVE_SQLITE = "native_sqlite"
    DBOS_POSTGRES = "dbos_postgres"


class WorkflowVersion(StrEnum):
    """Workflow revisions used by the version-isolation scenario."""

    V1 = "v1"
    V2 = "v2"


class RuntimeStatus(StrEnum):
    """Observable status values emitted by a driver."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    WAITING = "waiting"
    CANCELLED = "cancelled"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class DriverAction(StrEnum):
    """Commands accepted on the driver's JSONL stdin protocol."""

    START = "start"
    OBSERVE = "observe"
    APPROVE = "approve"
    CANCEL = "cancel"
    SHUTDOWN = "shutdown"


class DriverEventKind(StrEnum):
    """Synchronization events; these are not final oracle verdicts."""

    STARTED = "started"
    STEP_STARTED = "step_started"
    STEP_OBSERVED = "step_observed"
    WAITING = "waiting"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class ResultStatus(StrEnum):
    """Status of one scenario execution."""

    PASS = "pass"
    FAIL = "fail"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"
    ERROR = "error"


class DecisionStatus(StrEnum):
    """A comparison is never allowed to select a runtime by itself."""

    PENDING_ARCHITECT_REVIEW = "pending_architect_review"
    BLOCKED = "blocked"
    NO_CANDIDATE_QUALIFIED = "no_candidate_qualified"


class ValidationMode(StrEnum):
    """Origin of evidence associated with a scenario result."""

    REAL_LAB = "real_lab"
    TARGET_ENVIRONMENT = "target_environment"
    MOCK_ONLY = "mock_only"


class CliExitCode(IntEnum):
    """Stable CLI exit codes defined by the HF-02 handoff."""

    SUCCESS = 0
    ASSERTION_FAILED = 1
    ENVIRONMENT_BLOCKED = 2
    CONTRACT_ERROR = 3


class ScenarioCapability(StrEnum):
    """Closed capability vocabulary used by the R01-R12 catalogue."""

    DURABLE_STEPS = "durable_steps"
    RESUME_AFTER_CRASH = "resume_after_crash"
    EXTERNAL_EFFECT_DEDUPLICATION = "external_effect_deduplication"
    DURABLE_WAIT = "durable_wait"
    DEDUPLICATED_INTAKE = "deduplicated_intake"
    APPLICATION_PROTECTION = "application_protection"
    CANCEL_BEFORE_NEXT_STEP = "cancel_before_next_step"
    VERSION_ISOLATION = "version_isolation"
    BOUNDED_CONCURRENCY = "bounded_concurrency"
    INTEGRITY_VERIFICATION = "integrity_verification"
    DURABLE_COMPLETION = "durable_completion"
    STORAGE_FAILURE = "storage_failure"


class FaultPoint(StrEnum):
    """Controlled fault points permitted in the scenario catalogue."""

    CRASH_AFTER_CHECKPOINT = "crash_after_checkpoint"
    COMMIT_THEN_DISCONNECT_ONCE = "commit_then_disconnect_once"
    BEFORE_EFFECT = "before_effect"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    APPROVAL_DIGEST_MISMATCH = "approval_digest_mismatch"
    CANCEL_BEFORE_EFFECT = "cancel_before_effect"
    CORRUPT_RESULT = "corrupt_result"


class TerminalExpectation(StrEnum):
    """Expected terminal state recorded in the frozen catalogue."""

    SUCCEEDED = "succeeded"
    WAITING = "waiting"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


def _ensure_json_value(value: Any, *, path: str = "payload") -> None:
    """Reject non-JSON values without coercing caller input."""

    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _ensure_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} object keys must be strings")
            _ensure_json_value(item, path=f"{path}.{key}")
        return
    raise ValueError(f"{path} contains unsupported value type {type(value).__name__}")


def _reject_secret_material(value: Any, *, path: str = "value") -> None:
    """Reject raw credentials while preserving safe secret references."""

    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _SECRET_KEYS:
                raise ValueError(f"{path}.{key} must be a sanitized reference")
            _reject_secret_material(item, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_material(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str) and any(marker in value.lower() for marker in _SECRET_MARKERS):
        raise ValueError(f"{path} contains secret material")


def _non_blank(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized


def _safe_id(value: object, *, field_name: str) -> str:
    normalized = _non_blank(value, field_name=field_name)
    if not _ID_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} contains unsupported characters")
    return normalized


def _safe_reference(value: object, *, field_name: str) -> str:
    """Validate an evidence/reference locator without accepting credentials."""

    normalized = _non_blank(value, field_name=field_name)
    _reject_secret_material(normalized, path=field_name)
    parsed = urlsplit(normalized)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError(f"{field_name} must be a relative sanitized reference")
    path = Path(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must be a relative sanitized reference")
    if "\x00" in normalized or "\\" in normalized:
        raise ValueError(f"{field_name} contains an unsupported path character")
    return normalized


def _validate_root_dir(value: object) -> Path:
    """Accept Path/string on both Python and JSON validation routes."""

    if isinstance(value, bool) or not isinstance(value, (str, Path)):
        raise ValueError("root_dir must be a path string or pathlib.Path")
    if isinstance(value, str) and not value.strip():
        raise ValueError("root_dir must not be blank")
    path = Path(value)
    if not path.exists():
        raise ValueError("root_dir must exist")
    if not path.is_dir():
        raise ValueError("root_dir must be a directory")
    return path.resolve()


RootDirectory = Annotated[Path, PlainValidator(_validate_root_dir)]


class StrictLabModel(BaseModel):
    """Base model shared by every serialized laboratory contract."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_assignment=True,
    )


class LabConfig(StrictLabModel):
    """Configuration safe to serialize without a DSN or credential."""

    schema_version: str = Field(default="1", frozen=True)
    lab_id: str = Field(min_length=1)
    root_dir: RootDirectory
    runtime: RuntimeKind
    runtime_version: str = Field(min_length=1)
    workflow_version: WorkflowVersion
    database_alias: str = Field(min_length=1)
    database_url_env: str | None = None
    effect_base_url: str = Field(min_length=1)
    lease_seconds: float = Field(default=2.0, gt=0)
    scenario_timeout_seconds: float = Field(default=30.0, gt=0)
    process_start_timeout_seconds: float = Field(default=20.0, gt=0)
    sample_interval_ms: int = Field(default=100, gt=0)

    _ID_RE: ClassVar[re.Pattern[str]] = _LAB_ID_RE

    @field_validator("schema_version")
    @classmethod
    def _schema_is_supported(cls, value: str) -> str:
        if value != "1":
            raise ValueError("schema_version must be '1'")
        return value

    @field_validator("runtime", mode="before")
    @classmethod
    def _runtime_enum_from_json(cls, value: object) -> RuntimeKind:
        try:
            return value if isinstance(value, RuntimeKind) else RuntimeKind(value)
        except ValueError as error:
            raise ValueError("runtime must be a supported runtime") from error

    @field_validator("workflow_version", mode="before")
    @classmethod
    def _workflow_version_enum_from_json(cls, value: object) -> WorkflowVersion:
        try:
            return value if isinstance(value, WorkflowVersion) else WorkflowVersion(value)
        except ValueError as error:
            raise ValueError("workflow_version must be a supported version") from error

    @field_validator("lab_id")
    @classmethod
    def _lab_id_is_safe(cls, value: str) -> str:
        normalized = _non_blank(value, field_name="lab_id")
        if not _LAB_ID_RE.fullmatch(normalized):
            raise ValueError("lab_id contains unsupported characters")
        return normalized

    @field_validator("runtime_version")
    @classmethod
    def _runtime_version_is_safe(cls, value: str) -> str:
        normalized = _non_blank(value, field_name="runtime_version")
        _reject_secret_material(normalized, path="runtime_version")
        return normalized

    @field_validator("database_alias")
    @classmethod
    def _database_alias_is_safe(cls, value: str) -> str:
        normalized = _non_blank(value, field_name="database_alias").lower()
        if not _DATABASE_ALIAS_RE.fullmatch(normalized):
            raise ValueError("database_alias contains unsupported characters")
        if not normalized.startswith("darkfac_hf02_"):
            raise ValueError("database_alias must use the darkfac_hf02_ prefix")
        return normalized

    @field_validator("database_url_env")
    @classmethod
    def _database_url_is_an_env_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _non_blank(value, field_name="database_url_env")
        if not _ENV_NAME_RE.fullmatch(normalized):
            raise ValueError("database_url_env must be an uppercase environment name")
        return normalized

    @field_validator("effect_base_url")
    @classmethod
    def _effect_url_is_loopback(cls, value: str) -> str:
        normalized = _non_blank(value, field_name="effect_base_url").rstrip("/")
        try:
            parsed = urlsplit(normalized)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as error:
            raise ValueError("effect_base_url must be a valid HTTP URL") from error
        if parsed.scheme != "http" or hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("effect_base_url must be an HTTP loopback URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("effect_base_url must not contain credentials or query data")
        if port is None or not (1 <= port <= 65535):
            raise ValueError("effect_base_url must include a valid port")
        return normalized

    @model_validator(mode="after")
    def _runtime_database_contract(self) -> LabConfig:
        if self.runtime is RuntimeKind.DBOS_POSTGRES and self.database_url_env is None:
            raise ValueError("dbos_postgres requires database_url_env")
        _reject_secret_material(self.model_dump(mode="json"), path="LabConfig")
        return self


class ScenarioSpec(StrictLabModel):
    """One immutable entry in the R01-R12 oracle catalogue."""

    scenario_id: str
    required: bool
    capability: ScenarioCapability
    input_payload: dict[str, Any] = Field(default_factory=dict)
    fault_point: FaultPoint | None = None
    expected_terminal: TerminalExpectation
    expected_effect_count: int = Field(ge=0)
    expected_step_invocations: int = Field(ge=0)
    expected_block_reason: str | None = None

    @field_validator("capability", mode="before")
    @classmethod
    def _capability_enum_from_json(cls, value: object) -> ScenarioCapability:
        try:
            return value if isinstance(value, ScenarioCapability) else ScenarioCapability(value)
        except ValueError as error:
            raise ValueError("capability must be supported") from error

    @field_validator("fault_point", mode="before")
    @classmethod
    def _fault_point_enum_from_json(cls, value: object) -> FaultPoint | None:
        if value is None or isinstance(value, FaultPoint):
            return value
        try:
            return FaultPoint(value)
        except ValueError as error:
            raise ValueError("fault_point must be supported") from error

    @field_validator("expected_terminal", mode="before")
    @classmethod
    def _terminal_enum_from_json(cls, value: object) -> TerminalExpectation:
        try:
            return value if isinstance(value, TerminalExpectation) else TerminalExpectation(value)
        except ValueError as error:
            raise ValueError("expected_terminal must be supported") from error

    @field_validator("scenario_id")
    @classmethod
    def _scenario_id_is_known_shape(cls, value: str) -> str:
        if not _SCENARIO_ID_RE.fullmatch(value):
            raise ValueError("scenario_id must be one of R01 through R12")
        return value

    @field_validator("input_payload")
    @classmethod
    def _payload_is_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        _ensure_json_value(value, path="input_payload")
        _reject_secret_material(value, path="input_payload")
        return value

    @field_validator("expected_block_reason")
    @classmethod
    def _block_reason_is_normalized(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _non_blank(value, field_name="expected_block_reason")


class AdapterCapabilities(StrictLabModel):
    """Capabilities derived from an adapter, not asserted by its driver."""

    durable_steps: bool = False
    resume_after_crash: bool = False
    durable_wait: bool = False
    deduplicated_intake: bool = False
    cancel_before_next_step: bool = False
    version_isolation: bool = False
    bounded_concurrency: bool = False


class DriverCommand(StrictLabModel):
    """One JSONL command sent to a driver process."""

    command_id: str
    action: DriverAction
    workflow_id: str | None = None
    scenario_id: str | None = None
    workflow_version: WorkflowVersion | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("action", mode="before")
    @classmethod
    def _action_enum_from_json(cls, value: object) -> DriverAction:
        try:
            return value if isinstance(value, DriverAction) else DriverAction(value)
        except ValueError as error:
            raise ValueError("action must be supported") from error

    @field_validator("workflow_version", mode="before")
    @classmethod
    def _workflow_version_enum_from_json(cls, value: object) -> WorkflowVersion | None:
        if value is None or isinstance(value, WorkflowVersion):
            return value
        try:
            return WorkflowVersion(value)
        except ValueError as error:
            raise ValueError("workflow_version must be supported") from error

    @field_validator("command_id")
    @classmethod
    def _command_id_is_safe(cls, value: str) -> str:
        return _safe_id(value, field_name="command_id")

    @field_validator("workflow_id")
    @classmethod
    def _workflow_id_is_safe(cls, value: str | None) -> str | None:
        return None if value is None else _safe_id(value, field_name="workflow_id")

    @field_validator("scenario_id")
    @classmethod
    def _scenario_id_is_known(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _SCENARIO_ID_RE.fullmatch(value):
            raise ValueError("scenario_id must be one of R01 through R12")
        return value

    @field_validator("payload")
    @classmethod
    def _command_payload_is_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        _ensure_json_value(value, path="payload")
        _reject_secret_material(value, path="payload")
        return value

    @model_validator(mode="after")
    def _required_fields_match_action(self) -> DriverCommand:
        if self.action is DriverAction.SHUTDOWN:
            if self.workflow_id is not None or self.scenario_id is not None or self.workflow_version is not None:
                raise ValueError("shutdown does not accept workflow fields")
            return self
        if self.workflow_id is None:
            raise ValueError(f"{self.action.value} requires workflow_id")
        if self.action is DriverAction.START and (self.scenario_id is None or self.workflow_version is None):
            raise ValueError("start requires scenario_id and workflow_version")
        return self


class DriverEvent(StrictLabModel):
    """A synchronization signal emitted by a driver."""

    event_id: str
    workflow_id: str
    kind: DriverEventKind
    runtime_status: RuntimeStatus
    step_id: str | None = None
    code: str | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def _kind_enum_from_json(cls, value: object) -> DriverEventKind:
        try:
            return value if isinstance(value, DriverEventKind) else DriverEventKind(value)
        except ValueError as error:
            raise ValueError("kind must be supported") from error

    @field_validator("runtime_status", mode="before")
    @classmethod
    def _runtime_status_enum_from_json(cls, value: object) -> RuntimeStatus:
        try:
            return value if isinstance(value, RuntimeStatus) else RuntimeStatus(value)
        except ValueError as error:
            raise ValueError("runtime_status must be supported") from error

    @field_validator("event_id", "workflow_id")
    @classmethod
    def _ids_are_safe(cls, value: str, info: Any) -> str:
        return _safe_id(value, field_name=info.field_name)

    @field_validator("step_id", "code")
    @classmethod
    def _optional_strings_are_normalized(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _safe_id(value, field_name=info.field_name)


class ScenarioResult(StrictLabModel):
    """Sanitized result record for one scenario/repeat.

    The validation-origin fields are mandatory.  A result without an explicit
    origin cannot be promoted later into evidence for a real laboratory or a
    target deployment.
    """

    lab_id: str
    scenario_id: str
    runtime: RuntimeKind
    repeat_index: int = Field(ge=1)
    status: ResultStatus
    environment_ref: str
    validation_mode: ValidationMode
    target_differences: list[str]
    assertions: dict[str, bool] = Field(default_factory=dict)
    duration_ms: float = Field(ge=0)
    recovery_ms: float | None = Field(default=None, ge=0)
    rss_peak_mib: float | None = Field(default=None, ge=0)
    effect_count: int = Field(ge=0)
    actual_step_invocations: int = Field(ge=0)
    artifact_refs: list[str] = Field(default_factory=list)
    error_code: str | None = None

    @field_validator("runtime", mode="before")
    @classmethod
    def _runtime_enum_from_json(cls, value: object) -> RuntimeKind:
        try:
            return value if isinstance(value, RuntimeKind) else RuntimeKind(value)
        except ValueError as error:
            raise ValueError("runtime must be supported") from error

    @field_validator("status", mode="before")
    @classmethod
    def _status_enum_from_json(cls, value: object) -> ResultStatus:
        try:
            return value if isinstance(value, ResultStatus) else ResultStatus(value)
        except ValueError as error:
            raise ValueError("status must be supported") from error

    @field_validator("validation_mode", mode="before")
    @classmethod
    def _validation_mode_enum_from_json(cls, value: object) -> ValidationMode:
        try:
            return value if isinstance(value, ValidationMode) else ValidationMode(value)
        except ValueError as error:
            raise ValueError("validation_mode must be real_lab, target_environment, or mock_only") from error

    @field_validator("lab_id")
    @classmethod
    def _lab_id_is_safe(cls, value: str) -> str:
        normalized = _non_blank(value, field_name="lab_id")
        if not _LAB_ID_RE.fullmatch(normalized):
            raise ValueError("lab_id contains unsupported characters")
        return normalized

    @field_validator("scenario_id")
    @classmethod
    def _scenario_id_is_known(cls, value: str) -> str:
        if not _SCENARIO_ID_RE.fullmatch(value):
            raise ValueError("scenario_id must be one of R01 through R12")
        return value

    @field_validator("environment_ref")
    @classmethod
    def _environment_ref_is_sanitized(cls, value: str) -> str:
        return _safe_reference(value, field_name="environment_ref")

    @field_validator("target_differences")
    @classmethod
    def _target_differences_are_sanitized(cls, value: list[str]) -> list[str]:
        normalized = [_safe_reference(item, field_name="target_difference") for item in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("target_differences must not contain duplicate references")
        return normalized

    @field_validator("assertions")
    @classmethod
    def _assertions_are_safe(cls, value: dict[str, bool]) -> dict[str, bool]:
        _reject_secret_material(value, path="assertions")
        return value

    @field_validator("artifact_refs")
    @classmethod
    def _artifact_refs_are_relative(cls, value: list[str]) -> list[str]:
        normalized = [_safe_reference(ref, field_name="artifact_ref") for ref in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("artifact_refs must not contain duplicates")
        return normalized

    @field_validator("error_code")
    @classmethod
    def _error_code_is_safe(cls, value: str | None) -> str | None:
        return None if value is None else _safe_id(value, field_name="error_code")


class RuntimeComparison(StrictLabModel):
    """Comparison envelope consumed later by the architectural reviewer."""

    schema_version: str = Field(default="1", frozen=True)
    baseline_snapshot_hash: str = Field(min_length=1)
    environment_ref: str
    code_sha: str = Field(min_length=1)
    results: list[ScenarioResult] = Field(default_factory=list)
    capability_matrix: dict[str, AdapterCapabilities] = Field(default_factory=dict)
    eligibility: dict[str, Any] = Field(default_factory=dict)
    operational_metrics: dict[str, Any] = Field(default_factory=dict)
    decision_status: DecisionStatus = DecisionStatus.PENDING_ARCHITECT_REVIEW

    @field_validator("decision_status", mode="before")
    @classmethod
    def _decision_status_enum_from_json(cls, value: object) -> DecisionStatus:
        try:
            return value if isinstance(value, DecisionStatus) else DecisionStatus(value)
        except ValueError as error:
            raise ValueError("decision_status must be supported") from error

    @field_validator("schema_version")
    @classmethod
    def _comparison_schema_is_supported(cls, value: str) -> str:
        if value != "1":
            raise ValueError("schema_version must be '1'")
        return value

    @field_validator("baseline_snapshot_hash", "code_sha")
    @classmethod
    def _metadata_is_non_blank(cls, value: str, info: Any) -> str:
        normalized = _non_blank(value, field_name=info.field_name)
        _reject_secret_material(normalized, path=info.field_name)
        return normalized

    @field_validator("environment_ref")
    @classmethod
    def _comparison_environment_ref_is_sanitized(cls, value: str) -> str:
        return _safe_reference(value, field_name="environment_ref")

    @field_validator("capability_matrix")
    @classmethod
    def _capability_keys_are_closed(cls, value: dict[str, AdapterCapabilities]) -> dict[str, AdapterCapabilities]:
        supported = {runtime.value for runtime in RuntimeKind}
        unknown = set(value) - supported
        if unknown:
            raise ValueError(f"capability_matrix has unknown runtimes: {sorted(unknown)}")
        return value

    @field_validator("eligibility", "operational_metrics")
    @classmethod
    def _comparison_metadata_is_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        _ensure_json_value(value, path="comparison_metadata")
        _reject_secret_material(value, path="comparison_metadata")
        if "selected_runtime" in value:
            raise ValueError("comparison must not select a runtime")
        return value

    @model_validator(mode="after")
    def _results_are_unique(self) -> RuntimeComparison:
        seen: set[tuple[str, str, RuntimeKind, int]] = set()
        for result in self.results:
            key = (result.lab_id, result.scenario_id, result.runtime, result.repeat_index)
            if key in seen:
                raise ValueError("results must not contain repeated lab/scenario/runtime/repeat IDs")
            seen.add(key)
        return self


__all__ = [
    "AdapterCapabilities",
    "CliExitCode",
    "DecisionStatus",
    "DriverAction",
    "DriverCommand",
    "DriverEvent",
    "DriverEventKind",
    "FaultPoint",
    "LabConfig",
    "ResultStatus",
    "RuntimeComparison",
    "RuntimeKind",
    "RuntimeStatus",
    "ScenarioCapability",
    "ScenarioResult",
    "ScenarioSpec",
    "TerminalExpectation",
    "ValidationMode",
    "WorkflowVersion",
]
