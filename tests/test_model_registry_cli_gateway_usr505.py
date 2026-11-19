"""USR-505 T1: CLI slugs and provider/model gateway ids are separate registry names."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from core.line.agent_cli import AgentRequest, _codex_cli_slug, build_codex_argv
from core.harness import remote_worker
from core.harness.test_subagent import HarnessType, get_harness_test_runner_spec
from core.models import registry as reg

EXPECTED_ROLES = {
    "subagent.haiku": "claude-haiku-5-5",
    "subagent.sonnet": "claude-sonnet-5-5",
    "subagent.opus": "claude-opus-5-5",
    "subagent.antigravity.simple": "gemini-4-hydrogen-high",
    "subagent.antigravity.specific": "gemini-4-argon-high",
    "coordination.antigravity": "gemini-4-neon",
    "development.claude": "claude-sonnet-5-5",
    "development.high.claude": "claude-opus-5-5",
    "development.codex": "gpt-6-luna",
    "development.high.codex": "gpt-sol",
    "development.grok": "grok-4.7",
    "development.antigravity": "gemini-4-hydrogen-high",
    "development.high.antigravity": "gemini-4-neon",
    "planning.claude": "claude-opus-5-5",
    "planning.codex": "gpt-sol",
    "planning.antigravity": "gemini-4-neon",
    "planning.openrouter": "gpt-sol",
    "development.openrouter": "gpt-sol",
    "review.codex": "gpt-6-luna",
    "review.openrouter": "gpt-6-luna",
    "review.grok": "grok-4.6",
    "distill.openrouter": "deepseek/deepseek-v4.1-flash",
}

EXPECTED_REVIEW_PAIRS = [
    {"implementer": "development.claude", "reviewer": "review.codex"},
    {"implementer": "development.codex", "reviewer": "review.grok"},
    {"implementer": "development.grok", "reviewer": "review.codex"},
]


def _raw() -> dict:
    return json.loads(reg.DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))


def _minimal(**model_fields: object) -> dict:
    model = {
        "id": "m-one",
        "provider": "p",
        "family": "f",
        "tier": "mid",
        "status": "active",
        "qualification": {"status": "grandfathered"},
    }
    model.update(model_fields)
    return {"models": [model]}


def test_cli_id_resolves_only_cli_slugs_and_gateway_id_only_provider_models() -> None:
    parsed = reg.parse_registry(_raw())
    assert reg.cli_id("sol", parsed) == "gpt-5.6-sol"
    assert reg.cli_id("sonnet", parsed) == "claude-sonnet"
    assert reg.cli_id("gpt-5.6-sol", parsed) == "gpt-5.6-sol"
    assert reg.cli_id("claude-sonnet", parsed) == "claude-sonnet"
    assert reg.gateway_id("sol", parsed) == "openai/gpt-5.6-sol"
    assert reg.gateway_id("sonnet", parsed) == "anthropic/claude-sonnet-5.5"
    assert reg.gateway_id("gpt-5.6-sol", parsed) == "openai/gpt-5.6-sol"
    assert reg.gateway_id("claude-sonnet", parsed) == "anthropic/claude-sonnet-5.5"
    assert "/" not in reg.cli_id("sol", parsed)
    assert "/" in reg.gateway_id("sol", parsed)

    for entry in parsed.models:
        for wire in entry.gateway_ids:
            assert "/" in wire
        for slug in entry.cli_ids:
            assert "/" not in slug
        if entry.gateway_ids:
            assert reg.gateway_id(entry.id, parsed) == entry.gateway_ids[0]
            assert "/" in reg.gateway_id(entry.id, parsed)
        else:
            assert reg.gateway_id(entry.id, parsed) == entry.id
        if entry.cli_ids:
            assert reg.cli_id(entry.id, parsed) == entry.cli_ids[0]
            assert "/" not in reg.cli_id(entry.id, parsed)
        elif "/" not in entry.id:
            assert reg.cli_id(entry.id, parsed) == entry.id
        else:
            with pytest.raises(KeyError):
                reg.cli_id(entry.id, parsed)


def test_slash_free_gateway_id_and_slashed_cli_id_are_rejected() -> None:
    raw = _minimal(gateway_ids=["claude-sonnet"])
    with pytest.raises(ValidationError, match="provider/model"):
        reg.parse_registry(raw)

    raw = _minimal(cli_ids=["openai/gpt-5.6-sol"])
    with pytest.raises(ValidationError, match="slash-free"):
        reg.parse_registry(raw)

    parsed = reg.parse_registry(_minimal(gateway_ids=["openai/m-one"], cli_ids=["m-one-cli"]))
    assert reg.gateway_id("m-one", parsed) == "openai/m-one"
    assert reg.cli_id("m-one", parsed) == "m-one-cli"


def test_roles_and_review_pairs_stay_structurally_equal() -> None:
    parsed = reg.parse_registry(_raw())
    embedded = reg.embedded_registry()
    assert dict(parsed.roles) == EXPECTED_ROLES
    assert dict(embedded.roles) == EXPECTED_ROLES
    assert [pair.model_dump() for pair in parsed.review_pairs] == EXPECTED_REVIEW_PAIRS
    assert [pair.model_dump() for pair in embedded.review_pairs] == EXPECTED_REVIEW_PAIRS
    for role, model_id in EXPECTED_ROLES.items():
        assert reg.resolve(role, parsed) == model_id
        assert reg.resolve(role, embedded) == model_id


def test_declared_split_keeps_the_ids_sent_today() -> None:
    parsed = reg.parse_registry(_raw())
    embedded = reg.embedded_registry()
    canonical = reg.resolve("planning.codex", parsed)
    assert canonical == "gpt-sol"
    assert reg.cli_id(canonical, parsed) == "gpt-5.6-sol"
    assert reg.gateway_id(canonical, parsed) == "openai/gpt-5.6-sol"
    assert reg.gateway_id("opus", parsed) == "anthropic/claude-opus-5.5"
    assert reg.cli_id("opus", parsed) == "claude-opus-5-5"
    assert reg.gateway_id("deepseek-flash", parsed) == "deepseek/deepseek-v4.1-flash"
    with pytest.raises(KeyError):
        reg.cli_id("deepseek-flash", parsed)
    for name in ("sol", "sonnet", "haiku", "luna"):
        assert reg.cli_id(name, embedded) == reg.cli_id(name, parsed)
        assert reg.gateway_id(name, embedded) == reg.gateway_id(name, parsed)

    assert _codex_cli_slug("gpt-sol") == "gpt-5.6-sol"
    assert _codex_cli_slug("sol") == "gpt-5.6-sol"
    assert _codex_cli_slug("luna") == "luna"
    assert _codex_cli_slug("gpt-6-luna") == "gpt-6-luna"
    req = AgentRequest(prompt="x", cwd=Path("."), mode="read", harness="codex", model="gpt-sol")
    argv = build_codex_argv("codex", req, Path("out.txt"))
    assert argv[argv.index("-m") + 1] == "gpt-5.6-sol"


def test_cli_slug_still_names_the_same_canonical_model() -> None:
    parsed = reg.parse_registry(_raw())
    assert reg.resolve("gpt-5.6-sol", parsed) == "gpt-sol"
    assert reg.id_of("claude-sonnet", parsed) == "claude-sonnet-5-5"
    raw = copy.deepcopy(_raw())
    opus = next(model for model in raw["models"] if model["id"] == "claude-opus-5-5")
    opus["cli_ids"] = ["opus"]
    with pytest.raises(ValidationError, match="collides"):
        reg.parse_registry(raw)


@pytest.mark.parametrize(
    ("harness", "expected_model"),
    [
        (HarnessType.CODEX, "gpt-6-luna"),
        (HarnessType.GROK, "grok-4.6"),
        (HarnessType.CLAUDE_CODE, "claude-haiku-5-5"),
    ],
)
def test_subagent_runner_specs_use_cli_slugs(harness: HarnessType, expected_model: str) -> None:
    spec = get_harness_test_runner_spec(harness)
    assert spec.model_id == expected_model
    assert "/" not in spec.model_id
    if harness == HarnessType.CODEX:
        assert '"model": "gpt-6-luna"' in spec.config_snippet
    elif harness == HarnessType.CLAUDE_CODE:
        assert "--model claude-haiku-5-5" in spec.config_snippet


@pytest.mark.parametrize(
    ("harness", "expected_default"),
    [
        ("codex", "gpt-5.6-sol"),
        ("grok", "grok-4.6"),
        ("claude", "claude-sonnet"),
    ],
)
def test_remote_worker_reports_cli_defaults_and_normalizes_gateway_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    expected_default: str,
) -> None:
    monkeypatch.setattr(remote_worker, "find_codex_binary", lambda: "codex")
    monkeypatch.setattr(remote_worker, "find_grok_binary", lambda: "grok")
    monkeypatch.setattr(remote_worker, "find_claude_binary", lambda: "claude")
    monkeypatch.setattr(
        remote_worker.subprocess,
        "run",
        lambda *args, **kwargs: remote_worker.subprocess.CompletedProcess(args[0], 0, "ok", ""),
    )
    client = TestClient(remote_worker.create_worker_app(project_root=tmp_path, node_id="usr505"))

    default_response = client.post("/harness/execute", json={"harness": harness, "prompt": "test"})
    assert default_response.status_code == 200
    assert default_response.json()["model"] == expected_default

    gateway_override = {
        "codex": "openai/gpt-5.6-sol",
        "grok": "x-ai/grok-4.6",
        "claude": "anthropic/claude-sonnet-5.5",
    }[harness]
    override_response = client.post(
        "/harness/execute",
        json={"harness": harness, "prompt": "test", "model": gateway_override},
    )
    assert override_response.status_code == 200
    assert override_response.json()["model"] == reg.cli_id(gateway_override)


