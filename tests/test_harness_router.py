"""Unit tests for the modular headless harness router (HF-07-02)."""

from __future__ import annotations

import pytest

from core.router.harness_router import (
    HARNESS_ANTIGRAVITY,
    HARNESS_CLAUDE,
    HARNESS_CODEX,
    HARNESS_DEEPSEEK,
    HARNESS_GROK,
    ExplicitChoicePolicy,
    SpecializedCascadePolicy,
    UniversalCascadePolicy,
    get_harness_routing_policy,
    register_harness_routing_policy,
    resolve_harness_candidates,
)


def test_specialized_cascade_development_stage():
    """Development stages must prioritize Codex for code and tests."""
    policy = SpecializedCascadePolicy()
    candidates = policy.select_candidate_harnesses("development")
    assert candidates[0] == HARNESS_CODEX
    assert HARNESS_GROK in candidates
    assert HARNESS_ANTIGRAVITY in candidates


def test_specialized_cascade_planning_stage():
    """Planning and research stages must prioritize Grok for architecture and web search."""
    policy = SpecializedCascadePolicy()
    candidates = policy.select_candidate_harnesses("planning")
    assert candidates[0] == HARNESS_GROK
    assert HARNESS_CODEX in candidates


def test_specialized_cascade_synthesis_default_stage():
    """Synthesis / evaluation stages must prioritize Antigravity."""
    policy = SpecializedCascadePolicy()
    candidates = policy.select_candidate_harnesses("memory_observation")
    assert candidates[0] == HARNESS_ANTIGRAVITY


def test_specialized_cascade_preferred_override():
    """Explicit preferred_harness in metadata must take priority."""
    policy = SpecializedCascadePolicy()
    candidates = policy.select_candidate_harnesses(
        "development",
        metadata={"preferred_harness": HARNESS_GROK},
    )
    assert candidates[0] == HARNESS_GROK


def test_universal_cascade_policy():
    """Universal policy maintains fixed order regardless of stage."""
    policy = UniversalCascadePolicy(cascade_order=[HARNESS_CODEX, HARNESS_GROK, HARNESS_ANTIGRAVITY])
    assert policy.select_candidate_harnesses("planning") == [HARNESS_CODEX, HARNESS_GROK, HARNESS_ANTIGRAVITY]
    assert policy.select_candidate_harnesses("development") == [HARNESS_CODEX, HARNESS_GROK, HARNESS_ANTIGRAVITY]


def test_explicit_choice_policy():
    """Explicit choice policy forces primary harness and uses fallback."""
    policy = ExplicitChoicePolicy(primary_harness=HARNESS_CLAUDE)
    candidates = policy.select_candidate_harnesses("development")
    assert candidates[0] == HARNESS_CLAUDE
    assert HARNESS_CODEX in candidates


def test_factory_and_registry():
    """get_harness_routing_policy returns correct policy instances."""
    p_spec = get_harness_routing_policy("specialized")
    assert isinstance(p_spec, SpecializedCascadePolicy)

    p_univ = get_harness_routing_policy("universal")
    assert isinstance(p_univ, UniversalCascadePolicy)

    # Unknown defaults to specialized
    p_unknown = get_harness_routing_policy("nonexistent")
    assert isinstance(p_unknown, SpecializedCascadePolicy)


def test_resolve_harness_candidates_helper():
    """Convenience helper returns ordered list."""
    candidates = resolve_harness_candidates("development")
    assert candidates[0] == HARNESS_CODEX
