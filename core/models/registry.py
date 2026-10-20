"""Single model registry API (USR-185, slice 1).

Reads ``.factory/config/model_registry.json`` (validated, cached) and exposes ``resolve`` / ``list_candidates``.
When the file is missing or invalid, an embedded fallback (the Claude 5.5 family) is used, fail-closed.

Invariants enforced by the validator (the protected rules, now testable):
* a forbidden-autonomous model (fable, astra, ...) can never be bound to a role;
* the reviewer of a review pair must be of a different family than the implementer;
* a role can only route to a model whose status is ``active`` (approved qualification);
* the quota floor can never be below 15%.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY_PATH = REPO_ROOT / ".factory" / "config" / "model_registry.json"
QUOTA_FLOOR_MIN = 15.0

ModelStatus = Literal["candidate", "active", "deprecated", "retired"]


class ModelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    family: str = Field(min_length=1)
    tier: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    gateway_ids: list[str] = Field(default_factory=list)  # provider/gateway-prefixed ids (e.g. OpenRouter); naming only
    status: ModelStatus
    price_input_per_mtok: Optional[float] = Field(default=None, ge=0)
    price_output_per_mtok: Optional[float] = Field(default=None, ge=0)
    context_tokens: Optional[int] = Field(default=None, gt=0)
    max_output_tokens: Optional[int] = Field(default=None, gt=0)
    capabilities: list[str] = Field(default_factory=list)
    forbidden_autonomous: bool = False
    release_date: Optional[str] = None
    retirement_date: Optional[str] = None


class ReviewPair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    implementer: str
    reviewer: str


class ModelRegistry(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: int = 1
    quota_floor_percent: float = QUOTA_FLOOR_MIN
    forbidden_autonomous_markers: list[str] = Field(default_factory=list)
    models: list[ModelEntry]
    roles: dict[str, str] = Field(default_factory=dict)
    review_pairs: list[ReviewPair] = Field(default_factory=list)

    @model_validator(mode="after")
    def _invariants(self) -> "ModelRegistry":
        if self.quota_floor_percent < QUOTA_FLOOR_MIN:
            raise ValueError(f"quota_floor_percent must be >= {QUOTA_FLOOR_MIN}")
        by_id: dict[str, ModelEntry] = {}
        names: set[str] = set()
        for entry in self.models:
            if entry.id in by_id:
                raise ValueError(f"duplicate model id {entry.id!r}")
            by_id[entry.id] = entry
        for entry in self.models:
            for alias in [*entry.aliases, *entry.gateway_ids]:
                key = alias.strip().lower()
                if key in names or key in by_id:
                    raise ValueError(f"alias {alias!r} collides with another alias or model id")
                names.add(key)
        markers = {m.strip().lower() for m in self.forbidden_autonomous_markers if m.strip()}
        for role, model_id in self.roles.items():
            entry = by_id.get(model_id)
            if entry is None:
                raise ValueError(f"role {role!r} points to unknown model {model_id!r}")
            if entry.forbidden_autonomous or any(mk in model_id.lower() for mk in markers):
                raise ValueError(f"role {role!r} routes to forbidden autonomous model {model_id!r}")
            if entry.status != "active":
                raise ValueError(
                    f"role {role!r} routes to {model_id!r} without approved qualification (status={entry.status})"
                )
        for pair in self.review_pairs:
            for side in (pair.implementer, pair.reviewer):
                if side not in self.roles:
                    raise ValueError(f"review pair references unknown role {side!r}")
            impl = by_id[self.roles[pair.implementer]]
            rev = by_id[self.roles[pair.reviewer]]
            if impl.family == rev.family:
                raise ValueError(
                    f"reviewer {pair.reviewer!r} is the same family as implementer {pair.implementer!r}"
                )
        return self


def parse_registry(raw: Any) -> ModelRegistry:
    """Validate a raw JSON document. Raises ``ValidationError`` on violation."""
    return ModelRegistry.model_validate(raw)


def _fb(model_id: str, provider: str, family: str, tier: str, aliases: list[str], status: str = "active", **extra: Any) -> dict[str, Any]:
    return {"id": model_id, "provider": provider, "family": family, "tier": tier, "aliases": aliases, "status": status, **extra}


# Fail-closed copy of the seeded registry (a test pins that every alias/role resolves to the same id as the file).
_EMBEDDED_FALLBACK: dict[str, Any] = {
    "schema_version": 1,
    "quota_floor_percent": QUOTA_FLOOR_MIN,
    "forbidden_autonomous_markers": ["fable", "claude-fable", "astra", "gpt-6-astra"],
    "models": [
        _fb("claude-opus-5-5", "anthropic", "claude", "high", ["opus"]),
        _fb("claude-sonnet-5-5", "anthropic", "claude", "mid", ["sonnet"]),
        _fb("claude-haiku-5-5", "anthropic", "claude", "low", ["haiku"]),
        _fb("claude-fable-5-1", "anthropic", "claude", "frontier", ["fable"], "candidate", forbidden_autonomous=True),
        _fb("claude-opus-5", "anthropic", "claude", "high", ["opus-legacy"], "deprecated", gateway_ids=["anthropic/claude-opus-5"]),
        _fb("gpt-6-luna", "openai", "openai", "mid", ["luna"]),
        _fb("gpt-6-astra", "openai", "openai", "frontier", ["astra"], "candidate", forbidden_autonomous=True, gateway_ids=["openai/gpt-6-astra"]),
        _fb("gpt-sol", "openai", "openai", "high", ["sol"]),
        _fb("grok-4.6", "xai", "grok", "mid", ["grok-review"], gateway_ids=["xai/grok-4.6"]),
        _fb("grok-4.7", "xai", "grok", "mid", ["grok-dev"]),
        _fb("gemini-3.8-flash", "google", "gemini", "low", ["flash"], gateway_ids=["google/gemini-3.8-flash"]),
        _fb("deepseek/deepseek-v4.1-flash", "deepseek", "deepseek", "low", ["deepseek-flash"]),
        _fb("deepseek/deepseek-v4-pro", "deepseek", "deepseek", "high", ["deepseek-v4-pro"], "candidate"),
        _fb("gemini-3.1-pro", "google", "gemini", "high", ["gemini-pro"], "candidate", gateway_ids=["google/gemini-3.1-pro"]),
        _fb("gemini-3.1-flash-lite-image", "google", "gemini", "low", ["gemini-image-fast"], "candidate", gateway_ids=["google/gemini-3.1-flash-lite-image"]),
        _fb("gpt-image-2", "openai", "openai", "high", ["gpt-image"], "candidate", gateway_ids=["openai/gpt-image-2"]),
        _fb("claude-3-7-sonnet", "anthropic", "claude", "mid", ["claude-legacy-sonnet"], "deprecated", gateway_ids=["anthropic/claude-3.7-sonnet"]),
        _fb("deepseek/deepseek-r1", "deepseek", "deepseek", "high", ["deepseek-r1"], "deprecated"),
        _fb("gemini-2.0-flash-001", "google", "gemini", "low", ["gemini-legacy-flash"], "deprecated", gateway_ids=["google/gemini-2.0-flash-001"]),
    ],
    "roles": {
        "subagent.haiku": "claude-haiku-5-5",
        "subagent.sonnet": "claude-sonnet-5-5",
        "subagent.opus": "claude-opus-5-5",
        "development.claude": "claude-sonnet-5-5",
        "development.codex": "gpt-6-luna",
        "development.grok": "grok-4.7",
        "planning.claude": "claude-opus-5-5",
        "review.codex": "gpt-6-luna",
        "review.grok": "grok-4.6",
        "distill.openrouter": "deepseek/deepseek-v4.1-flash",
    },
    "review_pairs": [
        {"implementer": "development.claude", "reviewer": "review.codex"},
        {"implementer": "development.codex", "reviewer": "review.grok"},
        {"implementer": "development.grok", "reviewer": "review.codex"},
    ],
}


def embedded_registry() -> ModelRegistry:
    return parse_registry(_EMBEDDED_FALLBACK)


def load_registry(path: Optional[Union[str, Path]] = None) -> ModelRegistry:
    """Load and validate the registry; embedded fallback when missing or invalid (logged)."""
    target = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        return parse_registry(raw)
    except FileNotFoundError:
        logger.warning("model registry not found at %s; using embedded fallback", target)
    except (OSError, ValueError, TypeError, ValidationError) as exc:
        logger.warning("model registry at %s is invalid (%s); using embedded fallback", target, exc)
    return embedded_registry()


@lru_cache(maxsize=1)
def _cached_default() -> ModelRegistry:
    return load_registry()


def get_registry(path: Optional[Union[str, Path]] = None) -> ModelRegistry:
    """Cached default registry; an explicit ``path`` bypasses the cache."""
    return load_registry(path) if path is not None else _cached_default()


def clear_cache() -> None:
    _cached_default.cache_clear()


def _find(reg: ModelRegistry, name: str) -> Optional[ModelEntry]:
    low = name.strip().lower()
    for entry in reg.models:
        if low == entry.id.lower() or low in (a.strip().lower() for a in [*entry.aliases, *entry.gateway_ids]):
            return entry
    return None


def resolve(alias_or_role: str, registry: Optional[ModelRegistry] = None) -> str:
    """Resolve a role (``subagent.haiku``), alias (``haiku``) or exact id to an active model id.

    Raises ``KeyError`` when unknown or when the target is not routable (not active / forbidden).
    """
    reg = registry if registry is not None else get_registry()
    key = alias_or_role.strip()
    low = key.lower()
    if key in reg.roles:
        return reg.roles[key]
    entry = _find(reg, key)
    if entry is not None:
        if entry.status != "active" or entry.forbidden_autonomous:
            raise KeyError(
                f"{alias_or_role!r} resolves to {entry.id!r}, which is not routable (status={entry.status})"
            )
        return entry.id
    raise KeyError(f"unknown model alias or role: {alias_or_role!r}")


def list_candidates(registry: Optional[ModelRegistry] = None) -> list[ModelEntry]:
    """Models in ``candidate`` status (awaiting qualification)."""
    reg = registry if registry is not None else get_registry()
    return [entry for entry in reg.models if entry.status == "candidate"]


def resolve_known(alias_or_role: str, registry: Optional[ModelRegistry] = None) -> str:
    """Like ``resolve`` but also accepts ``deprecated`` models (legacy readers that still route to them).

    Still raises ``KeyError`` for unknown names, ``candidate``/``retired`` models and forbidden-autonomous ones.
    """
    reg = registry if registry is not None else get_registry()
    key = alias_or_role.strip()
    if key in reg.roles:
        return reg.roles[key]
    entry = _find(reg, key)
    if entry is not None:
        if entry.status not in ("active", "deprecated") or entry.forbidden_autonomous:
            raise KeyError(
                f"{alias_or_role!r} resolves to {entry.id!r}, which is not routable (status={entry.status})"
            )
        return entry.id
    raise KeyError(f"unknown model alias or role: {alias_or_role!r}")


def forbidden_markers(registry: Optional[ModelRegistry] = None) -> list[str]:
    """Substrings that mark a model as never autonomous (fable, astra, ...), from the registry."""
    reg = registry if registry is not None else get_registry()
    return list(reg.forbidden_autonomous_markers)


def id_of(alias_or_id: str, registry: Optional[ModelRegistry] = None) -> str:
    """Canonical id for a name regardless of status (naming only, NOT a routability check)."""
    reg = registry if registry is not None else get_registry()
    entry = _find(reg, alias_or_id)
    if entry is not None:
        return entry.id
    raise KeyError(f"unknown model alias or id: {alias_or_id!r}")


def gateway_id(alias_or_id: str, registry: Optional[ModelRegistry] = None) -> str:
    """Provider/gateway-prefixed id (e.g. ``anthropic/claude-opus-5``) for a name, regardless of status.

    Falls back to the canonical id when the model has no gateway id. Naming only, NOT a routability check.
    """
    reg = registry if registry is not None else get_registry()
    entry = _find(reg, alias_or_id)
    if entry is None:
        raise KeyError(f"unknown model alias or id: {alias_or_id!r}")
    return entry.gateway_ids[0] if entry.gateway_ids else entry.id
