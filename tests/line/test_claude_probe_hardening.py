"""Claude probe hardening: neutral cwd, redacted/visible failure detail, drop logging.

Production evidence: with a valid CLAUDE_CODE_OAUTH_TOKEN the probe was dropped
after ~68s (the 60s timeout) with no diagnostics, and each probe cost ~$0.2
because it ran with cwd=repo root (CLAUDE.md, AGENTS.md, .claude/settings.json).
All fakes below: no real CLI, no network.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

from core.line import agent_cli, auth_bootstrap
from core.line.agent_cli import AgentRequest, AgentResult, redact_secrets, run_agent
from core.orchestrator.cloud_worker import CloudWorker

SECRET = "sk-ant-oat01-AbCdEf_0123456789-SECRETSECRETSECRET"


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------


def test_redact_secrets_covers_tokens_bearer_and_env_assignments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "customvalue-not-shaped-like-a-token")
    text = (
        f"auth failed for {SECRET}; Authorization: Bearer abc.DEF-123 ; "
        "CLAUDE_CODE_OAUTH_TOKEN=whatever1234 ; literal customvalue-not-shaped-like-a-token ; sk-proj_abcdefghijklmnopqrstu"
    )
    out = redact_secrets(text)
    for leaked in (SECRET, "SECRETSECRET", "abc.DEF-123", "whatever1234", "customvalue-not-shaped", "abcdefghijklmnopqrstu"):
        assert leaked not in out
    assert "auth failed for" in out and "[REDACTED]" in out
    assert redact_secrets(None) == "" and redact_secrets("") == ""


# --------------------------------------------------------------------------
# probe_claude: neutral cwd + minimal request
# --------------------------------------------------------------------------


def test_probe_claude_runs_in_neutral_empty_dir_with_minimal_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run_agent(req: AgentRequest) -> AgentResult:
        seen["req"] = req
        seen["cwd_exists"] = req.cwd.is_dir()
        seen["cwd_entries"] = list(req.cwd.iterdir())
        return AgentResult(ok=True, text="OK", harness="claude", duration_s=1.0)

    monkeypatch.setattr(auth_bootstrap, "run_agent", fake_run_agent)
    result = auth_bootstrap.probe_claude()

    req: AgentRequest = seen["req"]
    assert result.ok is True
    assert req.mode == "read" and req.harness == "claude"
    assert req.max_turns == 1
    assert req.timeout_s == 120
    assert req.prompt == "Reply with the single word OK"
    # Neutral: exists and is EMPTY while the probe runs (no CLAUDE.md/AGENTS.md/.claude/hooks)...
    assert seen["cwd_exists"] is True and seen["cwd_entries"] == []
    assert req.cwd.resolve() != auth_bootstrap.REPO_ROOT.resolve()
    assert not (req.cwd / "CLAUDE.md").exists()
    # ...and cleaned up afterwards.
    assert not req.cwd.exists()


def test_probe_claude_failure_detail_has_error_kind_and_redacted_truncated_text(monkeypatch: pytest.MonkeyPatch) -> None:
    long_text = f"401 invalid token {SECRET} Bearer zzz.yyy " + "x" * 2000

    monkeypatch.setattr(
        auth_bootstrap,
        "run_agent",
        lambda req: AgentResult(ok=False, text=long_text, harness="claude", duration_s=2.0, error_kind="auth_expired"),
    )
    result = auth_bootstrap.probe_claude()

    assert result.ok is False
    assert result.detail.startswith("auth_expired: 401 invalid token")
    assert SECRET not in result.detail and "zzz.yyy" not in result.detail
    assert len(result.detail) <= len("auth_expired: ") + auth_bootstrap.PROBE_DETAIL_MAX_CHARS


def test_probe_claude_timeout_detail_mentions_timeout_and_partial_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_cli, "find_claude_binary", lambda: "claude-fake")

    def _timeout(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["cwd"] != str(auth_bootstrap.REPO_ROOT)
        raise subprocess.TimeoutExpired(
            cmd="claude", timeout=120, output=f"starting {SECRET}".encode(), stderr=b"connecting to api.anthropic.com"
        )

    monkeypatch.setattr(agent_cli.subprocess, "run", _timeout)
    result = auth_bootstrap.probe_claude()

    assert result.ok is False
    assert result.detail.startswith("timeout: timed out after 120s")
    assert "connecting to api.anthropic.com" in result.detail
    assert SECRET not in result.detail


# --------------------------------------------------------------------------
# _run_claude failure text
# --------------------------------------------------------------------------


def test_run_claude_timeout_keeps_partial_output_redacted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(agent_cli, "find_claude_binary", lambda: "claude-fake")

    def _timeout(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=5, output=None, stderr=f"hang {SECRET}")

    monkeypatch.setattr(agent_cli.subprocess, "run", _timeout)
    result = run_agent(AgentRequest(prompt="p", cwd=tmp_path, mode="read", harness="claude", timeout_s=5))
    assert result.error_kind == "timeout"
    assert result.text.startswith("timed out after 5s: hang")
    assert SECRET not in result.text


def test_run_claude_timeout_without_partial_output_still_says_so(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(agent_cli, "find_claude_binary", lambda: "claude-fake")

    def _timeout(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(agent_cli.subprocess, "run", _timeout)
    result = run_agent(AgentRequest(prompt="p", cwd=tmp_path, mode="write", harness="claude", timeout_s=1))
    assert result.error_kind == "timeout" and result.text == "timed out after 1s"


def test_run_claude_failure_without_stdout_falls_back_to_redacted_stderr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(agent_cli, "find_claude_binary", lambda: "claude-fake")

    def _fail(*args: Any, **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr=f"Error: connect ECONNREFUSED; token {SECRET}"
        )

    monkeypatch.setattr(agent_cli.subprocess, "run", _fail)
    result = run_agent(AgentRequest(prompt="p", cwd=tmp_path, mode="read", harness="claude"))
    assert result.ok is False
    assert "ECONNREFUSED" in result.text
    assert SECRET not in result.text


# --------------------------------------------------------------------------
# dropped capability logging (claude + codex)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "harness,detail",
    [
        ("claude", "timeout: timed out after 120s: connecting to api.anthropic.com"),
        ("codex", "not logged in"),
    ],
)
def test_worker_logs_probe_detail_when_dropping_a_harness(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, harness: str, detail: str
) -> None:
    monkeypatch.setenv("DARKFAC_WORKER_CAPS", f"harness:{harness}")
    fake = auth_bootstrap.HarnessProbeResult(harness=harness, ok=False, detail=f"{detail} {SECRET}")
    monkeypatch.setitem(auth_bootstrap._PROBES, harness, lambda: fake)

    with caplog.at_level(logging.WARNING, logger="darkfac.cloud_worker"):
        worker = CloudWorker(
            worker_id="w-detail",
            capability_prober=auth_bootstrap.probe_harness_auth,
            capability_detail_lookup=auth_bootstrap.last_probe_detail,
            store=object(),
        )

    assert f"harness:{harness}" not in worker.capabilities
    assert f"Dropping capability harness:{harness}: auth probe failed ({detail}" in caplog.text
    assert SECRET not in caplog.text


def test_dropping_without_detail_or_with_failing_lookup_still_logs_and_drops(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DARKFAC_WORKER_CAPS", "harness:claude")

    def _boom(_harness: str) -> str:
        raise RuntimeError("lookup broke")

    with caplog.at_level(logging.WARNING, logger="darkfac.cloud_worker"):
        worker = CloudWorker(
            worker_id="w-nodetail",
            capability_prober=lambda h: False,
            capability_detail_lookup=_boom,
            store=object(),
        )
    assert "harness:claude" not in worker.capabilities
    assert "Dropping capability harness:claude: auth probe failed" in caplog.text


def test_healthy_probe_clears_stale_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = auth_bootstrap.HarnessProbeResult(harness="claude", ok=True, detail="ok")
    bad = auth_bootstrap.HarnessProbeResult(harness="claude", ok=False, detail="crash: boom")
    current = {"r": bad}
    monkeypatch.setitem(auth_bootstrap._PROBES, "claude", lambda: current["r"])

    assert auth_bootstrap.probe_harness_auth("claude") is False
    assert auth_bootstrap.last_probe_detail("claude") == "crash: boom"
    current["r"] = ok
    assert auth_bootstrap.probe_harness_auth("claude") is True
    assert auth_bootstrap.last_probe_detail("claude") == ""
