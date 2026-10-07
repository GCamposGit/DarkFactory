"""Pydantic v2 contracts for the held-out E2E corpus, trajectories and hashing."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1"

CASE_ID_PATTERN = r"^E2E-[A-Z0-9]+(-[A-Z0-9]+)*$"
CANARY_PATTERN = r"^DFE2E-CANARY-[0-9a-f]{16}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"

# Directory prefixes and source extensions that betray a reference to the
# implementation.  An oracle may only use the public interface of the product.
_IMPL_DIR_RE = re.compile(
    r"(?i)(^|[\s/\\\"'=])(src|app|lib|tests?|node_modules|__pycache__|dist|build|core|pkg|cmd|internal)[/\\]"
)
_IMPL_EXT_RE = re.compile(
    r"(?i)\.(py|pyc|js|mjs|cjs|ts|tsx|jsx|go|rs|java|kt|rb|php|sh|ps1|cs|c|cpp|h)(?![A-Za-z0-9])"
)
_ABS_PATH_RE = re.compile(r"^([A-Za-z]:[\\/]|\\\\)")


def canonical_json(value: Any) -> bytes:
    """Stable UTF-8 JSON encoding used for every hash in this package."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def iter_strings(value: Any) -> Iterator[str]:
    """Yield every string leaf (keys excluded) of a JSON-like structure."""

    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_strings(item)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- #
# Corpus case                                                                  #
# --------------------------------------------------------------------------- #


class ProductKind(str, Enum):
    HTTP_API = "http_api"
    CLI = "cli"
    STATIC_SITE = "static_site"
    CHAT_BOT = "chat_bot"
    BATCH_JOB = "batch_job"
    WEBHOOK_SERVICE = "webhook_service"


class Visibility(str, Enum):
    RESERVED = "reserved"
    PUBLIC = "public"


class Expectation(_Frozen):
    """Assertions over an :class:`Observation`; every field is optional."""

    exit_code: int | None = None
    status: int | None = None
    contains: list[str] = Field(default_factory=list)
    not_contains: list[str] = Field(default_factory=list)
    regex: list[str] = Field(default_factory=list)
    error_contains: list[str] = Field(default_factory=list)
    json_subset: dict[str, Any] | None = None

    @field_validator("regex")
    @classmethod
    def regex_compiles(cls, value: list[str]) -> list[str]:
        for pattern in value:
            re.compile(pattern)
        return value

    @model_validator(mode="after")
    def not_empty(self) -> Expectation:
        if not (
            self.exit_code is not None
            or self.status is not None
            or self.contains
            or self.not_contains
            or self.regex
            or self.error_contains
            or self.json_subset
        ):
            raise ValueError("an oracle step must assert something")
        return self


class Signature(_Frozen):
    """HMAC-SHA256 (hex) over the canonical JSON body, sent in ``header``."""

    header: str = Field(min_length=1)
    secret: str = Field(min_length=1)


class _StepBase(_Frozen):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    expect: Expectation


class CliStep(_StepBase):
    kind: Literal["cli"] = "cli"
    args: list[str] = Field(default_factory=list)
    stdin: str = ""
    read_file: str | None = None


class HttpStep(_StepBase):
    kind: Literal["http"] = "http"
    method: Literal["GET", "POST", "PUT", "DELETE"] = "GET"
    path: str = Field(pattern=r"^/")
    json_body: dict[str, Any] | None = None
    sign: Signature | None = None


class ChatStep(_StepBase):
    kind: Literal["chat"] = "chat"
    message: str = Field(min_length=1)
    user: str = "user1"


OracleStep = Annotated[CliStep | HttpStep | ChatStep, Field(discriminator="kind")]


def _request_strings(step: CliStep | HttpStep | ChatStep) -> list[str]:
    """Strings of the *request* side of a step that must not name the implementation."""

    if isinstance(step, CliStep):
        values = list(step.args)
        if step.read_file:
            values.append(step.read_file)
        return values
    if isinstance(step, HttpStep):
        return [step.path]
    return []


def _check_public_only(text: str, *, absolute_check: bool) -> str | None:
    if _IMPL_DIR_RE.search(text):
        return "implementation directory"
    if _IMPL_EXT_RE.search(text):
        return "implementation source file"
    if ".." in re.split(r"[/\\]", text):
        return "parent traversal"
    if absolute_check and (text.startswith("/") or _ABS_PATH_RE.match(text)):
        return "absolute path"
    return None


class FaultKind(str, Enum):
    PROVIDER_OUTAGE = "provider_outage"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    MERGE_CONFLICT = "merge_conflict"
    WORKER_CRASH = "worker_crash"
    FLAKY_GATE = "flaky_gate"
    CORRUPTED_STATE = "corrupted_state"
    PROCESS_CRASH = "process_crash"


class FaultPhase(str, Enum):
    BUILD = "build"
    RUNTIME = "runtime"


class RecoveryScenario(_Frozen):
    """A fault injected into the run; the system must still deliver the journey."""

    fault: FaultKind
    phase: FaultPhase
    description: str = Field(min_length=10, max_length=400)
    inject_after_step: str | None = None
    max_recovery_seconds: int = Field(gt=0, le=86400)

    @model_validator(mode="after")
    def phase_consistency(self) -> RecoveryScenario:
        if self.phase is FaultPhase.RUNTIME and not self.inject_after_step:
            raise ValueError("runtime faults must name inject_after_step")
        if self.phase is FaultPhase.BUILD and self.inject_after_step:
            raise ValueError("build faults cannot name inject_after_step")
        return self


class E2ECase(_Frozen):
    schema_version: Literal["1"] = SCHEMA_VERSION
    case_id: str = Field(pattern=CASE_ID_PATTERN)
    product_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    product_kind: ProductKind
    title: str = Field(min_length=5, max_length=120)
    visibility: Visibility = Visibility.RESERVED
    canary: str = Field(pattern=CANARY_PATTERN)
    brief: str = Field(min_length=40, max_length=1200)
    spec: str = Field(min_length=60, max_length=2500)
    seed_files: dict[str, str] = Field(default_factory=dict)
    journey: list[OracleStep] = Field(min_length=3)
    recovery: RecoveryScenario
    time_budget_seconds: int = Field(gt=0, le=86400)
    cost_budget_usd: float = Field(gt=0, le=1000)

    @field_validator("seed_files")
    @classmethod
    def seed_paths_are_relative(cls, value: dict[str, str]) -> dict[str, str]:
        for path in value:
            if path.startswith("/") or _ABS_PATH_RE.match(path) or ".." in re.split(r"[/\\]", path):
                raise ValueError("seed file paths must be relative and cannot traverse parents")
        return value

    @model_validator(mode="after")
    def journey_is_public_only(self) -> E2ECase:
        ids = [step.id for step in self.journey]
        if len(set(ids)) != len(ids):
            raise ValueError("journey step ids must be unique within a case")
        for step in self.journey:
            for text in _request_strings(step):
                problem = _check_public_only(text, absolute_check=isinstance(step, CliStep))
                if problem:
                    raise ValueError(f"oracle step {step.id!r} references the implementation ({problem})")
        # Brief and spec may mention product behaviour, never implementation layout.
        for label, text in (("brief", self.brief), ("spec", self.spec)):
            if _IMPL_DIR_RE.search(text):
                raise ValueError(f"{label} references an implementation directory")
        if self.recovery.inject_after_step is not None and self.recovery.inject_after_step not in ids:
            raise ValueError("recovery.inject_after_step must name a journey step")
        return self

    @property
    def step_ids(self) -> list[str]:
        return [step.id for step in self.journey]

    def content_sha256(self) -> str:
        return sha256_hex(self.model_dump(mode="json"))

    def step_fingerprints(self) -> set[str]:
        """Hash of each step ignoring its id: used to detect shared journeys."""

        result: set[str] = set()
        for step in self.journey:
            payload = step.model_dump(mode="json")
            payload.pop("id", None)
            result.add(sha256_hex(payload))
        return result


# --------------------------------------------------------------------------- #
# Manifest                                                                     #
# --------------------------------------------------------------------------- #


class ManifestEntry(_Frozen):
    case_id: str = Field(pattern=CASE_ID_PATTERN)
    sha256: str = Field(pattern=SHA256_PATTERN)
    visibility: Visibility
    product_kind: ProductKind


class Manifest(_Frozen):
    """Versioned, publishable index of a corpus: ids and hashes, never content."""

    schema_version: Literal["1"] = SCHEMA_VERSION
    corpus_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,60}$")
    corpus_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    entries: list[ManifestEntry] = Field(min_length=1)
    manifest_sha256: str = Field(pattern=SHA256_PATTERN)

    @staticmethod
    def compute_hash(corpus_id: str, corpus_version: str, entries: list[ManifestEntry]) -> str:
        return sha256_hex(
            {
                "corpus_id": corpus_id,
                "corpus_version": corpus_version,
                "entries": [entry.model_dump(mode="json") for entry in entries],
            }
        )

    @model_validator(mode="after")
    def hash_is_consistent(self) -> Manifest:
        ids = [entry.case_id for entry in self.entries]
        if ids != sorted(ids) or len(set(ids)) != len(ids):
            raise ValueError("manifest entries must be unique and sorted by case_id")
        expected = self.compute_hash(self.corpus_id, self.corpus_version, list(self.entries))
        if expected != self.manifest_sha256:
            raise ValueError("manifest_sha256 does not match manifest content")
        return self


# --------------------------------------------------------------------------- #
# Trajectory                                                                   #
# --------------------------------------------------------------------------- #


class EventKind(str, Enum):
    AGENT_STEP = "agent_step"
    TOOL_CALL = "tool_call"
    FAULT_INJECTED = "fault_injected"
    FAULT_DETECTED = "fault_detected"
    RECOVERED = "recovered"
    HUMAN_INTERVENTION = "human_intervention"


class FinalState(str, Enum):
    DELIVERED = "delivered"
    ABANDONED = "abandoned"
    TIMEOUT = "timeout"
    ERROR = "error"


class TrajectoryEvent(_Frozen):
    seq: int = Field(ge=0)
    t_seconds: float = Field(ge=0)
    kind: EventKind
    detail: str = Field(default="", max_length=500)


class JourneyResult(_Frozen):
    step_id: str
    passed: bool


class Trajectory(_Frozen):
    """A recorded run of one system on one case; the unit the replay runner reads."""

    schema_version: Literal["1"] = SCHEMA_VERSION
    trajectory_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,80}$")
    case_id: str = Field(pattern=CASE_ID_PATTERN)
    case_sha256: str = Field(pattern=SHA256_PATTERN)
    system_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,60}$")
    final_state: FinalState
    duration_seconds: float = Field(ge=0)
    cost_usd: float = Field(ge=0)
    journey_results: list[JourneyResult] = Field(default_factory=list)
    events: list[TrajectoryEvent] = Field(default_factory=list)
    notes: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def events_are_ordered(self) -> Trajectory:
        previous_seq = -1
        previous_t = 0.0
        for event in self.events:
            if event.seq <= previous_seq:
                raise ValueError("event seq must be strictly increasing")
            if event.t_seconds < previous_t:
                raise ValueError("event t_seconds must be non-decreasing")
            if event.t_seconds > self.duration_seconds + 1e-9:
                raise ValueError("event t_seconds exceeds duration_seconds")
            previous_seq, previous_t = event.seq, event.t_seconds
        ids = [item.step_id for item in self.journey_results]
        if len(set(ids)) != len(ids):
            raise ValueError("journey_results contains duplicate step ids")
        return self

    def content_sha256(self) -> str:
        return sha256_hex(self.model_dump(mode="json"))
