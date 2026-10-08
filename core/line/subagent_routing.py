"""Model policy for development SUBAGENTS of the harness (USR-168).

Scope: which Anthropic model (``haiku`` | ``sonnet`` | ``opus``) a development subagent (the Agent tool with
``model: haiku|sonnet|opus``) must use. It does NOT touch the production-line cascade
(`core.line.routing.pick` / `.factory/config/line_routing.json`); that stays the single source for the line.

Policy (documented in skills 03-model-router and 19-run-ticket, section "Subagentes de desenvolvimento"):

* Haiku (``claude-haiku-5-5``) is eligible only when EVERY condition holds: the task type is one of the
  mechanical kinds in the config, the complexity is allowed for that kind, at most ``max_files`` files and
  ``max_changed_lines`` lines are touched, a focused executable acceptance check exists, the ambiguity (Gate G1) is
  resolved and Haiku has not failed on this task before.
* Haiku is forbidden for governance-protected paths (`core.orchestrator.guard.audit_paths`), risk tags, and the
  stages planning/grill/review/integration. Everything non-eligible goes to Sonnet; ``planning`` goes to Opus.
* One Haiku attempt only: on a failed focused gate or a correctness defect found in review, retry with Sonnet.
  Haiku never escalates to Opus/Fable by itself and models in ``forbidden_autonomous_models`` are never returned.
* Fail-closed: any invalid input or invalid config resolves to Sonnet / the embedded defaults.

The 15% quota floor, the "reviewer is never the implementer family" rule and the single gate
(`runner.py --quick`) are unchanged invariants and live elsewhere.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator, model_validator

from core.orchestrator.guard import audit_paths

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / ".factory" / "config" / "subagent_model_routing.json"

ModelAlias = Literal["haiku", "sonnet", "opus"]
Complexity = Literal["low", "medium", "high", "critical"]

_ALIASES: tuple[str, ...] = ("haiku", "sonnet", "opus")

# Reason codes (stable identifiers used in decisions, tests and logs).
HAIKU_ELIGIBLE = "HAIKU_ELIGIBLE"
OPUS_STAGE = "OPUS_STAGE"
STAGE_FORBIDDEN = "STAGE_FORBIDDEN"
PROTECTED_PATH = "PROTECTED_PATH"
RISK_TAG = "RISK_TAG"
TASK_TYPE_NOT_ELIGIBLE = "TASK_TYPE_NOT_ELIGIBLE"
COMPLEXITY_NOT_ALLOWED = "COMPLEXITY_NOT_ALLOWED"
TOO_MANY_FILES = "TOO_MANY_FILES"
TOO_MANY_LINES = "TOO_MANY_LINES"
NO_EXECUTABLE_ACCEPTANCE = "NO_EXECUTABLE_ACCEPTANCE"
AMBIGUITY_PENDING = "AMBIGUITY_PENDING"
PRIOR_HAIKU_FAILURE = "PRIOR_HAIKU_FAILURE"
INVALID_INPUT = "INVALID_INPUT"


class SubagentRoutingConfig(BaseModel):
    """Validated content of `.factory/config/subagent_model_routing.json`."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    models: dict[str, str]
    max_files: int = Field(ge=0)
    max_changed_lines: int = Field(ge=0)
    max_haiku_attempts: int = Field(ge=0)
    eligible_task_types: dict[str, list[str]]
    forbidden_tags: list[str]
    forbidden_stages: list[str]
    opus_stages: list[str]
    forbidden_autonomous_models: list[str]

    @field_validator("models")
    @classmethod
    def _models_complete(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) != set(_ALIASES):
            raise ValueError(f"models must define exactly {list(_ALIASES)}")
        for alias, model_id in value.items():
            if not isinstance(model_id, str) or not model_id.strip():
                raise ValueError(f"model id for {alias} must be a non-empty string")
        return {alias: value[alias].strip() for alias in _ALIASES}

    @field_validator("eligible_task_types")
    @classmethod
    def _eligible_valid(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        allowed = {"low", "medium", "high", "critical"}
        normalized: dict[str, list[str]] = {}
        for task_type, complexities in value.items():
            bad = [c for c in complexities if c not in allowed]
            if bad or not complexities:
                raise ValueError(f"invalid complexities for {task_type}: {complexities}")
            normalized[task_type.strip().lower()] = list(complexities)
        return normalized

    @field_validator("forbidden_tags", "forbidden_stages", "opus_stages", "forbidden_autonomous_models")
    @classmethod
    def _lower_unique(cls, value: list[str]) -> list[str]:
        return sorted({item.strip().lower() for item in value if item.strip()})

    @model_validator(mode="after")
    def _deny_lists_not_relaxed(self) -> "SubagentRoutingConfig":
        # A config can tighten the deny lists but never relax the built-in ones.
        for field_name, builtin in (
            ("forbidden_tags", _DEFAULT_FORBIDDEN_TAGS),
            ("forbidden_stages", _DEFAULT_FORBIDDEN_STAGES),
            ("forbidden_autonomous_models", _DEFAULT_FORBIDDEN_MODELS),
        ):
            missing = set(builtin) - set(getattr(self, field_name))
            if missing:
                raise ValueError(f"{field_name} must keep built-in entries: {sorted(missing)}")
        forbidden = set(self.forbidden_autonomous_models)
        for alias, model_id in self.models.items():
            if _is_forbidden_model(model_id, forbidden):
                raise ValueError(f"model {alias}={model_id!r} is a forbidden autonomous model")
        overlap = set(self.opus_stages) & {"review", "integration"}
        if overlap:
            raise ValueError(f"opus_stages cannot include {sorted(overlap)}")
        return self


_DEFAULT_FORBIDDEN_TAGS: tuple[str, ...] = (
    "architecture",
    "auth",
    "concurrency",
    "credentials",
    "data_deletion",
    "flaky_test",
    "locking",
    "migration",
    "payments",
    "public_contract",
    "quota",
    "root_cause_debug",
    "routing",
    "security",
    "transactions",
)
_DEFAULT_FORBIDDEN_STAGES: tuple[str, ...] = ("grill", "integration", "planning", "review")
_DEFAULT_FORBIDDEN_MODELS: tuple[str, ...] = ("astra", "claude-fable", "fable", "gpt-6-astra")


def _is_forbidden_model(model_id: str, forbidden: Union[set[str], frozenset[str]]) -> bool:
    lowered = model_id.strip().lower()
    return any(entry and entry in lowered for entry in forbidden)


def default_config() -> SubagentRoutingConfig:
    """Embedded defaults (identical to the committed JSON; a test pins the equality)."""
    return SubagentRoutingConfig(
        models={"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"},
        max_files=3,
        max_changed_lines=150,
        max_haiku_attempts=1,
        eligible_task_types={
            "mechanical_edit": ["low"],
            "docs_sync": ["low", "medium"],
            "test_from_spec": ["low"],
            "config_data": ["low"],
            "boilerplate_from_template": ["low"],
            "test_run_distill": ["low", "medium"],
            "ledger_update": ["low"],
        },
        forbidden_tags=list(_DEFAULT_FORBIDDEN_TAGS),
        forbidden_stages=list(_DEFAULT_FORBIDDEN_STAGES),
        opus_stages=["planning"],
        forbidden_autonomous_models=list(_DEFAULT_FORBIDDEN_MODELS),
    )


def parse_config(raw: Mapping[str, Any]) -> SubagentRoutingConfig:
    """Build the config from the JSON document shape (``models`` + nested ``limits``). Raises on invalid input."""
    limits = raw.get("limits")
    if not isinstance(limits, Mapping):
        raise ValueError("limits must be an object")
    flat: dict[str, Any] = {
        "models": raw.get("models"),
        "max_files": limits.get("max_files"),
        "max_changed_lines": limits.get("max_changed_lines"),
        "max_haiku_attempts": limits.get("max_haiku_attempts"),
        "eligible_task_types": raw.get("eligible_task_types"),
        "forbidden_tags": raw.get("forbidden_tags"),
        "forbidden_stages": raw.get("forbidden_stages"),
        "opus_stages": raw.get("opus_stages"),
        "forbidden_autonomous_models": raw.get("forbidden_autonomous_models"),
    }
    return SubagentRoutingConfig.model_validate(flat)


def load_subagent_routing_config(path: Optional[Union[str, Path]] = None) -> SubagentRoutingConfig:
    """Load the routing config; fall back to the embedded defaults (with a log) when absent or invalid."""
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("top-level JSON must be an object")
        return parse_config(raw)
    except FileNotFoundError:
        logger.warning("subagent routing config not found at %s; using embedded defaults", target)
    except (OSError, ValueError, TypeError, ValidationError) as exc:
        logger.warning("subagent routing config at %s is invalid (%s); using embedded defaults", target, exc)
    return default_config()


class SubagentTask(BaseModel):
    """Description of a unit of work to be delegated to a development subagent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    complexity: Complexity
    files: list[str] = Field(default_factory=list)
    expected_changed_lines: StrictInt = Field(ge=0)
    has_executable_acceptance: StrictBool
    ambiguity_resolved: StrictBool
    tags: list[str] = Field(default_factory=list)
    previous_haiku_failures: StrictInt = Field(default=0, ge=0)

    @field_validator("stage", "task_type")
    @classmethod
    def _normalize_key(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, value: list[str]) -> list[str]:
        return sorted({tag.strip().lower() for tag in value if tag.strip()})


class SubagentModelDecision(BaseModel):
    """Result of the policy: which model the subagent must use and why."""

    model_config = ConfigDict(frozen=True)

    model: ModelAlias
    model_id: str
    reason_codes: list[str]
    escalate_to: Optional[ModelAlias] = None
    eligible_for_haiku: bool


def _decision(
    config: SubagentRoutingConfig,
    alias: ModelAlias,
    reasons: list[str],
    *,
    eligible: bool,
    escalate_to: Optional[ModelAlias] = None,
) -> SubagentModelDecision:
    model_id = config.models[alias]
    if _is_forbidden_model(model_id, frozenset(config.forbidden_autonomous_models)):
        # Unreachable with a validated config; kept as a last line of defence.
        logger.error("model %s resolved to a forbidden id; forcing embedded sonnet", alias)
        fallback = default_config()
        return SubagentModelDecision(
            model="sonnet",
            model_id=fallback.models["sonnet"],
            reason_codes=[*reasons, INVALID_INPUT],
            escalate_to=None,
            eligible_for_haiku=False,
        )
    return SubagentModelDecision(
        model=alias,
        model_id=model_id,
        reason_codes=reasons,
        escalate_to=escalate_to,
        eligible_for_haiku=eligible,
    )


def pick_subagent_model(
    task: Union[SubagentTask, Mapping[str, Any]],
    config: Optional[SubagentRoutingConfig] = None,
) -> SubagentModelDecision:
    """Pure policy: decide the model alias for a development subagent. No I/O.

    ``config`` defaults to the embedded defaults (use `load_subagent_routing_config` to read the JSON).
    Invalid input (including a mapping that does not validate as `SubagentTask`) resolves to Sonnet.
    """
    cfg = config if config is not None else default_config()

    if isinstance(task, SubagentTask):
        parsed: Optional[SubagentTask] = task
    else:
        try:
            parsed = SubagentTask.model_validate(task)
        except (ValidationError, TypeError, ValueError) as exc:
            logger.warning("invalid subagent task (%s); failing closed to sonnet", exc)
            parsed = None
    if parsed is None:
        return _decision(cfg, "sonnet", [INVALID_INPUT], eligible=False)

    reasons: list[str] = []

    if parsed.stage in cfg.opus_stages:
        return _decision(cfg, "opus", [OPUS_STAGE, STAGE_FORBIDDEN], eligible=False)

    if parsed.stage in cfg.forbidden_stages:
        reasons.append(STAGE_FORBIDDEN)
    if audit_paths(parsed.files):
        reasons.append(PROTECTED_PATH)
    if set(parsed.tags) & set(cfg.forbidden_tags):
        reasons.append(RISK_TAG)

    allowed_complexities = cfg.eligible_task_types.get(parsed.task_type)
    if allowed_complexities is None:
        reasons.append(TASK_TYPE_NOT_ELIGIBLE)
    elif parsed.complexity not in allowed_complexities:
        reasons.append(COMPLEXITY_NOT_ALLOWED)

    if len(parsed.files) > cfg.max_files:
        reasons.append(TOO_MANY_FILES)
    if parsed.expected_changed_lines > cfg.max_changed_lines:
        reasons.append(TOO_MANY_LINES)
    if not parsed.has_executable_acceptance:
        reasons.append(NO_EXECUTABLE_ACCEPTANCE)
    if not parsed.ambiguity_resolved:
        reasons.append(AMBIGUITY_PENDING)
    if parsed.previous_haiku_failures > 0 or parsed.previous_haiku_failures >= cfg.max_haiku_attempts:
        reasons.append(PRIOR_HAIKU_FAILURE)

    if reasons:
        return _decision(cfg, "sonnet", reasons, eligible=False)

    # One Haiku attempt: a failed focused gate or a review-found correctness defect escalates to Sonnet.
    return _decision(cfg, "haiku", [HAIKU_ELIGIBLE], eligible=True, escalate_to="sonnet")
