"""USR-486 T2: host preflight before the manual line reservation.

Runners are injected. These tests never execute gh, claude, codex or grok.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from types import SimpleNamespace
from typing import Any, Callable

import pytest

import run_ticket
from core.line import active_pull_request, host_preflight
from core.line.active_pull_request import (
    GH_AUTH_LOGIN_COMMAND,
    GH_AUTH_STATUS_COMMAND,
    GH_INSTALL_COMMAND,
)
from core.line.host_preflight import (
    GH_AUTH_STATUS_ARGV,
    GH_AUTH_STATUS_TIMEOUT_MESSAGE,
    NO_USABLE_HARNESS,
    run_host_preflight as real_preflight,
)

_LOGGED_IN = SimpleNamespace(
    returncode=0,
    stdout='{"loggedIn": true, "email": "owner@example.com"}',
    stderr="",
)
_CLAUDE_ARGV = ("claude", "auth", "status", "--json")
_CODEX_ARGV = ("codex", "login", "status")
_GROK_ARGV = ("grok", "models")
Runner = Callable[..., Any]


@pytest.fixture(autouse=True)
def _block_real_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("host preflight tried to execute a real CLI")

    monkeypatch.setattr(host_preflight.subprocess, "run", boom)


def _table(mapping: dict[tuple[str, ...], Any]) -> Runner:
    def run(argv: list[str], **_kwargs: object) -> Any:
        key = tuple(argv)
        if key not in mapping:
            raise AssertionError(f"unexpected probe {argv}")
        value = mapping[key]
        if isinstance(value, BaseException):
            raise value
        return value

    return run


def _ok_harness(overrides: dict[tuple[str, ...], Any] | None = None) -> dict[tuple[str, ...], Any]:
    table: dict[tuple[str, ...], Any] = {
        GH_AUTH_STATUS_ARGV: SimpleNamespace(returncode=0, stdout="", stderr=""),
        _CLAUDE_ARGV: _LOGGED_IN,
        _CODEX_ARGV: SimpleNamespace(returncode=0, stdout="Logged in", stderr=""),
        _GROK_ARGV: SimpleNamespace(returncode=0, stdout="grok-build", stderr=""),
    }
    if overrides:
        table.update(overrides)
    return table


def _install(monkeypatch: pytest.MonkeyPatch, runner: Runner) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def wrapped(**kwargs: Any) -> Any:
        report = real_preflight(run=runner, **kwargs)
        seen["report"] = report
        return report

    monkeypatch.setattr(host_preflight, "run_host_preflight", wrapped)
    return seen


def _guard(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    calls: list[bool] = []

    @contextlib.contextmanager
    def guard(*_args: object, **_kwargs: object) -> Any:
        calls.append(True)
        yield SimpleNamespace()

    monkeypatch.setattr(run_ticket.line_lock, "guard", guard)
    return calls


def _stage(argv: list[str], monkeypatch: pytest.MonkeyPatch, runner: Runner) -> tuple[int | None, str, list[bool], dict[str, Any]]:
    seen = _install(monkeypatch, runner)
    calls = _guard(monkeypatch)
    args = run_ticket.build_parser().parse_args(argv)
    code = run_ticket._stage_line_lock(args, contextlib.ExitStack(), None)
    return code, "", calls, seen


@pytest.mark.parametrize(
    ("failure", "cause", "fix_command"),
    [
        (FileNotFoundError("gh"), "gh_missing", GH_INSTALL_COMMAND),
        (SimpleNamespace(returncode=1, stdout="", stderr="You are not logged in"), "gh_unauthenticated", GH_AUTH_LOGIN_COMMAND),
        (subprocess.TimeoutExpired(["gh", "auth", "status"], 20), "gh_timeout", GH_AUTH_STATUS_COMMAND),
    ],
)
def test_gh_failure_refuses_before_reservation(
    failure: Any,
    cause: str,
    fix_command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    classified: list[dict[str, Any]] = []
    real_classify = active_pull_request.classify_gh_call
    real_nonzero = active_pull_request._classify_gh_result

    def classify(outcome: Any, **kwargs: Any) -> Any:
        classified.append(kwargs)
        return real_classify(outcome, **kwargs)

    def nonzero(result: Any, repo: str, **kwargs: Any) -> Any:
        classified.append({"nonzero": True, "repo": repo})
        return real_nonzero(result, repo, **kwargs)

    monkeypatch.setattr(active_pull_request, "classify_gh_call", classify)
    monkeypatch.setattr(active_pull_request, "_classify_gh_result", nonzero)
    code, _err, calls, _seen = _stage(
        ["USR-486", "--json"],
        monkeypatch,
        _table({GH_AUTH_STATUS_ARGV: failure}),
    )
    payload = json.loads(capsys.readouterr().err)

    assert run_ticket._needs_line_lock(run_ticket.build_parser().parse_args(["USR-486", "--json"])) is True
    assert code == 2
    assert calls == []
    assert payload["status"] == "refused"
    assert payload["reason"] == "host_preflight"
    assert payload["cause"] == cause
    assert payload["fix_command"] == fix_command
    assert payload["ticket_id"] == "USR-486"
    assert classified
    assert classified[0]["github_fix_command"] == GH_AUTH_STATUS_COMMAND
    assert classified[0]["timeout_message"] == GH_AUTH_STATUS_TIMEOUT_MESSAGE
    assert any(item.get("nonzero") for item in classified) is (cause == "gh_unauthenticated")


def test_preflight_runs_before_guard_and_reuses_the_classifier(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    real_classify = active_pull_request.classify_gh_call

    def classify(outcome: Any, **kwargs: Any) -> Any:
        order.append("classify")
        return real_classify(outcome, **kwargs)

    @contextlib.contextmanager
    def guard(*_args: object, **_kwargs: object) -> Any:
        order.append("guard")
        yield SimpleNamespace()

    def runner(argv: list[str], **_kwargs: object) -> Any:
        order.append(" ".join(argv))
        return _LOGGED_IN

    monkeypatch.setattr(active_pull_request, "classify_gh_call", classify)
    monkeypatch.setattr(run_ticket.line_lock, "guard", guard)
    monkeypatch.setattr(
        host_preflight,
        "run_host_preflight",
        lambda **kwargs: real_preflight(run=runner, **kwargs),
    )
    args = run_ticket.build_parser().parse_args(["USR-486"])
    assert run_ticket._needs_line_lock(args) is True
    assert run_ticket._stage_line_lock(args, contextlib.ExitStack(), None) is None
    assert order[0] == "gh auth status"
    assert order.index("classify") < order.index("guard")
    assert order[-1] == "guard"


def test_quota_gaps_do_not_refuse_when_codex_can_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = _table(_ok_harness({
        _CLAUDE_ARGV: SimpleNamespace(returncode=0, stdout='{"loggedIn": false}', stderr=""),
        _GROK_ARGV: SimpleNamespace(returncode=1, stdout="", stderr="not logged in"),
    }))
    code, _err, calls, seen = _stage(["USR-486"], monkeypatch, runner)
    err = capsys.readouterr().err
    report = seen["report"]

    assert code is None
    assert calls == [True]
    assert [(gap.harness, gap.cause, gap.fix_command) for gap in report.gaps] == [
        ("claude", "quota_unread", "claude auth login"),
        ("grok", "quota_unread", "grok login"),
    ]
    assert "lacuna de cota claude" in err
    assert "claude auth login" in err
    assert "lacuna de cota grok" in err
    assert "grok login" in err


def test_requested_grok_without_auth_refuses_before_reservation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = _table(_ok_harness({
        _GROK_ARGV: SimpleNamespace(returncode=1, stdout="", stderr="not logged in"),
    }))
    code, _err, calls, _seen = _stage(["USR-486", "--harness", "grok", "--json"], monkeypatch, runner)
    payload = json.loads(capsys.readouterr().err)

    assert code == 2
    assert calls == []
    assert payload["reason"] == "host_preflight"
    assert payload["cause"] == "grok_unauthenticated"
    assert payload["fix_command"] == "grok login"


def test_no_usable_harness_refuses_before_reservation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    logged_out = SimpleNamespace(returncode=1, stdout="", stderr="not logged in")
    runner = _table(_ok_harness({
        _CLAUDE_ARGV: logged_out,
        _CODEX_ARGV: logged_out,
        _GROK_ARGV: logged_out,
    }))
    code, _err, calls, _seen = _stage(["USR-486", "--json"], monkeypatch, runner)
    payload = json.loads(capsys.readouterr().err)
    fixes = {gap["harness"]: gap["fix_command"] for gap in payload["gaps"]}

    assert code == 2
    assert calls == []
    assert payload["reason"] == "host_preflight"
    assert payload["cause"] == NO_USABLE_HARNESS
    assert fixes == {
        "claude": "claude auth login",
        "codex": "codex login",
        "grok": "grok login",
    }
    for command in fixes.values():
        assert command in payload["fix_command"]


def test_inherited_delivery_skips_probes_and_reaches_guard(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probes: list[str] = []

    def runner(argv: list[str], **_kwargs: object) -> Any:
        probes.append(" ".join(argv))
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(host_preflight, "run_host_preflight", real_preflight)
    monkeypatch.setattr(host_preflight, "default_runner", runner)
    calls = _guard(monkeypatch)
    monkeypatch.setenv(run_ticket.line_lock.ENV_INHERITED, "1")
    args = run_ticket.build_parser().parse_args(["--resume-delivery", "USR-486", "--json"])

    assert run_ticket._needs_line_lock(args) is True
    assert run_ticket.line_lock.is_inherited() is True
    assert run_ticket._stage_line_lock(args, contextlib.ExitStack(), "USR-486") is None
    assert probes == []
    assert calls == [True]
    assert capsys.readouterr().err == ""


def test_standalone_resume_delivery_still_preflights_before_reservation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(run_ticket.line_lock.ENV_INHERITED, raising=False)
    code, _err, calls, _seen = _stage(
        ["--resume-delivery", "USR-486", "--json"],
        monkeypatch,
        _table({GH_AUTH_STATUS_ARGV: FileNotFoundError("gh")}),
    )
    payload = json.loads(capsys.readouterr().err)

    assert run_ticket._needs_line_lock(
        run_ticket.build_parser().parse_args(["--resume-delivery", "USR-486", "--json"])
    ) is True
    assert code == 2
    assert calls == []
    assert payload["reason"] == "host_preflight"
    assert payload["cause"] == "gh_missing"
    assert payload["fix_command"] == GH_INSTALL_COMMAND


def test_dry_run_does_not_invoke_preflight_runners(monkeypatch: pytest.MonkeyPatch) -> None:
    def runner(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("dry-run invoked a preflight runner")

    monkeypatch.setattr(host_preflight, "run_host_preflight", real_preflight)
    monkeypatch.setattr(host_preflight, "default_runner", runner)
    args = run_ticket.build_parser().parse_args(["USR-486", "--dry-run"])
    assert run_ticket._needs_line_lock(args) is False
    assert run_ticket._stage_line_lock(args, contextlib.ExitStack(), None) is None
