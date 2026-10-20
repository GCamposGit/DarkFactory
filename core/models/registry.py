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
            for alias in entry.aliases:
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


_EMBEDDED_FALLBACK: dict[str, Any] = {
    "schema_version": 1,
    "quota_floor_percent": QUOTA_FLOOR_MIN,
    "forbidden_autonomous_markers": ["fable", "claude-fable", "astra", "gpt-6-astra"],
    "models": [
        {"id": "claude-opus-5-5", "provider": "anthropic", "family": "claude", "tier": "high", "aliases": ["opus"], "status": "active"},
        {"id": "claude-sonnet-5-5", "provider": "anthropic", "family": "claude", "tier": "mid", "aliases": ["sonnet"], "status": "active"},
        {"id": "claude-haiku-5-5", "provider": "anthropic", "family": "claude", "tier": "low", "aliases": ["haiku"], "status": "active"},
    ],
    "roles": {
        "subagent.haiku": "claude-haiku-5-5",
        "subagent.sonnet": "claude-sonnet-5-5",
        "subagent.opus": "claude-opus-5-5",
    },
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


def resolve(alias_or_role: str, registry: Optional[ModelRegistry] = None) -> str:
    """Resolve a role (``subagent.haiku``), alias (``haiku``) or exact id to an active model id.

    Raises ``KeyError`` when unknown or when the target is not routable (not active / forbidden).
    """
    reg = registry if registry is not None else get_registry()
    key = alias_or_role.strip()
    low = key.lower()
    if key in reg.roles:
        return reg.roles[key]
    for entry in reg.models:
        if low == entry.id.lower() or low in (a.strip().lower() for a in entry.aliases):
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
