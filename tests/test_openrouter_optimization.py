"""Tests for USR-440: OpenRouter optimization, write mode, high-tier planning, and budget alerts."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.line.agent_cli import (
    HARNESS_CAPABILITIES,
    AgentRequest,
    _apply_openrouter_write_blocks,
    run_agent,
    supports,
)
from core.line.routing import (
    STAGE_MODES,
    RoutingConfig,
    _check_openrouter_monthly_budget,
    default_config_path,
    is_high_tier_model,
    load_routing_config,
    pick,
)
from core.models.registry import resolve


def test_openrouter_capabilities_include_read_and_write() -> None:
    assert supports("openrouter", "read") is True
    assert supports("openrouter", "write") is True
    assert HARNESS_CAPABILITIES["openrouter"] == frozenset({"read", "write"})


def test_apply_openrouter_write_blocks_creates_files(tmp_path: Path) -> None:
    text = (
        "Aqui estao os arquivos:\n"
        "```file:pkg/module.py\n"
        "def greet(name: str) -> str:\n"
        "    return f'Hello, {name}!'\n"
        "```\n"
        "```file:README.md\n"
        "# Project Title\n"
        "```\n"
    )
    modified = _apply_openrouter_write_blocks(text, tmp_path)
    assert "pkg/module.py" in modified
    assert "README.md" in modified
    assert (tmp_path / "pkg" / "module.py").read_text(encoding="utf-8") == (
        "def greet(name: str) -> str:\n    return f'Hello, {name}!'\n"
    )
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# Project Title\n"


def test_apply_openrouter_write_blocks_applies_surgical_patches(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("x = 10\ny = 20\nz = x + y\n", encoding="utf-8")

    patch_text = (
        "Substituindo valor de y:\n"
        "```patch:app.py\n"
        "<<<<<<< SEARCH\n"
        "y = 20\n"
        "=======\n"
        "y = 50\n"
        ">>>>>>> REPLACE\n"
        "```\n"
    )
    modified = _apply_openrouter_write_blocks(patch_text, tmp_path)
    assert "app.py" in modified
    assert target.read_text(encoding="utf-8") == "x = 10\ny = 50\nz = x + y\n"


def test_openrouter_roles_resolve_to_high_tier_models() -> None:
    planning_model = resolve("planning.openrouter")
    dev_model = resolve("development.openrouter")
    distill_model = resolve("distill.openrouter")

    assert planning_model == "gpt-sol"
    assert is_high_tier_model("planning.openrouter") is True
    assert is_high_tier_model(planning_model) is True

    assert dev_model == "gpt-sol"
    assert is_high_tier_model("development.openrouter") is True
    assert is_high_tier_model(dev_model) is True

    assert distill_model == "deepseek/deepseek-v4.1-flash"


def test_dynamic_pareto_model_resolution_selects_frontier_high_tier() -> None:
    from core.line.routing import resolve_pareto_openrouter_model

    plan_pareto = resolve_pareto_openrouter_model("planning")
    assert is_high_tier_model(plan_pareto) is True
    assert plan_pareto in ("anthropic/claude-opus-5", "openai/gpt-5-6-sol", "gpt-sol")

    dev_pareto = resolve_pareto_openrouter_model("development")
    assert dev_pareto is not None


def test_openrouter_write_mode_executes_and_modifies_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeResponse:
        text = (
            "Desenvolvimento concluido:\n"
            "```file:core/calc.py\n"
            "def multiply(a: int, b: int) -> int:\n"
            "    return a * b\n"
            "```\n"
        )
        tokens_prompt = 15
        tokens_completion = 30
        total_tokens = 45
        is_measured = True
        measured_cost = 0.0005
        estimated_cost = 0.0005
        model = "qwen/qwen-2.5-coder-32b-instruct"

    class _FakeProvider:
        def generate(self, prompt, **kwargs):
            return _FakeResponse()

    monkeypatch.setattr("core.execution.providers.OpenRouterModelProvider", _FakeProvider)

    req = AgentRequest(
        prompt="Implemente multiply",
        cwd=tmp_path,
        mode="write",
        harness="openrouter",
        model="development.openrouter",
    )
    result = run_agent(req)

    assert result.ok is True
    assert result.harness == "openrouter"
    assert (tmp_path / "core" / "calc.py").is_file()
    assert "def multiply" in (tmp_path / "core" / "calc.py").read_text(encoding="utf-8")


def test_openrouter_fallback_elected_for_both_planning_and_development(tmp_path: Path) -> None:
    config = load_routing_config(default_config_path())
    cooldowns = tmp_path / "cooldowns.json"

    # Quando Claude, Codex e Grok estao no piso critico (<= 15%):
    plan_choice = pick(
        "planning",
        ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"],
        config=config,
        quota_lookup=lambda _p: 5.0,  # critico
        cooldown_path=cooldowns,
        operating_harness=None,
        openrouter_balance_lookup=lambda: 10.0,
    )
    assert plan_choice[0] == "openrouter"
    assert is_high_tier_model(plan_choice[1]) is True
    assert plan_choice[1] in ("anthropic/claude-opus-5", "openai/gpt-5-6-sol", "gpt-sol")

    dev_choice = pick(
        "development",
        ["harness:claude", "harness:codex", "harness:grok"],
        config=config,
        quota_lookup=lambda _p: 5.0,  # critico
        cooldown_path=cooldowns,
        operating_harness=None,
        openrouter_balance_lookup=lambda: 10.0,
    )
    assert dev_choice[0] == "openrouter"
    assert dev_choice[1] is not None


def test_check_openrouter_monthly_budget_triggers_alert_when_exceeding_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.usage.api_credits import ProviderCreditCard, CreditAccountStatus

    fake_card = ProviderCreditCard(
        provider_id="openrouter",
        provider_name="OpenRouter",
        status=CreditAccountStatus.ACTIVE,
        is_connected=True,
        current_month_spend_usd=8.50,  # 85% do teto de 10.0
        cumulative_spend_usd=8.50,
        available_credit_usd=1.50,
        currency="USD",
    )

    class _FakeMonitor:
        def inspect_openrouter(self):
            return fake_card

    monkeypatch.setattr("core.usage.api_credits.ApiCreditsMonitor", _FakeMonitor)

    dispatched_events = []

    class _FakeNotificationService:
        def __init__(self, cooldown_minutes=60):
            pass

        def dispatch(self, **kwargs):
            dispatched_events.append(kwargs)

    monkeypatch.setattr("core.notifications.service.NotificationService", _FakeNotificationService)

    config = load_routing_config(default_config_path())
    _check_openrouter_monthly_budget(config)

    assert len(dispatched_events) == 1
    event = dispatched_events[0]
    assert "Alerta de Orcamento OpenRouter" in event["title"]
    assert "8.50" in event["message"]
    assert "10.00" in event["message"]
