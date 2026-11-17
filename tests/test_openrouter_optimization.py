"""Tests for USR-440: OpenRouter optimization, write mode, high-tier planning, and budget alerts."""

from __future__ import annotations

import json
import types
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
    dev_model = resolve("review.openrouter")
    distill_model = resolve("distill.openrouter")

    assert planning_model == "gpt-sol"
    assert is_high_tier_model("planning.openrouter") is True
    assert is_high_tier_model(planning_model) is True

    assert dev_model == "gpt-6-luna"  # development on OpenRouter uses the cheap role
    assert is_high_tier_model("review.openrouter") is False

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
    monkeypatch.setattr("core.line.agent_cli.find_codex_binary", lambda: None)  # sem o agente: resposta unica

    req = AgentRequest(
        prompt="Implemente multiply",
        cwd=tmp_path,
        mode="write",
        harness="openrouter",
        model="review.openrouter",
    )
    result = run_agent(req)

    assert result.ok is True
    assert result.harness == "openrouter"
    assert (tmp_path / "core" / "calc.py").is_file()
    assert "def multiply" in (tmp_path / "core" / "calc.py").read_text(encoding="utf-8")


def test_openrouter_write_mode_uses_the_agent_loop_when_the_cli_and_key_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.line import agent_cli, routing

    seen: dict[str, object] = {}

    def fake_codex(req, *, gateway_model=None, env=None):
        seen.update(gateway=gateway_model, key=(env or {}).get("OPENROUTER_API_KEY"))
        return agent_cli.AgentResult(ok=True, text="feito", harness="codex", model="x", duration_s=1.0)

    states = iter([set(), {"core/calc.py"}])
    monkeypatch.setattr(agent_cli, "find_codex_binary", lambda: "codex")
    monkeypatch.setattr("core.execution.providers.get_openrouter_api_key", lambda: "chave-de-teste")
    monkeypatch.setattr(agent_cli, "_run_codex", fake_codex)
    monkeypatch.setattr(agent_cli, "_changed_paths", lambda _cwd: next(states))
    monkeypatch.setattr(routing, "record_openrouter_edits", lambda count, **_: seen.update(edits=count))

    result = run_agent(AgentRequest(
        prompt="Implemente multiply", cwd=tmp_path, mode="write", harness="openrouter", model="review.openrouter",
    ))

    assert (result.ok, result.harness) == (True, "openrouter")
    assert seen == {"gateway": result.model, "key": "chave-de-teste", "edits": 1}
    assert "/" in str(result.model)  # id de gateway, nao slug de CLI
    argv = agent_cli.build_codex_argv("codex", AgentRequest(prompt="p", cwd=tmp_path, mode="write", harness="openrouter"), tmp_path / "o",
                                      gateway_model="fornecedor/modelo")
    assert argv[argv.index("-m") + 1] == "fornecedor/modelo"
    assert 'model_provider="openrouter"' in argv


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
        current_month_spend_usd=23.0,  # 82% do teto de 28.13
        cumulative_spend_usd=23.0,
        available_credit_usd=5.0,
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
    assert "23.00" in event["message"]
    assert "28.13" in event["message"]


def test_openrouter_model_wire_resolution() -> None:
    from core.execution.providers import _resolve_openrouter_wire_model
    from core.models.registry import gateway_id

    # Internal alias resolves to live OpenRouter wire id with provider prefix
    assert gateway_id("gpt-sol") == "openai/gpt-5.6-sol"
    assert _resolve_openrouter_wire_model("gpt-sol") == "openai/gpt-5.6-sol"
    assert _resolve_openrouter_wire_model("openai/gpt-5.6-sol") == "openai/gpt-5.6-sol"


def _card(spend, credit):
    from core.usage.api_credits import CreditAccountStatus, ProviderCreditCard

    return ProviderCreditCard(
        provider_id="openrouter", provider_name="OpenRouter", status=CreditAccountStatus.ACTIVE, is_connected=True,
        current_month_spend_usd=spend, available_credit_usd=credit, currency="USD",
    )


def test_monthly_cap_blocks_openrouter_but_unknown_spend_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.line import routing as r

    cards = {"card": _card(28.13, 50.0)}
    monkeypatch.setattr(r, "_openrouter_card", lambda: cards["card"])
    assert r._openrouter_has_credits(None, 28.13) is False  # month spend reached the cap
    cards["card"] = _card(None, 5.0)
    assert r._openrouter_has_credits(None, 28.13) is True  # unknown spend, known positive balance
    cards["card"] = _card(3.0, 0.0)
    assert r._openrouter_has_credits(None, 28.13) is False  # no balance


def test_run_spend_comes_from_the_ledger_and_two_empty_edit_runs_cool_openrouter_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.line import routing as r
    from core.usage.ledger import ModelUsageLedger
    from core.usage.models import ModelCallEvent

    ledger = ModelUsageLedger(tmp_path / "usage")
    for ticket, cost in (("USR-1", 1.5), ("USR-1", 0.75), ("USR-2", 9.0)):
        ledger.record(ModelCallEvent(ticket_id=ticket, provider="openrouter", model="m", cost_usd=cost))
    monkeypatch.setattr("core.usage.ledger.ModelUsageLedger", lambda: ledger)
    assert r._openrouter_run_spend("USR-1") == 2.25
    cfg = load_routing_config(default_config_path())
    cooldowns = tmp_path / "cooldowns.json"
    caps = ["harness:claude"]
    pick_kwargs = dict(config=cfg, quota_lookup=lambda _p: 5.0, cooldown_path=cooldowns, ticket_id="USR-1",
                       openrouter_balance_lookup=lambda: 10.0)
    assert pick("development", caps, **pick_kwargs) is None  # run spend 2.25 is over the 2.00 run cap
    assert pick("development", caps, **{**pick_kwargs, "ticket_id": "USR-2"}) is None
    assert pick("development", caps, **{**pick_kwargs, "ticket_id": "USR-3"})[0] == "openrouter"

    r.record_openrouter_edits(0, config=cfg, cooldown_path=cooldowns)
    assert pick("development", caps, **{**pick_kwargs, "ticket_id": "USR-3"})[0] == "openrouter"
    r.record_openrouter_edits(0, config=cfg, cooldown_path=cooldowns)
    assert pick("development", caps, **{**pick_kwargs, "ticket_id": "USR-3"}) is None


def _agent_run_with_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, usage, ledger, model: str = "gpt-sol"):
    from core.line import agent_cli

    def fake_codex(req, *, gateway_model=None, env=None):  # nunca chama o Codex CLI de verdade
        return agent_cli.AgentResult(ok=True, text="feito", harness="codex", model="x", duration_s=1.0, usage=usage)

    monkeypatch.setattr(agent_cli, "find_codex_binary", lambda: "codex")
    monkeypatch.setattr(agent_cli, "_run_codex", fake_codex)
    monkeypatch.setattr(agent_cli, "_changed_paths", lambda _cwd: set())
    monkeypatch.setattr("core.execution.providers.get_openrouter_api_key", lambda: "chave-de-teste")
    monkeypatch.setattr("core.line.routing.record_openrouter_edits", lambda *_a, **_k: None)
    monkeypatch.setattr("core.usage.ledger.ModelUsageLedger", lambda *_a, **_k: ledger)
    return agent_cli.run_agent(agent_cli.AgentRequest(
        prompt="p", cwd=tmp_path, mode="write", harness="openrouter", model=model, ticket_id="USR-9",
    ))


def test_openrouter_agent_cost_enters_the_ledger_and_caps_the_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.usage.ledger import ModelUsageLedger

    ledger = ModelUsageLedger(tmp_path / "usage")
    prices = types.SimpleNamespace(input_cost_per_m=4.0, output_cost_per_m=20.0)
    monkeypatch.setattr(
        "core.benchmarks.fetcher.ensure_daily_benchmark",
        lambda: types.SimpleNamespace(models={"openai/gpt-5.6-sol": prices}),
    )
    usage = {"input_tokens": 400_000, "cached_input_tokens": 300_000, "output_tokens": 50_000}
    result = _agent_run_with_usage(tmp_path, monkeypatch, usage, ledger)

    expected = (400_000 * 4.0 + 50_000 * 20.0) / 1_000_000  # cache conta como entrada normal
    assert result.cost_usd == pytest.approx(expected)
    assert ledger.spent_usd("USR-9", "openrouter") == pytest.approx(expected)
    assert expected > 2.0

    cfg = load_routing_config(default_config_path())
    kwargs = dict(config=cfg, quota_lookup=lambda _p: 5.0, cooldown_path=tmp_path / "cd.json",
                  openrouter_balance_lookup=lambda: 10.0)
    assert pick("development", ["harness:claude"], ticket_id="USR-9", **kwargs) is None
    assert pick("development", ["harness:claude"], ticket_id="USR-10", **kwargs)[0] == "openrouter"


def test_openrouter_agent_with_unpriced_model_records_null_cost_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from core.usage.ledger import ModelUsageLedger

    ledger = ModelUsageLedger(tmp_path / "usage")
    monkeypatch.setattr("core.benchmarks.fetcher.ensure_daily_benchmark", lambda: types.SimpleNamespace(models={}))
    with caplog.at_level("WARNING"):
        result = _agent_run_with_usage(tmp_path, monkeypatch, {"input_tokens": 10, "output_tokens": 5}, ledger)

    assert result.ok is True and result.cost_usd is None
    assert ledger.spent_usd("USR-9", "openrouter") == 0.0
    assert any("cost unknown" in rec.message for rec in caplog.records)
