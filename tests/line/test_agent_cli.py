"""Tests for core.line.agent_cli — real AgentCLI runner (HF-27-03).

Every test uses a fake CLI executable built by the `make_fake_cli` fixture
(tests/line/conftest.py) and monkeypatches the binary finders in
`core.line.agent_cli`. No network access and no real Claude/Codex/Grok/
Antigravity CLI is ever invoked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.line import agent_cli
from core.line.agent_cli import AgentRequest, run_agent
from tests.line.conftest import set_fake_response


def test_claude_write_mode_flags_and_cwd(tmp_path, make_fake_cli, monkeypatch):
    cmd_path, env_var = make_fake_cli("claude")
    monkeypatch.setattr(agent_cli, "find_claude_binary", lambda: cmd_path)

    record_path = tmp_path / "record.json"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    set_fake_response(
        monkeypatch,
        env_var,
        {
            "record_path": str(record_path),
            "stdout": json.dumps(
                {
                    "result": "wrote the feature",
                    "is_error": False,
                    "total_cost_usd": 0.1234,
                    "usage": {"input_tokens": 1000, "output_tokens": 500},
                }
            ),
            "returncode": 0,
        },
    )

    req = AgentRequest(
        prompt="implement the ticket",
        cwd=project_dir,
        mode="write",
        harness="claude",
        model="sonnet",
        max_turns=5,
    )
    result = run_agent(req)

    assert result.ok is True
    assert result.text == "wrote the feature"
    assert result.cost_usd == pytest.approx(0.1234)
    assert result.usage == {"input_tokens": 1000, "output_tokens": 500}

    record = json.loads(record_path.read_text(encoding="utf-8"))
    argv = record["argv"]
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "--output-format" in argv and "json" in argv
    assert "--max-turns" in argv and argv[argv.index("--max-turns") + 1] == "5"
    assert record["stdin"] == "implement the ticket"
    assert Path(record["cwd"]).resolve() == project_dir.resolve()


def test_claude_read_mode_uses_plan_permission(tmp_path, make_fake_cli, monkeypatch):
    cmd_path, env_var = make_fake_cli("claude")
    monkeypatch.setattr(agent_cli, "find_claude_binary", lambda: cmd_path)
    record_path = tmp_path / "record.json"
    set_fake_response(
        monkeypatch,
        env_var,
        {
            "record_path": str(record_path),
            "stdout": json.dumps({"result": "looks fine", "is_error": False}),
            "returncode": 0,
        },
    )
    req = AgentRequest(prompt="review this", cwd=tmp_path, mode="read", harness="claude", model=None)
    result = run_agent(req)

    assert result.ok is True
    assert result.text == "looks fine"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    argv = record["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert "--model" not in argv  # model omitted when None


def test_claude_json_is_error_classifies_rate_limited_and_reset_at(tmp_path, make_fake_cli, monkeypatch):
    cmd_path, env_var = make_fake_cli("claude")
    monkeypatch.setattr(agent_cli, "find_claude_binary", lambda: cmd_path)
    set_fake_response(
        monkeypatch,
        env_var,
        {
            "stdout": json.dumps(
                {
                    "result": "Error: usage limit exceeded. Try again in 42 minutes.",
                    "is_error": True,
                    "total_cost_usd": 0.02,
                }
            ),
            "returncode": 1,
        },
    )
    req = AgentRequest(prompt="do work", cwd=tmp_path, mode="write", harness="claude")
    result = run_agent(req)

    assert result.ok is False
    assert result.error_kind == "rate_limited"
    assert result.cost_usd == pytest.approx(0.02)
    assert result.reset_at is not None


def test_claude_auth_expired_classification(tmp_path, make_fake_cli, monkeypatch):
    cmd_path, env_var = make_fake_cli("claude")
    monkeypatch.setattr(agent_cli, "find_claude_binary", lambda: cmd_path)
    set_fake_response(
        monkeypatch,
        env_var,
        {"stdout": "", "stderr": "Error: unauthorized (401). Please login again.", "returncode": 1},
    )
    req = AgentRequest(prompt="do work", cwd=tmp_path, mode="write", harness="claude")
    result = run_agent(req)

    assert result.ok is False
    assert result.error_kind == "auth_expired"


def test_claude_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_cli, "find_claude_binary", lambda: None)
    req = AgentRequest(prompt="do work", cwd=tmp_path, mode="write", harness="claude")
    result = run_agent(req)

    assert result.ok is False
    assert result.error_kind == "not_installed"


def test_claude_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_cli, "find_claude_binary", lambda: "claude-fake-path")

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(agent_cli.subprocess, "run", _raise_timeout)
    req = AgentRequest(prompt="do work", cwd=tmp_path, mode="write", harness="claude", timeout_s=1)
    result = run_agent(req)

    assert result.ok is False
    assert result.error_kind == "timeout"


def test_codex_write_mode_flags_and_cwd(tmp_path, make_fake_cli, monkeypatch):
    cmd_path, env_var = make_fake_cli("codex")
    monkeypatch.setattr(agent_cli, "find_codex_binary", lambda: cmd_path)
    record_path = tmp_path / "record.json"
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    tmp_out_content = "\n".join(
        [
            json.dumps({"type": "info"}),
            json.dumps({"usage": {"input_tokens": 200, "output_tokens": 80}, "total_cost_usd": 0.005}),
        ]
    )
    set_fake_response(
        monkeypatch,
        env_var,
        {
            "record_path": str(record_path),
            "tmp_out_content": tmp_out_content,
            "returncode": 0,
        },
    )

    req = AgentRequest(
        prompt="implement ticket HF-1", cwd=project_dir, mode="write", harness="codex", model="gpt-6-astra"
    )
    result = run_agent(req)

    assert result.ok is True
    assert result.usage == {"input_tokens": 200, "output_tokens": 80}
    assert result.cost_usd == pytest.approx(0.005)

    record = json.loads(record_path.read_text(encoding="utf-8"))
    argv = record["argv"]
    assert "--sandbox" in argv
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "--skip-git-repo-check" in argv
    assert "--json" in argv
    assert "-m" in argv and argv[argv.index("-m") + 1] == "gpt-6-astra"
    assert argv[-1] == "-"
    assert record["stdin"] == "implement ticket HF-1"
    assert Path(record["cwd"]).resolve() == project_dir.resolve()


def test_codex_read_mode_uses_read_only_sandbox(tmp_path, make_fake_cli, monkeypatch):
    cmd_path, env_var = make_fake_cli("codex")
    monkeypatch.setattr(agent_cli, "find_codex_binary", lambda: cmd_path)
    record_path = tmp_path / "record.json"
    set_fake_response(
        monkeypatch,
        env_var,
        {"record_path": str(record_path), "tmp_out_content": "", "returncode": 0},
    )
    req = AgentRequest(prompt="review", cwd=tmp_path, mode="read", harness="codex", model=None)
    result = run_agent(req)

    assert result.ok is True
    record = json.loads(record_path.read_text(encoding="utf-8"))
    argv = record["argv"]
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "-m" not in argv  # model omitted when None


def test_codex_rate_limited_from_stderr(tmp_path, make_fake_cli, monkeypatch):
    cmd_path, env_var = make_fake_cli("codex")
    monkeypatch.setattr(agent_cli, "find_codex_binary", lambda: cmd_path)
    set_fake_response(
        monkeypatch,
        env_var,
        {"stderr": "Error: rate limit exceeded (429). Resets in 2 hours.", "returncode": 1},
    )
    req = AgentRequest(prompt="do work", cwd=tmp_path, mode="write", harness="codex")
    result = run_agent(req)

    assert result.ok is False
    assert result.error_kind == "rate_limited"
    assert result.reset_at is not None


def test_grok_write_mode_unsupported(tmp_path, monkeypatch):
    # find_grok_binary should not even be consulted for write mode.
    monkeypatch.setattr(agent_cli, "find_grok_binary", lambda: (_ for _ in ()).throw(AssertionError("should not be called")))
    req = AgentRequest(prompt="do work", cwd=tmp_path, mode="write", harness="grok")
    result = run_agent(req)

    assert result.ok is False
    assert result.error_kind is not None


def test_antigravity_write_mode_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(
        agent_cli, "find_antigravity_binary", lambda: (_ for _ in ()).throw(AssertionError("should not be called"))
    )
    req = AgentRequest(prompt="do work", cwd=tmp_path, mode="write", harness="antigravity")
    result = run_agent(req)

    assert result.ok is False
    assert result.error_kind is not None


def test_grok_read_mode_success(tmp_path, make_fake_cli, monkeypatch):
    cmd_path, env_var = make_fake_cli("grok")
    monkeypatch.setattr(agent_cli, "find_grok_binary", lambda: cmd_path)
    set_fake_response(monkeypatch, env_var, {"stdout": "grok says hi", "returncode": 0})
    req = AgentRequest(prompt="review", cwd=tmp_path, mode="read", harness="grok")
    result = run_agent(req)

    assert result.ok is True
    assert result.text == "grok says hi"


def test_openrouter_write_mode_rejected(tmp_path):
    req = AgentRequest(prompt="do work", cwd=tmp_path, mode="write", harness="openrouter")
    result = run_agent(req)

    assert result.ok is False
    assert "read-only" in result.text.lower()


def test_openrouter_read_mode_success(tmp_path, monkeypatch):
    calls = {}

    class _FakeResponse:
        text = "cheap review text"
        model = "deepseek/deepseek-v4.1-flash"
        tokens_prompt = 100
        tokens_completion = 50
        total_tokens = 150
        measured_cost = 0.0003
        estimated_cost = 0.0003
        is_measured = True

    class _FakeProvider:
        def __init__(self, *args, **kwargs):
            pass

        def generate(self, prompt, *, model, **kwargs):
            calls["prompt"] = prompt
            calls["model"] = model
            return _FakeResponse()

    monkeypatch.setattr("core.execution.providers.OpenRouterModelProvider", _FakeProvider)

    req = AgentRequest(prompt="review this diff", cwd=tmp_path, mode="read", harness="openrouter")
    result = run_agent(req)

    assert result.ok is True
    assert result.text == "cheap review text"
    assert result.cost_usd == pytest.approx(0.0003)
    assert calls["prompt"] == "review this diff"


def test_not_installed_returns_error_kind_for_unknown_harness(tmp_path):
    req = AgentRequest(prompt="do work", cwd=tmp_path, mode="read", harness="totally-unknown")
    result = run_agent(req)

    assert result.ok is False
    assert result.error_kind == "not_installed"
