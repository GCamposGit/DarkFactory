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
        {"record_path": str(record_path), "tmp_out_content": "review ok", "returncode": 0},
    )
    req = AgentRequest(prompt="review", cwd=tmp_path, mode="read", harness="codex", model=None)
    result = run_agent(req)

    assert result.ok is True
    record = json.loads(record_path.read_text(encoding="utf-8"))
    argv = record["argv"]
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "-m" not in argv  # model omitted when None


def test_codex_cloud_bypass_uses_stdin_without_explicit_model(tmp_path, make_fake_cli, monkeypatch):
    cmd_path, env_var = make_fake_cli("codex")
    monkeypatch.setattr(agent_cli, "find_codex_binary", lambda: cmd_path)
    monkeypatch.setenv(agent_cli.CODEX_SANDBOX_ENV, "bypass")
    record_path = tmp_path / "record.json"
    set_fake_response(
        monkeypatch,
        env_var,
        {"record_path": str(record_path), "tmp_out_content": "done", "returncode": 0},
    )

    result = run_agent(AgentRequest(prompt="implement", cwd=tmp_path, mode="write", harness="codex"))

    assert result.ok is True
    assert result.text == "done"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["argv"][:4] == [
        "exec", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "--json",
    ]
    assert "-m" not in record["argv"]
    assert record["argv"][-1] == "-"
    assert record["stdin"] == "implement"


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


def test_codex_helper_unknown_error_nonzero_exit_is_harness_infra_error(tmp_path, make_fake_cli, monkeypatch):
    """USR-188: Codex's own sandbox/terminal setup failing is infra, not a generic crash."""
    cmd_path, env_var = make_fake_cli("codex")
    monkeypatch.setattr(agent_cli, "find_codex_binary", lambda: cmd_path)
    set_fake_response(
        monkeypatch,
        env_var,
        {"stderr": "helper_unknown_error: setup refresh had errors", "returncode": 1},
    )
    req = AgentRequest(prompt="implement ticket", cwd=tmp_path, mode="write", harness="codex")
    result = run_agent(req)

    assert result.ok is False
    assert result.error_kind == "harness_infra_error"


def test_codex_helper_unknown_error_exit_zero_is_still_a_failure(tmp_path, make_fake_cli, monkeypatch):
    """The 09/10/2026 incident: Codex gave up on its sandboxed terminal but still exited 0 with no
    real work done, so the exit code alone must never be trusted when this signature is present."""
    cmd_path, env_var = make_fake_cli("codex")
    monkeypatch.setattr(agent_cli, "find_codex_binary", lambda: cmd_path)
    record_path = tmp_path / "record.json"
    set_fake_response(
        monkeypatch,
        env_var,
        {
            "record_path": str(record_path),
            "stdout": "helper_unknown_error: setup refresh had errors",
            "tmp_out_content": "",
            "returncode": 0,
        },
    )
    req = AgentRequest(prompt="implement ticket", cwd=tmp_path, mode="write", harness="codex")
    result = run_agent(req)

    assert result.ok is False
    assert result.error_kind == "harness_infra_error"
    assert result.exit_code == 0


def test_codex_helper_unknown_error_puts_the_harness_in_a_cooldown(tmp_path, make_fake_cli, monkeypatch):
    """`harness_infra_error` is cooled down like `crash` (USR-188), so the router stops re-electing a
    Codex blocked by the Desktop app's sandbox lock straight away."""
    from core.line import routing

    cmd_path, env_var = make_fake_cli("codex")
    monkeypatch.setattr(agent_cli, "find_codex_binary", lambda: cmd_path)
    set_fake_response(
        monkeypatch,
        env_var,
        {"stderr": "helper_unknown_error: setup refresh had errors", "returncode": 1},
    )
    result = run_agent(AgentRequest(prompt="implement", cwd=tmp_path, mode="write", harness="codex"))
    cooldown_path = tmp_path / "codex_cooldowns.json"

    routing.record_result(result, config=routing.RoutingConfig(), cooldown_path=cooldown_path)

    stored = json.loads(cooldown_path.read_text(encoding="utf-8"))
    assert stored["codex"]["reason"] == "harness_infra_error"


def test_codex_write_mode_forwards_the_declared_reasoning_effort(tmp_path, make_fake_cli, monkeypatch):
    """USR-201: a stage's harness pin reaches the Codex CLI as `-c model_reasoning_effort="..."`."""
    cmd_path, env_var = make_fake_cli("codex")
    monkeypatch.setattr(agent_cli, "find_codex_binary", lambda: cmd_path)
    record_path = tmp_path / "record.json"
    set_fake_response(
        monkeypatch,
        env_var,
        {"record_path": str(record_path), "tmp_out_content": "done", "returncode": 0},
    )

    req = AgentRequest(
        prompt="implement ticket HF-1", cwd=tmp_path, mode="write", harness="codex",
        model="gpt-6-luna", effort="high",
    )
    result = run_agent(req)

    assert result.ok is True
    record = json.loads(record_path.read_text(encoding="utf-8"))
    argv = record["argv"]
    assert "-m" in argv and argv[argv.index("-m") + 1] == "gpt-6-luna"
    assert "-c" in argv and argv[argv.index("-c") + 1] == 'model_reasoning_effort="high"'


def test_codex_write_mode_drops_an_effort_value_that_is_not_a_single_token(
    tmp_path, make_fake_cli, monkeypatch, caplog
):
    """A reasoning-effort value must never smuggle another flag into the Codex argv."""
    cmd_path, env_var = make_fake_cli("codex")
    monkeypatch.setattr(agent_cli, "find_codex_binary", lambda: cmd_path)
    record_path = tmp_path / "record.json"
    set_fake_response(
        monkeypatch,
        env_var,
        {"record_path": str(record_path), "tmp_out_content": "done", "returncode": 0},
    )

    req = AgentRequest(
        prompt="implement ticket HF-1", cwd=tmp_path, mode="write", harness="codex",
        effort="high --dangerously-bypass-approvals-and-sandbox",
    )
    with caplog.at_level("WARNING", logger="core.line.agent_cli"):
        result = run_agent(req)

    assert result.ok is True
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert "-c" not in record["argv"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in record["argv"]


def test_codex_effective_model_and_effort_are_read_from_the_cli_events_when_not_requested(
    tmp_path, make_fake_cli, monkeypatch
):
    """USR-201: with no model/effort declared, the effective values come from the CLI's own `--json` events."""
    cmd_path, env_var = make_fake_cli("codex")
    monkeypatch.setattr(agent_cli, "find_codex_binary", lambda: cmd_path)
    record_path = tmp_path / "record.json"
    tmp_out_content = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "deadbeef12345678"}),
            json.dumps({"model": "gpt-6-luna", "reasoning_effort": "high"}),
            json.dumps({"usage": {"input_tokens": 50, "output_tokens": 10}, "total_cost_usd": 0.001}),
        ]
    )
    set_fake_response(
        monkeypatch,
        env_var,
        {"record_path": str(record_path), "tmp_out_content": tmp_out_content, "returncode": 0},
    )

    req = AgentRequest(prompt="implement ticket HF-1", cwd=tmp_path, mode="write", harness="codex")
    result = run_agent(req)

    assert result.ok is True
    assert result.model == "gpt-6-luna"
    assert result.effort == "high"
    # Nothing was declared, so the executed argv carries neither flag -- the model only came back
    # on the CLI's report, not because it was asked for.
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert "-m" not in record["argv"] and "-c" not in record["argv"]


# Grok write mode (USR-109) is covered in tests/line/test_grok_runner.py.


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


def _capture_openrouter(monkeypatch):
    calls = {}

    class _FakeResponse:
        text = "ok"
        model = "m"
        tokens_prompt = tokens_completion = total_tokens = 1
        measured_cost = estimated_cost = 0.0
        is_measured = True

    class _FakeProvider:
        def __init__(self, *args, **kwargs):
            pass

        def generate(self, prompt, *, model, **kwargs):
            calls.update(kwargs)
            return _FakeResponse()

    monkeypatch.setattr("core.execution.providers.OpenRouterModelProvider", _FakeProvider)
    return calls


def test_openrouter_disables_reasoning_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("DARKFAC_OPENROUTER_REASONING", raising=False)
    calls = _capture_openrouter(monkeypatch)
    run_agent(AgentRequest(prompt="p", cwd=tmp_path, mode="read", harness="openrouter"))
    assert calls["reasoning"] == {"enabled": False}
    assert calls["max_tokens"] == 4096


@pytest.mark.parametrize("value", ["on", "true", "1", "ON"])
def test_openrouter_reasoning_env_override(tmp_path, monkeypatch, value):
    monkeypatch.setenv("DARKFAC_OPENROUTER_REASONING", value)
    calls = _capture_openrouter(monkeypatch)
    run_agent(AgentRequest(prompt="p", cwd=tmp_path, mode="read", harness="openrouter"))
    assert calls["reasoning"] is None


def test_not_installed_returns_error_kind_for_unknown_harness(tmp_path):
    req = AgentRequest(prompt="do work", cwd=tmp_path, mode="read", harness="totally-unknown")
    result = run_agent(req)

    assert result.ok is False
    assert result.error_kind == "not_installed"


# --------------------------------------------------------------------------
# Antigravity: a non-zero exit is a failure even when the CLI printed text (USR-230)
# --------------------------------------------------------------------------

_LS_ERROR_STDOUT = json.dumps({"error": "ANTIGRAVITY_LS_ADDRESS is not set"})


def _fake_antigravity(make_fake_cli, monkeypatch, spec):
    """Fake `antigravity` binary (a python shim from `write_python_shim`) driven by `spec`."""
    cmd_path, env_var = make_fake_cli("antigravity")
    monkeypatch.setattr(agent_cli, "find_antigravity_binary", lambda: cmd_path)
    set_fake_response(monkeypatch, env_var, spec)


def _antigravity_request(tmp_path):
    return AgentRequest(prompt="review the diff", cwd=tmp_path, mode="read", harness="antigravity")


def test_antigravity_exit_zero_with_text_is_success(tmp_path, make_fake_cli, monkeypatch):
    record_path = tmp_path / "record.json"
    _fake_antigravity(
        make_fake_cli, monkeypatch,
        {"record_path": str(record_path), "stdout": "  looks good to me\n", "returncode": 0},
    )
    result = run_agent(_antigravity_request(tmp_path))

    assert result.ok is True
    assert result.error_kind is None
    assert result.text == "looks good to me"
    assert result.exit_code == 0
    assert result.model == "flash"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["argv"] == ["new-conversation", "--model=flash", "review the diff"]


def test_antigravity_ls_address_missing_on_stdout_is_harness_unavailable(tmp_path, make_fake_cli, monkeypatch):
    """The 11/10/2026 incident: exit 1 + the error on STDOUT used to come back as ok=True review text."""
    _fake_antigravity(make_fake_cli, monkeypatch, {"stdout": _LS_ERROR_STDOUT, "returncode": 1})
    result = run_agent(_antigravity_request(tmp_path))

    assert result.ok is False
    assert result.error_kind == "crash"
    assert result.exit_code == 1
    assert "ANTIGRAVITY_LS_ADDRESS" in result.text
    assert "unavailable" in result.text.lower()


def test_antigravity_ls_address_missing_on_stderr_is_harness_unavailable(tmp_path, make_fake_cli, monkeypatch):
    _fake_antigravity(
        make_fake_cli, monkeypatch,
        {"stdout": "", "stderr": "fatal: ANTIGRAVITY_LS_ADDRESS is not set", "returncode": 2},
    )
    result = run_agent(_antigravity_request(tmp_path))

    assert result.ok is False
    assert result.error_kind == "crash"
    assert result.exit_code == 2
    assert "ANTIGRAVITY_LS_ADDRESS" in result.stderr_tail
    assert "unavailable" in result.text.lower()


def test_antigravity_ls_address_failure_puts_harness_in_routing_cooldown(tmp_path, make_fake_cli, monkeypatch):
    """`crash` is the kind routing cools down, so the router stops electing a harness with no language server."""
    from core.line import routing

    _fake_antigravity(make_fake_cli, monkeypatch, {"stdout": _LS_ERROR_STDOUT, "returncode": 1})
    result = run_agent(_antigravity_request(tmp_path))
    cooldown_path = tmp_path / "antigravity_cooldowns.json"

    routing.record_result(result, config=routing.RoutingConfig(), cooldown_path=cooldown_path)

    stored = json.loads(cooldown_path.read_text(encoding="utf-8"))
    assert stored["antigravity"]["reason"] == "crash"


def test_antigravity_nonzero_exit_with_verdict_like_stdout_is_not_ok(tmp_path, make_fake_cli, monkeypatch):
    """Non-empty stdout must not make a failed run look like a finished review."""
    verdict = json.dumps({"verdict": "APPROVED", "findings": []})
    _fake_antigravity(
        make_fake_cli, monkeypatch,
        {"stdout": verdict, "stderr": "traceback: something exploded", "returncode": 1},
    )
    result = run_agent(_antigravity_request(tmp_path))

    assert result.ok is False
    assert result.error_kind == "crash"
    assert result.exit_code == 1
    assert result.text == verdict  # preserved for diagnosis, never as a verdict (ok is False)
    assert result.stderr_tail == "traceback: something exploded"


def test_antigravity_nonzero_exit_without_any_output(tmp_path, make_fake_cli, monkeypatch):
    _fake_antigravity(make_fake_cli, monkeypatch, {"stdout": "", "stderr": "", "returncode": 1})
    result = run_agent(_antigravity_request(tmp_path))

    assert result.ok is False
    assert result.error_kind == "crash"
    assert result.exit_code == 1
    assert result.text == ""
    assert result.stderr_tail == ""


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected_kind"),
    [
        ("Error: usage limit exceeded. Try again in 2 hours.", "", "rate_limited"),
        ('{"error": "429 Too Many Requests: rate limit"}', "", "rate_limited"),
        ("Error: unauthorized (401). Please login again.", "", "auth_expired"),
        ("", "Error: session expired, login required", "auth_expired"),
    ],
)
def test_antigravity_nonzero_exit_classifies_rate_limit_and_auth(
    tmp_path, make_fake_cli, monkeypatch, stdout, stderr, expected_kind
):
    _fake_antigravity(make_fake_cli, monkeypatch, {"stdout": stdout, "stderr": stderr, "returncode": 1})
    result = run_agent(_antigravity_request(tmp_path))

    assert result.ok is False
    assert result.error_kind == expected_kind
    assert result.exit_code == 1
    if expected_kind == "rate_limited" and "2 hours" in stdout:
        assert result.reset_at is not None


def test_antigravity_failure_text_and_stderr_tail_are_redacted(tmp_path, make_fake_cli, monkeypatch):
    _fake_antigravity(
        make_fake_cli, monkeypatch,
        {
            "stdout": "boom ANTHROPIC_API_KEY=sk-ant-supersecret123 leaked",
            "stderr": "OPENAI_API_KEY=sk-live-abcdefghijklmnopqrstuvwxyz",
            "returncode": 1,
        },
    )
    result = run_agent(_antigravity_request(tmp_path))

    assert result.ok is False
    assert "sk-ant-supersecret123" not in result.text
    assert "[REDACTED]" in result.text
    assert "sk-live-abcdefghijklmnopqrstuvwxyz" not in result.stderr_tail
    assert "[REDACTED]" in result.stderr_tail


def test_antigravity_timeout_keeps_timeout_error_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_cli, "find_antigravity_binary", lambda: "antigravity-fake-path")

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="antigravity", timeout=1)

    monkeypatch.setattr(agent_cli.subprocess, "run", _raise_timeout)
    req = AgentRequest(prompt="review the diff", cwd=tmp_path, mode="read", harness="antigravity", timeout_s=1)
    result = run_agent(req)

    assert result.ok is False
    assert result.error_kind == "timeout"
