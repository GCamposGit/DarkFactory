"""Typed configuration and evidence contracts for the validation harness."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _validate_hex(value: str, *, minimum: int, maximum: int, label: str) -> str:
    normalized = value.strip().lower()
    if not minimum <= len(normalized) <= maximum or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{label} must be hexadecimal with length {minimum}-{maximum}")
    return normalized


class HarnessStepConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    cmd: str = Field(min_length=1)
    quick: bool = False
    holdout: bool = False
    timeout_sec: int = Field(default=120, ge=1, le=3600)
    kind: Literal["test", "check"] | None = None


class HarnessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    steps: list[HarnessStepConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_step_names(self) -> "HarnessConfig":
        names = [step.name for step in self.steps]
        if len(names) != len(set(names)):
            raise ValueError("Harness step names must be unique")
        return self


class HarnessResult(BaseModel):
    """Supervisor-produced evidence bound to a commit and validated config."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    candidate_sha: str
    config_hash: str
    required_steps: list[str] = Field(min_length=1)
    started_steps: list[str] = Field(min_length=1)
    passed_steps: list[str] = Field(default_factory=list)
    failed_steps: list[str] = Field(default_factory=list)
    discovered_count: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    exit_codes: dict[str, int]
    artifact_refs: list[str] = Field(default_factory=list)
    reused_from: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Present only on a cache-hit verdict reuse: origin host, original "
            "candidate_sha, and age of the cached PASS being replayed for this "
            "candidate. Absent on every freshly executed run, so existing "
            "consumers of HARNESS_RESULT that never look at this field are "
            "unaffected."
        ),
    )

    @field_validator("candidate_sha")
    @classmethod
    def validate_candidate_sha(cls, value: str) -> str:
        return _validate_hex(value, minimum=7, maximum=64, label="candidate_sha")

    @field_validator("config_hash")
    @classmethod
    def validate_config_hash(cls, value: str) -> str:
        return _validate_hex(value, minimum=64, maximum=64, label="config_hash")
