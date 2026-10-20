"""USR-185 slice 2: migrated readers resolve the SAME ids as the literals they replaced (before/after table)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.line import agent_cli, routing, subagent_routing
from core.models import registry
from core.portfolio import router_optimizer
from core.router import model_router
from core.usage import adapters

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_embedded_fallback_matches_registry_file() -> None:
    file_reg = registry.load_registry()
    emb = registry.embedded_registry()
    assert emb.roles == file_reg.roles
    assert {m.id for m in emb.models} == {m.id for m in file_reg.models}
    assert emb.review_pairs == file_reg.review_pairs
    assert emb.forbidden_autonomous_markers == file_reg.forbidden_autonomous_markers
    for entry in file_reg.models:
        twin = next(m for m in emb.models if m.id == entry.id)
        assert (twin.aliases, twin.gateway_ids, twin.status, twin.family, twin.forbidden_autonomous) == (
            entry.aliases,
            entry.gateway_ids,
            entry.status,
            entry.family,
            entry.forbidden_autonomous,
        )


def test_resolve_known_accepts_deprecated_but_not_forbidden_or_candidate() -> None:
    assert registry.resolve_known("opus-legacy") == "claude-opus-5"
    with pytest.raises(KeyError):
        registry.resolve("opus-legacy")
    for blocked in ("fable", "astra"):
        with pytest.raises(KeyError):
            registry.resolve_known(blocked)
    assert registry.id_of("astra") == "gpt-6-astra"


def test_routing_defaults_and_pins_unchanged() -> None:
    assert routing._DEFAULT_FORBIDDEN_AUTONOMOUS_MODELS == ["fable", "claude-fable", "astra", "gpt-6-astra"]
    default = routing._default_routing_config()
    pins = default.stages["development"].harness_pins
    assert (pins["codex"].model, pins["codex"].effort) == ("gpt-6-luna", "high")
    assert (pins["grok"].model, pins["grok"].effort) == ("grok-4.7", "high")
    assert pins["claude"].model == "sonnet"
    assert default.stages["review"].openrouter_model == "deepseek/deepseek-v4.1-flash"


def test_line_routing_json_resolves_to_same_values_as_defaults() -> None:
    cfg = routing.load_routing_config(REPO_ROOT / ".factory" / "config" / "line_routing.json")
    assert cfg.forbidden_autonomous_models == ["fable", "claude-fable", "astra", "gpt-6-astra"]
    dev = cfg.stages["development"]
    assert dev.harness_pins["codex"].model == "gpt-6-luna"
    assert dev.harness_pins["grok"].model == "grok-4.7"
    for stage in cfg.stages.values():
        assert stage.openrouter_model == "deepseek/deepseek-v4.1-flash"
    assert agent_cli._DEFAULT_OPENROUTER_MODEL == "deepseek/deepseek-v4.1-flash"


def test_role_reference_only_for_registry_roles() -> None:
    assert routing.HarnessPin(model="development.codex").model == "gpt-6-luna"
    assert routing.HarnessPin(model="sonnet").model == "sonnet"  # CLI alias untouched
    assert routing.HarnessPin(model="some/other-model").model == "some/other-model"


def test_subagent_config_json_has_no_literal_models_and_equals_defaults() -> None:
    raw = json.loads((REPO_ROOT / ".factory" / "config" / "subagent_model_routing.json").read_text(encoding="utf-8"))
    assert "models" not in raw
    cfg = subagent_routing.parse_config(raw)
    assert cfg.models == {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
    assert cfg == subagent_routing.default_config()
    assert subagent_routing._DEFAULT_FORBIDDEN_MODELS == ("astra", "claude-fable", "fable", "gpt-6-astra")


def test_subagent_legacy_models_key_still_honored() -> None:
    raw = json.loads((REPO_ROOT / ".factory" / "config" / "subagent_model_routing.json").read_text(encoding="utf-8"))
    raw["models"] = {"haiku": "h", "sonnet": "s", "opus": "o"}
    assert subagent_routing.parse_config(raw).models == {"haiku": "h", "sonnet": "s", "opus": "o"}


def test_model_router_ids_unchanged() -> None:
    mm = model_router.MODEL_MATRIX
    assert mm["research"]["primary"] == "gemini-3.8-flash"
    assert mm["research"]["secondary"] == "grok-4.6"
    assert mm["code_scout"]["secondary"] == "claude-opus-5"
    assert mm["architecture"]["secondary"] == "gpt-sol"
    assert mm["coding"]["medium_complexity"]["secondary"] == "deepseek/deepseek-v4.1-flash"
    assert mm["adversarial_review"]["tier2_alt"] == "grok-4.6"
    assert model_router.HIGH_INTELLIGENCE_BY_HARNESS["codex"]["model"] == "gpt-6-astra"
    assert model_router.HIGH_INTELLIGENCE_BY_HARNESS["grok_build"]["model"] == "grok-4.6"
    fo = model_router._QUOTA_FAILOVER_MODELS
    assert (fo["anthropic"], fo["openai"], fo["codex"], fo["xai"], fo["google"]) == (
        "claude-opus-5", "gpt-sol", "gpt-sol", "grok-4.6", "gemini-3.8-flash",
    )
    assert fo["openrouter"] == fo["deepseek"] == fo["siliconflow"] == "deepseek/deepseek-v4.1-flash"


def test_router_optimizer_and_usage_ids_unchanged() -> None:
    paid = router_optimizer.PAID_DIRECT_MODELS
    assert paid["research"]["model"] == paid["coding_medium"]["model"] == "gemini-3.8-flash"
    assert paid["web_research"]["model"] == paid["review"]["model"] == "grok-4.6"
    assert adapters._ANTIGRAVITY_MODEL == "gemini-3.8-flash"


def test_gateway_ids_resolve_naming_only() -> None:
    assert registry.gateway_id("opus-legacy") == "anthropic/claude-opus-5"
    assert registry.gateway_id("anthropic/claude-opus-5") == "anthropic/claude-opus-5"
    assert registry.gateway_id("astra") == "openai/gpt-6-astra"
    assert registry.gateway_id("deepseek-v4-pro") == "deepseek/deepseek-v4-pro"  # no gateway id: canonical id
    assert registry.id_of("xai/grok-4.6") == "grok-4.6"
    with pytest.raises(KeyError):
        registry.gateway_id("nope/unknown")
    # Gateway-only models were never routable: no role points at them and resolve() refuses them.
    for name in ("deepseek-v4-pro", "gemini-pro", "gpt-image", "gemini-image-fast"):
        with pytest.raises(KeyError):
            registry.resolve(name)
    assert registry.resolve_known("claude-legacy-sonnet") == "claude-3-7-sonnet"
    routed = set(registry.get_registry().roles.values())
    assert not routed & {"deepseek/deepseek-v4-pro", "gemini-3.1-pro", "gpt-image-2", "claude-3-7-sonnet"}


def test_qualified_routes_ids_unchanged() -> None:
    from core.workflow import qualified_routes as qr

    fs = qr.FAMILY_SYNONYMS
    for key, fam in {
        "anthropic/claude-opus-5": "claude", "claude-opus-5": "claude", "gpt-6-astra": "gpt-oss",
        "gemini-3.8-flash": "gemini", "google/gemini-3.8-flash": "gemini", "google/gemini-3.1-pro": "gemini",
        "deepseek-v4-pro": "deepseek", "deepseek-v4.1-flash": "deepseek", "deepseek/deepseek-v4-pro": "deepseek",
        "deepseek/deepseek-v4.1-flash": "deepseek", "grok-4.6": "grok", "xai/grok-4.6": "grok",
    }.items():
        assert fs[key] == fam
    cat = qr._EMBEDDED_DEFAULT_CATALOG
    assert cat["roles_mapping"]["high_architecture"]["allowed_models"] == [
            "anthropic/claude-opus-5", "deepseek/deepseek-v4-pro", "google/gemini-3.1-pro",
    ]
    assert cat["roles_mapping"]["verifier"]["allowed_models"][1:] == ["deepseek/deepseek-v4.1-flash", "xai/grok-4.6"]


def test_gateway_readers_ids_unchanged() -> None:
    from core.acceptance import engine as acceptance
    from core.execution import resilience
    from core.game import cli as game_cli

    assert resilience.DEFAULT_PARETO_FALLBACK_MODELS == {
        "low": "qwen/qwen3-8-flash-next",
        "medium": "deepseek/deepseek-v4-pro",
        "high": "deepseek/deepseek-v4-pro",
        "critical": "anthropic/claude-3.7-sonnet",
    }
    assert game_cli.FRONTIER_MODEL == "openai/gpt-6-astra"
    assert game_cli.FAST_IMAGE_MODEL == "google/gemini-3.1-flash-lite-image"
    assert game_cli.FRONTIER_IMAGE_MODEL == "openai/gpt-image-2"
    assert acceptance._PARETO_FLASH == "deepseek-v4.1-flash"


def test_subagent_forbidden_models_come_from_registry_when_absent() -> None:
    raw = json.loads((REPO_ROOT / ".factory" / "config" / "subagent_model_routing.json").read_text(encoding="utf-8"))
    assert "forbidden_autonomous_models" not in raw
    cfg = subagent_routing.parse_config(raw)
    assert cfg.forbidden_autonomous_models == ["astra", "claude-fable", "fable", "gpt-6-astra"]

