"""Comprehensive integration and decoupling tests for core.execution providers.

Validates DF-14:
1. Strict architectural decoupling: core/ must never import from hub/.
2. Common ModelProvider adapter conforming to protocol (Mock, Ollama, OpenRouter, Unified).
3. Telemetry integration with ModelUsageLedger and cost policy enforcement.
4. Echo Garden CLI workflow integration using real provider adapter, budget reservations, and offline assets.
"""

from __future__ import annotations

import ast
import json
import unittest.mock as mock
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.execution.budget import ExecutionBudgetManager
from core.execution.contracts import (
    AttemptOutcome,
    Budget,
    UnknownCostPolicy,
)
from core.execution.providers import (
    MockModelProvider,
    ModelProvider,
    OllamaModelProvider,
    OpenRouterModelProvider,
    ProviderResponse,
    RemoteCodexModelProvider,
    UnifiedModelProvider,
    get_model_provider,
    get_openrouter_api_key,
)
from core.game.cli import (
    BALANCED_MODEL,
    FRONTIER_MODEL,
    LOCAL_MODEL,
    build_live,
)
from core.game.models import GameManifest
from core.usage.ledger import ModelUsageLedger


def test_strict_architectural_decoupling_core_never_imports_hub() -> None:
    """Validate that no module in core/ imports from hub/."""
    core_dir = Path(__file__).resolve().parent.parent / "core"
    violations: list[str] = []

    for py_file in core_dir.rglob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(py_file))
        except Exception as exc:
            violations.append(f"Failed to parse {py_file}: {exc}")
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "hub" or alias.name.startswith("hub."):
                        violations.append(f"{py_file.name}:{node.lineno} imports '{alias.name}'")
            elif isinstance(node, ast.ImportFrom):
                if node.module and (node.module == "hub" or node.module.startswith("hub.")):
                    violations.append(f"{py_file.name}:{node.lineno} imports from '{node.module}'")

    assert not violations, f"Forbidden imports of 'hub' found in core/:\n" + "\n".join(violations)


def test_get_openrouter_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify get_openrouter_api_key returns the environment key."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test-key")
    assert get_openrouter_api_key() == "sk-or-v1-test-key"


def test_get_openrouter_api_key_empty_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify get_openrouter_api_key returns None when empty or unset without registry."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("sys.platform", "linux")
    assert get_openrouter_api_key() is None


def test_mock_model_provider_conforms_to_protocol() -> None:
    """Verify MockModelProvider implements the ModelProvider protocol."""
    provider = MockModelProvider()
    assert isinstance(provider, ModelProvider)
    assert provider.provider_id == "mock-provider"

    res = provider.generate("hello world", model="mock-test")
    assert isinstance(res, ProviderResponse)
    assert res.model == "mock-test"
    assert res.total_tokens > 0
    assert res.latency_seconds > 0
    assert res.is_measured is True
    assert res.text == "Mock execution completed successfully."
    assert res.metadata == {"backend": "mock"}


def test_mock_model_provider_responses_by_model_and_sequence() -> None:
    """Verify MockModelProvider supports per-model mapping and response sequence."""
    by_model_provider = MockModelProvider(
        responses_by_model={
            "model-a": "Response A",
            "model-b": "Response B",
        }
    )
    res_a = by_model_provider.generate("prompt", model="model-a")
    res_b = by_model_provider.generate("prompt", model="model-b")
    res_c = by_model_provider.generate("prompt", model="model-c")
    assert res_a.text == "Response A"
    assert res_b.text == "Response B"
    assert res_c.text == "Mock execution completed successfully."

    seq_provider = MockModelProvider(response_sequence=["First", "Second"])
    assert seq_provider.generate("1", model="m").text == "First"
    assert seq_provider.generate("2", model="m").text == "Second"
    assert seq_provider.generate("3", model="m").text == "Mock execution completed successfully."


def test_mock_model_provider_unknown_cost_policies() -> None:
    """Verify MockModelProvider obeys UnknownCostPolicy in simulation mode."""
    provider = MockModelProvider(simulate_unknown_cost=True)

    with pytest.raises(ValueError, match="policy is REJECT"):
        provider.generate("prompt", model="m", unknown_cost_policy=UnknownCostPolicy.REJECT)

    est_res = provider.generate("prompt", model="m", unknown_cost_policy=UnknownCostPolicy.ESTIMATE)
    assert est_res.measured_cost is None
    assert est_res.estimated_cost > 0
    assert est_res.is_measured is False

    max_res = provider.generate("prompt", model="m", unknown_cost_policy=UnknownCostPolicy.CONSERVATIVE_MAX)
    assert max_res.measured_cost is None
    assert max_res.estimated_cost > est_res.estimated_cost
    assert max_res.is_measured is False


def test_ollama_model_provider_mocked_http(tmp_path: Path) -> None:
    """Verify OllamaModelProvider executes HTTP call and logs usage to ledger."""
    usage_dir = tmp_path / "usage"
    ledger = ModelUsageLedger(usage_dir)
    provider = OllamaModelProvider(base_url="http://localhost:11434", usage_ledger=ledger)

    fake_resp = mock.MagicMock()
    fake_resp.read.return_value = json.dumps({
        "model": "qwen-code-fast:latest",
        "response": '{"tagline": "test puzzle"}',
        "prompt_eval_count": 12,
        "eval_count": 24,
        "done": True,
    }).encode("utf-8")
    fake_resp.__enter__.return_value = fake_resp

    with mock.patch("urllib.request.urlopen", return_value=fake_resp):
        res = provider.generate("generate tagline", model="qwen-code-fast:latest")
        assert res.model == "qwen-code-fast:latest"
        assert res.text == '{"tagline": "test puzzle"}'
        assert res.tokens_prompt == 12
        assert res.tokens_completion == 24
        assert res.total_tokens == 36
        assert res.measured_cost == 0.0
        assert res.is_measured is True
        assert res.metadata == {"backend": "ollama"}

    summary = ledger.report()
    assert summary.total_calls >= 1


def test_openrouter_model_provider_mocked_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify OpenRouterModelProvider executes chat completion and records usage."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-mock-key")
    usage_dir = tmp_path / "usage"
    ledger = ModelUsageLedger(usage_dir)
    provider = OpenRouterModelProvider(usage_ledger=ledger)

    fake_resp = mock.MagicMock()
    fake_resp.read.return_value = json.dumps({
        "model": "mistralai/mistral-nemo",
        "choices": [{"message": {"content": '{"moves": ["weave"]}'}}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 15, "total_tokens": 55},
    }).encode("utf-8")
    fake_resp.__enter__.return_value = fake_resp

    with mock.patch("urllib.request.urlopen", return_value=fake_resp):
        res = provider.generate(
            "solve moves",
            model="mistralai/mistral-nemo",
            unknown_cost_policy=UnknownCostPolicy.ESTIMATE,
        )
        assert res.model == "mistralai/mistral-nemo"
        assert res.text == '{"moves": ["weave"]}'
        assert res.tokens_prompt == 40
        assert res.tokens_completion == 15
        assert res.total_tokens == 55
        assert res.metadata == {"backend": "openrouter"}

    summary = ledger.report()
    assert summary.total_calls >= 1


def test_openrouter_missing_key_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify OpenRouterModelProvider raises RuntimeError when key is missing."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("sys.platform", "linux")
    provider = OpenRouterModelProvider(api_key=None)

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        provider.generate("prompt", model="openai/gpt-6-astra")


def test_unified_model_provider_routing() -> None:
    """Verify UnifiedModelProvider routes slash models to cloud and bare models to local."""
    mock_ollama = MockModelProvider(provider_id="ollama", fixed_response="ollama_output")
    mock_openrouter = MockModelProvider(provider_id="openrouter", fixed_response="openrouter_output")
    mock_local = MockModelProvider(provider_id="mock", fixed_response="mock_output")

    unified = UnifiedModelProvider(
        ollama_provider=mock_ollama,
        openrouter_provider=mock_openrouter,
        mock_provider=mock_local,
    )

    # Local model -> ollama
    res_local = unified.generate("prompt", model="qwen-code-fast:latest")
    assert res_local.text == "ollama_output"

    # Cloud model with slash -> openrouter
    res_cloud = unified.generate("prompt", model="anthropic/claude-3.7-sonnet")
    assert res_cloud.text == "openrouter_output"

    # Mock model -> mock
    res_mock = unified.generate("prompt", model="mock-agent")
    assert res_mock.text == "mock_output"


def test_get_model_provider_factory() -> None:
    """Verify get_model_provider factory returns correct instances."""
    mock_p = get_model_provider("mock")
    assert isinstance(mock_p, MockModelProvider)

    ollama_p = get_model_provider("ollama")
    assert isinstance(ollama_p, OllamaModelProvider)

    openrouter_p = get_model_provider("openrouter", api_key="test-key")
    assert isinstance(openrouter_p, OpenRouterModelProvider)

    codex_p = get_model_provider("codex", base_url="http://127.0.0.1:8080")
    assert isinstance(codex_p, RemoteCodexModelProvider)

    auto_p = get_model_provider("auto", api_key="test-key")
    assert isinstance(auto_p, UnifiedModelProvider)

    with pytest.raises(ValueError, match="Unknown provider_id"):
        get_model_provider("invalid-provider")


def test_echo_garden_build_live_with_mock_provider_and_budget(tmp_path: Path) -> None:
    """Execute complete Echo Garden build_live using MockModelProvider and ExecutionBudgetManager."""
    mock_provider = MockModelProvider(
        provider_id="mock",
        responses_by_model={
            LOCAL_MODEL: (
                '{"tagline":"Balance the garden before the echo returns.",'
                '"move_labels":{"weave":"Weave","echo":"Reflect","ground":"Ground"}}'
            ),
            BALANCED_MODEL: (
                '{"moves":["weave","ground","weave"],'
                '"rationale":"The third direct pulse and rotated ground echo cancel the spread."}'
            ),
            FRONTIER_MODEL: (
                '{"verdict":"APPROVE",'
                '"risks_checked":["energy bounds","turn budget","echo ordering"],'
                '"summary":"The trace reaches exact resonance on turn three without leaving bounds."}'
            ),
        },
    )

    budget_db = tmp_path / "test_budget.db"
    budget_mgr = ExecutionBudgetManager(database_path=budget_db)

    game_output = tmp_path / "game_output"
    result = build_live(
        output_dir=game_output,
        seed=0,
        provider=mock_provider,
        budget_manager=budget_mgr,
        offline_assets=True,
    )

    # 1. Output verification
    assert result["status"] == "passed"
    assert Path(result["manifest_path"]).exists()
    assert Path(result["html_path"]).exists()

    # 2. Manifest verification
    manifest_data = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    manifest = GameManifest(**manifest_data)
    assert len(manifest.model_evidence) == 3
    for ev in manifest.model_evidence:
        assert ev.provider == "mock"
        assert ev.validation == "passed"
        assert ev.duration_ms > 0
        assert ev.tokens_used is not None and ev.tokens_used > 0

    # 3. Budget & attempts verification
    budget_id = result["budget_id"]
    attempts = budget_mgr.list_attempts(budget_id)
    assert len(attempts) == 3
    for attempt in attempts:
        assert attempt.outcome == AttemptOutcome.SUCCEEDED
        assert attempt.tokens > 0
        assert attempt.estimated_cost >= 0.0

    budget = budget_mgr.get_budget(budget_id)
    assert budget is not None
    assert budget.spent > 0.0
    assert budget.reserved == 0.0  # all reservations committed

    budget_mgr.close()
