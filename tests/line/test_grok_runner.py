"""Tests for the Grok Build runner in write mode (USR-109).

`subprocess.run` and the binary finder are faked, so nothing here talks to the real Grok CLI; the one
real smoke test at the bottom is opt-in (`DARKFAC_LIVE_GROK_TEST=1`) and skipped by default and in the gate.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from core.line import agent_cli
from core.line.agent_cli import (
    GROK_DEFAULT_PERMISSION_MODE,
    GROK_PERMISSION_ENV,
    HARNESS_CAPABILITIES,
    AgentRequest,
    grok_permission_mode,
    run_agent,
    supports,
)

_PROMPT = "Implemente o ticket: crie a funcao ação() e rode os testes. sk-ant-api03-SECRETSECRET"
_GROK_OK_JSON = {
    "text": "Implementei a funcao e os testes passaram.",
    "stopReason": "end_turn",
    "sessionId": "01a0f7a4-b97e-7110-b94c-78dfc452eef3",
    "requestId": "aa917e84-301c-4d26-84c1-05569f2027d1",
    "usage": {
        "input_tokens": 32999,
        "cache_read_input_tokens": 2432,
        "cache_creation_input_tokens": 0,
        "output_tokens": 251,
        "reasoning_tokens": 77,
        "total_tokens": 35682,
    },
    "num_turns": 2,
    "total_cost_usd": 0.0233648,
    "modelUsage": {"grok-4.7-build": {"inputTokens": 32999, "outputTokens": 251, "costUSD": 0.0233648}},
}


@pytest.fixture(autouse=True)
def _hermetic_grok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No ambient permission override, and the prompt file / usage ledger go under tmp_path, never the repo."""
    monkeypatch.delenv(GROK_PERMISSION_ENV, raising=False)
    root = tmp_path / "factory_root"
    monkeypatch.setattr(agent_cli, "REPO_ROOT", root)
    return root


def _tmp_dir(root: Path) -> Path:
    return root / ".factory" / "tmp"


def _leftover_prompt_files(root: Path) -> list[Path]:
    directory = _tmp_dir(root)
    return sorted(directory.glob("grok_line_*")) if directory.is_dir() else []


class _FakeRun:
    """Stand-in for `subprocess.run` that records the call and reads the prompt file while it still exists."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        raises: Optional[BaseException] = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raises = raises
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.prompt_file_text: Optional[str] = None
        self.prompt_file_path: Optional[Path] = None

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), kwargs))
        if "--prompt-file" in argv:
            self.prompt_file_path = Path(argv[argv.index("--prompt-file") + 1])
            self.prompt_file_text = self.prompt_file_path.read_text(encoding="utf-8")
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)

    @property
    def argv(self) -> list[str]:
        return self.calls[-1][0]

    @property
    def kwargs(self) -> dict[str, Any]:
        return self.calls[-1][1]


@pytest.fixture
def install_fake(monkeypatch: pytest.MonkeyPatch):
    """`install_fake(**kwargs) -> _FakeRun`: fake Grok binary + fake `subprocess.run` for the runner."""

    def _install(**kwargs: Any) -> _FakeRun:
        fake = _FakeRun(**kwargs)
        monkeypatch.setattr(agent_cli, "find_grok_binary", lambda: "grok-fake")
        monkeypatch.setattr(agent_cli.subprocess, "run", fake)
        return fake

    return _install


def _write_request(cwd: Path, **overrides: Any) -> AgentRequest:
    fields: dict[str, Any] = {"prompt": _PROMPT, "cwd": cwd, "mode": "write", "harness": "grok", "timeout_s": 60}
    fields.update(overrides)
    return AgentRequest(**fields)


# --------------------------------------------------------------------------
# Capability and argv
# --------------------------------------------------------------------------


def test_grok_declares_write_and_antigravity_still_does_not() -> None:
    assert HARNESS_CAPABILITIES["grok"] == frozenset({"read", "write"})
    assert supports("grok", "write") is True
    assert supports("antigravity", "write") is False


def test_write_argv_uses_prompt_file_cwd_json_and_the_default_permission_mode(
    install_fake, tmp_path: Path
) -> None:
    fake = install_fake(stdout=json.dumps(_GROK_OK_JSON))
    cwd = tmp_path / "worktree"
    cwd.mkdir()

    result = run_agent(_write_request(cwd))

    assert result.ok is True and result.error_kind is None
    argv = fake.argv
    assert argv[0] == "grok-fake"
    assert "--prompt-file" in argv
    assert argv[argv.index("--cwd") + 1] == str(cwd)
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--permission-mode") + 1] == "auto" == GROK_DEFAULT_PERMISSION_MODE
    assert _PROMPT not in argv and not any("SECRETSECRET" in part for part in argv)  # never on the command line
    assert "-p" not in argv and "--single" not in argv
    assert "-m" not in argv and "--max-turns" not in argv  # only when the request sets them
    assert "--always-approve" not in argv and "bypassPermissions" not in argv


def test_write_runs_in_the_ticket_cwd_with_stdin_closed_and_the_request_timeout(
    install_fake, tmp_path: Path
) -> None:
    fake = install_fake(stdout=json.dumps(_GROK_OK_JSON))
    cwd = tmp_path / "worktree"
    cwd.mkdir()

    run_agent(_write_request(cwd, timeout_s=77))

    assert fake.kwargs["cwd"] == str(cwd)
    assert fake.kwargs["timeout"] == 77
    assert fake.kwargs["stdin"] is subprocess.DEVNULL
    assert "input" not in fake.kwargs


def test_the_prompt_file_holds_the_full_utf8_prompt_and_is_removed_afterwards(
    install_fake, tmp_path: Path, _hermetic_grok: Path
) -> None:
    fake = install_fake(stdout=json.dumps(_GROK_OK_JSON))

    run_agent(_write_request(tmp_path))

    assert fake.prompt_file_text == _PROMPT  # accents intact, read while the file existed
    assert fake.prompt_file_path is not None and fake.prompt_file_path.parent == _tmp_dir(_hermetic_grok)
    assert not fake.prompt_file_path.exists()
    assert _leftover_prompt_files(_hermetic_grok) == []


def test_each_run_uses_a_unique_prompt_file_name(install_fake, tmp_path: Path) -> None:
    fake = install_fake(stdout=json.dumps(_GROK_OK_JSON))

    run_agent(_write_request(tmp_path))
    first = fake.prompt_file_path
    run_agent(_write_request(tmp_path))

    assert first is not None and fake.prompt_file_path is not None and first != fake.prompt_file_path


def test_model_and_max_turns_are_forwarded_when_set(install_fake, tmp_path: Path) -> None:
    fake = install_fake(stdout=json.dumps(_GROK_OK_JSON))

    run_agent(_write_request(tmp_path, model="grok-4.7", max_turns=9))

    argv = fake.argv
    assert argv[argv.index("-m") + 1] == "grok-4.7"
    assert argv[argv.index("--max-turns") + 1] == "9"


# --------------------------------------------------------------------------
# DARKFAC_GROK_PERMISSION_MODE
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan"])
def test_permission_mode_override_accepts_each_valid_mode(
    mode: str, install_fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GROK_PERMISSION_ENV, mode)
    fake = install_fake(stdout=json.dumps(_GROK_OK_JSON))

    run_agent(_write_request(tmp_path))

    assert fake.argv[fake.argv.index("--permission-mode") + 1] == mode


def test_permission_mode_override_is_case_insensitive_and_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GROK_PERMISSION_ENV, "  BYPASSpermissions ")

    assert grok_permission_mode() == "bypassPermissions"


def test_an_invalid_permission_mode_warns_and_falls_back_to_the_default(
    install_fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(GROK_PERMISSION_ENV, "yolo-everything")
    fake = install_fake(stdout=json.dumps(_GROK_OK_JSON))

    with caplog.at_level(logging.WARNING, logger="core.line.agent_cli"):
        run_agent(_write_request(tmp_path))

    assert fake.argv[fake.argv.index("--permission-mode") + 1] == GROK_DEFAULT_PERMISSION_MODE
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and GROK_PERMISSION_ENV in r.getMessage()]
    assert warnings and "yolo-everything" in warnings[0].getMessage()


def test_an_empty_permission_override_means_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GROK_PERMISSION_ENV, "   ")

    assert grok_permission_mode() == GROK_DEFAULT_PERMISSION_MODE


# --------------------------------------------------------------------------
# Result parsing
# --------------------------------------------------------------------------


def test_success_returns_the_final_text_usage_cost_and_diagnostics(install_fake, tmp_path: Path) -> None:
    install_fake(stdout=json.dumps(_GROK_OK_JSON, indent=2), stderr="warn: something harmless")

    result = run_agent(_write_request(tmp_path, model="grok-4.7"))

    assert result.ok is True
    assert result.text == _GROK_OK_JSON["text"]
    assert result.usage == _GROK_OK_JSON["usage"]
    assert result.cost_usd == pytest.approx(0.0233648)
    assert result.harness == "grok" and result.model == "grok-4.7"
    assert result.exit_code == 0 and result.stderr_tail == "warn: something harmless"


def test_success_text_is_redacted(install_fake, tmp_path: Path) -> None:
    payload = {**_GROK_OK_JSON, "text": "done, token was sk-ant-api03-LEAKLEAKLEAK ok"}
    install_fake(stdout=json.dumps(payload))

    result = run_agent(_write_request(tmp_path))

    assert "LEAKLEAKLEAK" not in result.text and "[REDACTED]" in result.text


def test_write_success_without_usage_or_cost_still_succeeds(install_fake, tmp_path: Path) -> None:
    install_fake(stdout=json.dumps({"text": "ok", "stopReason": "end_turn"}))

    result = run_agent(_write_request(tmp_path))

    assert result.ok is True and result.usage is None and result.cost_usd is None


def test_write_success_with_empty_text_is_not_an_empty_output_failure(install_fake, tmp_path: Path) -> None:
    install_fake(stdout=json.dumps({"text": "", "stopReason": "end_turn"}))

    result = run_agent(_write_request(tmp_path))

    assert result.ok is True and result.error_kind is None  # write mode may legitimately print nothing


def test_non_json_stdout_on_success_is_kept_as_the_text(install_fake, tmp_path: Path) -> None:
    install_fake(stdout="plain words from grok\n")

    result = run_agent(_write_request(tmp_path))

    assert result.ok is True and result.text == "plain words from grok"


# --------------------------------------------------------------------------
# Error mapping (same classifier as the other harnesses)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected_kind"),
    [
        ("Usage limit reached. Your limit resets in 2 hours.", "rate_limited"),
        ("429 Too Many Requests: rate limit exceeded", "rate_limited"),
        ("Not signed in: please login again", "auth_expired"),
        ("401 Unauthorized: token expired", "auth_expired"),
        ("something exploded", "crash"),
    ],
)
def test_json_error_object_is_classified_with_the_shared_classifier(
    message: str, expected_kind: str, install_fake, tmp_path: Path
) -> None:
    install_fake(returncode=1, stdout=json.dumps({"type": "error", "message": message}), stderr=f"Error: {message}")

    result = run_agent(_write_request(tmp_path))

    assert result.ok is False
    assert result.error_kind == expected_kind == agent_cli._classify_error(message)
    assert message in result.text
    assert result.exit_code == 1 and message in result.stderr_tail


def test_rate_limit_error_carries_the_reset_time(install_fake, tmp_path: Path) -> None:
    message = "Usage limit reached. Resets in 2 hours."
    install_fake(returncode=1, stdout=json.dumps({"type": "error", "message": message}))

    result = run_agent(_write_request(tmp_path))

    assert result.error_kind == "rate_limited"
    assert result.reset_at is not None
    assert abs(result.reset_at - (datetime.now(timezone.utc) + timedelta(hours=2))) < timedelta(minutes=1)


def test_nonzero_exit_with_only_stderr_is_a_failure_with_the_stderr_as_text(install_fake, tmp_path: Path) -> None:
    install_fake(returncode=2, stderr="Error: Failed to read prompt file: not found")

    result = run_agent(_write_request(tmp_path))

    assert result.ok is False and result.error_kind == "crash"
    assert result.text == "Error: Failed to read prompt file: not found"
    assert result.exit_code == 2


def test_stderr_rate_limit_without_json_is_still_classified(install_fake, tmp_path: Path) -> None:
    install_fake(returncode=1, stderr="Error: rate limit exceeded (429)")

    result = run_agent(_write_request(tmp_path))

    assert result.ok is False and result.error_kind == "rate_limited"


def test_an_error_object_with_exit_zero_is_still_a_failure(install_fake, tmp_path: Path) -> None:
    install_fake(returncode=0, stdout=json.dumps({"type": "error", "message": "401 unauthorized"}))

    result = run_agent(_write_request(tmp_path))

    assert result.ok is False and result.error_kind == "auth_expired"


def test_the_agents_own_prose_is_never_classified_as_an_error(install_fake, tmp_path: Path) -> None:
    """Max turns exits 1 with a normal result object whose text may mention login or limits."""
    payload = {**_GROK_OK_JSON, "text": "I fixed the login form and the rate limit config.", "stopReason": "cancelled"}
    install_fake(returncode=1, stdout=json.dumps(payload), stderr="Error: max turns reached")

    result = run_agent(_write_request(tmp_path))

    assert result.ok is False and result.error_kind == "crash"
    assert result.text == payload["text"]  # the partial work report survives for diagnosis
    assert result.usage == _GROK_OK_JSON["usage"]


def test_cancelled_stop_with_exit_zero_is_a_crash_that_names_the_permission_mode(
    install_fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What `acceptEdits`/`dontAsk` do headless: the first tool call needing approval is cancelled, exit 0."""
    monkeypatch.setenv(GROK_PERMISSION_ENV, "acceptEdits")
    install_fake(stdout=json.dumps({"text": "Vou criar o arquivo.", "stopReason": "cancelled"}))

    result = run_agent(_write_request(tmp_path))

    assert result.ok is False and result.error_kind == "crash"
    assert "cancelled" in result.text and "acceptEdits" in result.text and "Vou criar o arquivo." in result.text


def test_error_text_is_redacted(install_fake, tmp_path: Path) -> None:
    install_fake(returncode=1, stdout=json.dumps({"type": "error", "message": "boom sk-ant-api03-LEAKLEAKLEAK"}))

    result = run_agent(_write_request(tmp_path))

    assert "LEAKLEAKLEAK" not in result.text and "[REDACTED]" in result.text


def test_timeout_is_classified_and_keeps_the_redacted_partial_output(install_fake, tmp_path: Path) -> None:
    install_fake(
        raises=subprocess.TimeoutExpired(
            cmd="grok", timeout=5, output=b"partial sk-ant-api03-LEAKLEAKLEAK", stderr=b"still thinking"
        )
    )

    result = run_agent(_write_request(tmp_path, timeout_s=5))

    assert result.ok is False and result.error_kind == "timeout"
    assert result.text.startswith("timed out after 5s")
    assert "partial" in result.text and "LEAKLEAKLEAK" not in result.text
    assert result.stderr_tail == "still thinking"


def test_timeout_without_partial_output_has_a_bare_message(install_fake, tmp_path: Path) -> None:
    install_fake(raises=subprocess.TimeoutExpired(cmd="grok", timeout=5))

    result = run_agent(_write_request(tmp_path, timeout_s=5))

    assert result.error_kind == "timeout" and result.text == "timed out after 5s"


def test_spawn_failure_is_a_redacted_crash(install_fake, tmp_path: Path) -> None:
    install_fake(raises=OSError("cannot spawn Bearer abcdefgh12345678"))

    result = run_agent(_write_request(tmp_path))

    assert result.ok is False and result.error_kind == "crash"
    assert "abcdefgh12345678" not in result.text


def test_missing_binary_is_not_installed_and_spawns_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("must not spawn without a binary")

    monkeypatch.setattr(agent_cli, "find_grok_binary", lambda: None)
    monkeypatch.setattr(agent_cli.subprocess, "run", _boom)

    result = run_agent(_write_request(tmp_path))

    assert result.ok is False and result.error_kind == "not_installed"


@pytest.mark.parametrize(
    "scenario",
    ["success", "error_exit", "timeout", "spawn_failure"],
)
def test_the_prompt_file_is_removed_in_every_outcome(
    scenario: str, install_fake, tmp_path: Path, _hermetic_grok: Path
) -> None:
    kwargs: dict[str, Any] = {
        "success": {"stdout": json.dumps(_GROK_OK_JSON)},
        "error_exit": {"returncode": 1, "stdout": json.dumps({"type": "error", "message": "boom"})},
        "timeout": {"raises": subprocess.TimeoutExpired(cmd="grok", timeout=1)},
        "spawn_failure": {"raises": OSError("nope")},
    }[scenario]
    fake = install_fake(**kwargs)

    run_agent(_write_request(tmp_path))

    assert fake.prompt_file_path is not None  # it did exist during the call
    assert _leftover_prompt_files(_hermetic_grok) == []


def test_an_unwritable_tmp_dir_is_a_crash_not_an_exception(
    install_fake, tmp_path: Path, _hermetic_grok: Path
) -> None:
    fake = install_fake(stdout=json.dumps(_GROK_OK_JSON))
    _hermetic_grok.mkdir(parents=True)
    (_hermetic_grok / ".factory").write_text("a file where the directory should be", encoding="utf-8")

    result = run_agent(_write_request(tmp_path))

    assert result.ok is False and result.error_kind == "crash"
    assert "prompt file" in result.text
    assert fake.calls == []  # never spawned without a prompt file


def test_run_agent_with_write_mode_on_grok_is_not_refused_as_unsupported(install_fake, tmp_path: Path) -> None:
    install_fake(stdout=json.dumps(_GROK_OK_JSON))

    result = run_agent(_write_request(tmp_path))

    assert result.error_kind != "unsupported_mode"
    assert result.ok is True


# --------------------------------------------------------------------------
# Read mode is unchanged
# --------------------------------------------------------------------------


def test_read_mode_keeps_the_plain_print_call(install_fake, tmp_path: Path, _hermetic_grok: Path) -> None:
    fake = install_fake(stdout="grok says hi\n")
    req = AgentRequest(prompt="review this", cwd=tmp_path, mode="read", harness="grok", model="grok-4.7", timeout_s=30)

    result = run_agent(req)

    assert fake.argv == ["grok-fake", "-p", "review this", "--output-format", "plain", "-m", "grok-4.7"]
    assert "--prompt-file" not in fake.argv and "--permission-mode" not in fake.argv
    assert fake.kwargs["cwd"] == str(tmp_path) and "stdin" not in fake.kwargs
    assert result.ok is True and result.text == "grok says hi"
    assert _leftover_prompt_files(_hermetic_grok) == []  # read mode never writes a prompt file


def test_read_mode_ignores_the_permission_override(
    install_fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GROK_PERMISSION_ENV, "bypassPermissions")
    fake = install_fake(stdout="analysis")

    run_agent(AgentRequest(prompt="analyse", cwd=tmp_path, mode="read", harness="grok"))

    assert "--permission-mode" not in fake.argv and "bypassPermissions" not in fake.argv


def test_read_mode_with_no_text_is_still_empty_output(install_fake, tmp_path: Path) -> None:
    install_fake(stdout="   ")

    result = run_agent(AgentRequest(prompt="analyse", cwd=tmp_path, mode="read", harness="grok"))

    assert result.ok is False and result.error_kind == "empty_output"


def test_read_mode_nonzero_exit_without_output_is_classified(install_fake, tmp_path: Path) -> None:
    install_fake(returncode=1, stderr="Error: usage limit reached")

    result = run_agent(AgentRequest(prompt="analyse", cwd=tmp_path, mode="read", harness="grok"))

    assert result.ok is False and result.error_kind == "rate_limited"


# --------------------------------------------------------------------------
# Real smoke test (opt-in): DARKFAC_LIVE_GROK_TEST=1 python -m pytest tests/line/test_grok_runner.py -k live
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("DARKFAC_LIVE_GROK_TEST") or shutil.which("grok") is None,
    reason="opt-in live smoke test: set DARKFAC_LIVE_GROK_TEST=1 with the Grok Build CLI on PATH",
)
def test_live_grok_creates_a_file_in_a_temporary_git_repository(tmp_path: Path) -> None:
    repo = tmp_path / "live_repo"
    repo.mkdir()
    flags: dict[str, Any] = {"cwd": str(repo), "capture_output": True, "text": True, "check": True}
    if sys.platform == "win32":
        flags["creationflags"] = subprocess.CREATE_NO_WINDOW
    for git_args in (
        ["init", "-q"],
        ["config", "user.email", "smoke@example.com"],
        ["config", "user.name", "Smoke"],
    ):
        subprocess.run(["git", *git_args], **flags)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], **flags)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], **flags)

    result = run_agent(
        AgentRequest(
            prompt="Create the file hello.txt in the repository root containing exactly the word: ok. "
            "Do not run any shell command and do not touch any other file.",
            cwd=repo,
            mode="write",
            harness="grok",
            timeout_s=300,
            max_turns=8,
        )
    )

    assert result.ok is True, f"{result.error_kind}: {result.text}"
    created = repo / "hello.txt"
    assert created.is_file(), f"Grok reported success but created nothing: {result.text}"
    assert "ok" in created.read_text(encoding="utf-8").lower()
