"""Strict Pydantic v2 contracts for reusable pilots and shadow evaluations (HF-23-02)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

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
HashRef = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
]

_SECRET_PATTERN = re.compile(
    r"(?:postgres(?:ql)?://[^\s]+|(?:password|passwd|token|api[_-]?key|secret|credential|access[_-]?token)\s*[:=]\s*[^\s]+|sk-[A-Za-z0-9]{20,})",
    re.IGNORECASE,
)


def _looks_like_secret_value(value: str) -> bool:
    return bool(_SECRET_PATTERN.search(value))


class PilotContractModel(BaseModel):
    """Shared strict base model for reusable pilot contracts."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


PilotVerdict = Literal["promising", "inconclusive", "harmful", "invalid"]


class PilotSpec(PilotContractModel):
    """Frozen specification for a reusable evaluation pilot."""

    spec_id: Identifier
    hypothesis: ShortText
    target_population: ShortText
    unit_of_analysis: ShortText = "demand_version"
    candidate_version: ShortText
    baseline_version: ShortText
    questions_version: ShortText
    labels: list[str]
    exclusions: list[str] = Field(default_factory=list)
    oracle_source: ShortText
    primary_metric: str = "paired_stage_error_delta"
    secondary_metrics: list[str] = Field(
        default_factory=lambda: [
            "coverage",
            "cost_per_demand",
            "latency_p50_ms",
            "latency_p95_ms",
            "confusion_matrix",
        ]
    )
    sentinel_risks: list[str] = Field(
        default_factory=lambda: [
            "offline_leak",
            "fake_ready_for_spec",
            "role_switch",
            "out_of_catalog",
            "production_executor_mutation",
        ]
    )
    budget_limit_usd: float = Field(default=10.0, ge=0.0)
    sample_size_target: int = Field(default=20, ge=1)
    min_sample_size: int = Field(default=10, ge=1)
    alpha: float = Field(default=0.05, gt=0.0, lt=1.0)
    frozen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    stopping_rules: dict[str, Any] = Field(default_factory=dict)

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("labels list must contain at least one valid label")
        if len(v) != len(set(v)):
            raise ValueError("duplicate labels are not allowed")
        return v


class PilotObservation(PilotContractModel):
    """Per-case observation record from shadow or replay evaluation.

    Strictly forbids persisting raw task descriptions, prompts, diffs, tokens, or API secrets.
    """

    case_id: Identifier
    demand_hash: HashRef
    stratum: ShortText
    eligible: bool
    exclusion_reason: str | None = None
    candidate_prediction: dict[str, Any] | None = None
    baseline_prediction: dict[str, Any] | None = None
    effective_version: str | None = None
    latency_ms: float | None = Field(default=None, ge=0.0)
    cost_usd: float | None = Field(default=None, ge=0.0)
    error: str | None = None
    ground_truth: str | None = None
    provenance: dict[str, Any] | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("exclusion_reason", "error")
    @classmethod
    def reject_secrets_in_text(cls, v: str | None) -> str | None:
        if v and _looks_like_secret_value(v):
            raise ValueError("text fields cannot contain secret values")
        return v

    @model_validator(mode="after")
    def validate_observation_invariants(self) -> PilotObservation:
        # Check that candidate_prediction does not leak raw prompt text
        if self.candidate_prediction:
            for key in ("prompt", "task", "text", "raw_demand", "content"):
                if key in self.candidate_prediction:
                    raise ValueError(f"candidate_prediction must not leak raw input: {key}")
        # If not eligible, reason must be set
        if not self.eligible and not self.exclusion_reason:
            raise ValueError("ineligible observations must supply an exclusion_reason")
        return self


class PilotReport(PilotContractModel):
    """Statistical summary report produced by the PilotEvaluator."""

    spec_id: Identifier
    total_cases: int = Field(ge=0)
    eligible_cases: int = Field(ge=0)
    excluded_cases: int = Field(ge=0)
    unlabeled_cases: int = Field(ge=0)
    candidate_error_rate: float = Field(ge=0.0, le=1.0)
    baseline_error_rate: float = Field(ge=0.0, le=1.0)
    paired_delta: float = Field(ge=-1.0, le=1.0)
    ci_lower: float = Field(ge=-1.0, le=1.0)
    ci_upper: float = Field(ge=-1.0, le=1.0)
    confidence_level: float = Field(default=0.95, gt=0.0, lt=1.0)
    p_value: float | None = Field(default=None, ge=0.0, le=1.0)
    candidate_confusion: dict[str, dict[str, int]] = Field(default_factory=dict)
    baseline_confusion: dict[str, dict[str, int]] = Field(default_factory=dict)
    strata_breakdown: dict[str, dict[str, Any]] = Field(default_factory=dict)
    cost_total_usd: float = Field(default=0.0, ge=0.0)
    latency_p50_ms: float = Field(default=0.0, ge=0.0)
    latency_p95_ms: float = Field(default=0.0, ge=0.0)
    sentinel_violations: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    verdict: PilotVerdict
    verdict_reason: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
