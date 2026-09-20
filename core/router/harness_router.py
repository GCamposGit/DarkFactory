"""Modular Headless Harness Router for Dark Factory.

Provides pluggable routing policies to dispatch tasks to appropriate headless harnesses
(Codex, Grok Build, Antigravity, Claude Code, DeepSeek) while prioritizing $0 marginal cost
subscription quotas on on-premises and local hardware.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Canonical harness identifiers
HARNESS_CODEX = "codex"
HARNESS_GROK = "grok"
HARNESS_ANTIGRAVITY = "antigravity"
HARNESS_CLAUDE = "claude"
HARNESS_DEEPSEEK = "deepseek"

ALL_HARNESSES = [
    HARNESS_CODEX,
    HARNESS_GROK,
    HARNESS_ANTIGRAVITY,
    HARNESS_CLAUDE,
    HARNESS_DEEPSEEK,
]


@runtime_checkable
class HarnessRoutingPolicy(Protocol):
    """Pluggable contract for selecting and prioritizing headless harnesses."""

    def select_candidate_harnesses(
        self,
        stage: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[str]:
        """Return ordered list of candidate harness names to attempt for the given stage and context."""
        ...


class SpecializedCascadePolicy:
    """Routes by domain specialization with automatic fallback cascade.

    - Development / Code / Validation: Codex (primary) -> Grok -> Antigravity
    - Planning / Architecture / Research: Grok (primary) -> Codex -> Antigravity
    - Context / Memory / Evaluation / Default: Antigravity (primary) -> Codex -> Grok
    """

    def __init__(
        self,
        allowed_harnesses: list[str] | None = None,
    ) -> None:
        self.allowed_harnesses = allowed_harnesses or [
            HARNESS_CODEX,
            HARNESS_GROK,
            HARNESS_ANTIGRAVITY,
            HARNESS_CLAUDE,
            HARNESS_DEEPSEEK,
        ]

    def select_candidate_harnesses(
        self,
        stage: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[str]:
        meta = metadata or {}
        preferred = meta.get("preferred_harness") or meta.get("harness_preference")
        if preferred and preferred in self.allowed_harnesses:
            ordered = [preferred] + [h for h in self.allowed_harnesses if h != preferred]
            return ordered

        normalized_stage = stage.lower().strip()
        if normalized_stage in ("development", "validation", "integration", "build_deploy"):
            candidates = [HARNESS_CODEX, HARNESS_GROK, HARNESS_ANTIGRAVITY, HARNESS_CLAUDE, HARNESS_DEEPSEEK]
        elif normalized_stage in ("planning", "grill", "research", "architecture"):
            candidates = [HARNESS_GROK, HARNESS_CODEX, HARNESS_ANTIGRAVITY, HARNESS_CLAUDE, HARNESS_DEEPSEEK]
        else:
            candidates = [HARNESS_ANTIGRAVITY, HARNESS_CODEX, HARNESS_GROK, HARNESS_CLAUDE, HARNESS_DEEPSEEK]

        # Filter by allowed harnesses preserving order
        return [h for h in candidates if h in self.allowed_harnesses]


class UniversalCascadePolicy:
    """Strict fixed-order cascade across all stages."""

    def __init__(self, cascade_order: list[str] | None = None) -> None:
        self.cascade_order = cascade_order or [
            HARNESS_CODEX,
            HARNESS_GROK,
            HARNESS_ANTIGRAVITY,
            HARNESS_CLAUDE,
            HARNESS_DEEPSEEK,
        ]

    def select_candidate_harnesses(
        self,
        stage: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[str]:
        meta = metadata or {}
        preferred = meta.get("preferred_harness") or meta.get("harness_preference")
        if preferred and preferred in self.cascade_order:
            return [preferred] + [h for h in self.cascade_order if h != preferred]
        return list(self.cascade_order)


class ExplicitChoicePolicy:
    """Forces a specific harness or defaults to specialized cascade."""

    def __init__(self, primary_harness: str, fallback_policy: HarnessRoutingPolicy | None = None) -> None:
        self.primary_harness = primary_harness
        self.fallback_policy = fallback_policy or SpecializedCascadePolicy()

    def select_candidate_harnesses(
        self,
        stage: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[str]:
        fallbacks = self.fallback_policy.select_candidate_harnesses(stage, metadata)
        return [self.primary_harness] + [h for h in fallbacks if h != self.primary_harness]


_POLICY_REGISTRY: dict[str, type] = {
    "specialized": SpecializedCascadePolicy,
    "universal": UniversalCascadePolicy,
    "explicit": ExplicitChoicePolicy,
}


def register_harness_routing_policy(name: str, policy_cls: type) -> None:
    """Register custom routing policy class."""
    _POLICY_REGISTRY[name.lower()] = policy_cls


def get_harness_routing_policy(
    policy_type: str = "specialized",
    **kwargs: Any,
) -> HarnessRoutingPolicy:
    """Factory returning configured harness routing policy instance."""
    normalized = policy_type.lower().strip()
    cls = _POLICY_REGISTRY.get(normalized)
    if cls is None:
        logger.warning(
            "Unknown harness routing policy '%s'; falling back to 'specialized'",
            policy_type,
        )
        cls = SpecializedCascadePolicy
    return cls(**kwargs)


def resolve_harness_candidates(
    stage: str,
    metadata: dict[str, Any] | None = None,
    policy_type: str = "specialized",
) -> list[str]:
    """Convenience helper to resolve candidate harnesses directly."""
    policy = get_harness_routing_policy(policy_type)
    return policy.select_candidate_harnesses(stage, metadata)
