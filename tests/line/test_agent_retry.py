"""Shared agent-failure policy (USR-68): retry, exclusion, cooldown and diagnostics.

`run_with_retry` is the launcher loop built on the policy that `DevelopmentStage` already followed
(`core.line.agent_retry`); every test here drives it with fake agents and an injected `sleep_fn`, so
nothing waits and nothing touches the network or a real CLI.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import pytest

import run_ticket
from core.line import agent_cli, agent_retry, stage_build
from core.line.agent_cli import STDERR_TAIL_CHARS, AgentRequest, AgentResult, run_agent
from core.line.agent_retry import RetryReport, classify_failure, run_with_retry
from core.line.routing import RoutingConfig, record_result
from tests.line.conftest import set_fake_response

SECRET = "sk-ant-api03-TOPSECRETTOKENVALUE1234567890"
_CAPS = ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"]


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


def _ok(harness: str, model: Optional[str] = None) -> AgentResult:
    return AgentResult(ok=True, text="done", harness=harness, model=model, duration_s=0.5, exit_code=0)


def _fail(
    kind: str,
    harness: str,
    model: Optional[str] = None,
    *,
    exit_code: Optional[int] = 1,
    stderr: str = "boom on stderr",
    duration: float = 1.5,
    text: str = "",
    reset_at: Optional[datetime] = None,
) -> AgentResult:
    return AgentResult(
        ok=False, text=text, harness=harness, model=model, duration_s=duration, error_kind=kind,  # type: ignore[arg-type]
        exit_code=exit_code, stderr_tail=stderr, reset_at=reset_at,
    )


class _Agent:
    """`run_func` double: `script[harness]` is consumed one result per call (the last repeats)."""

    def __init__(self, script: dict[str, list[AgentResult]]) -> None:
        self.script = {harness: list(results) for harness, results in script.items()}
        self.calls: list[AgentRequest] = []

    def __call__(self, req: AgentRequest) -> AgentResult:
        self.calls.append(req)
        queue = self.script[req.harness]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    @property
    def harnesses(self) -> list[str]:
        return [c.harness for c in self.calls]


class _Picker:
    """`pick_func` double honouring `exclude` like routing.pick and recording every call."""

    def __init__(self, routes: list[tuple[str, Optional[str]]]) -> None:
        self.routes = routes
        self.calls: list[dict] = []

    def __call__(self, stage, caps, config=None, exclude=(), mode=None, **kwargs):
        self.calls.append({"stage": stage, "exclude": set(exclude), "mode": mode, "config": config})
        for route in self.routes:
            if route not in set(exclude):
                return route
        return None


def _request(harness: str, model: Optional[str]) -> AgentRequest:
    return AgentRequest(prompt="implement", cwd=Path("."), mode="write", harness=harness, model=model)


def _run(
    agent: _Agent,
    first: Optional[tuple[str, Optional[str]]],
    picker: _Picker,
    sleeps: list[float],
    **kwargs,
) -> RetryReport:
    kwargs.setdefault("record_func", lambda *a, **k: None)
    return run_with_retry(
        _request, first, host_caps=_CAPS, run_func=agent, pick_func=picker, sleep_fn=sleeps.append, **kwargs
    )


# --------------------------------------------------------------------------
# Transient failures: retry with backoff, then next harness
# --------------------------------------------------------------------------


def test_empty_output_is_retried_after_a_30s_backoff_and_then_succeeds() -> None:
    agent = _Agent({"codex": [_fail("empty_output", "codex", exit_code=0, stderr=""), _ok("codex")]})
    sleeps: list[float] = []

    report = _run(agent, ("codex", None), _Picker([("codex", None), ("claude", "sonnet")]), sleeps)

    assert report.ok and report.route == ("codex", None)
    assert agent.harnesses == ["codex", "codex"]  # same route, no fallback needed
    assert sleeps == [30.0]
    assert [a.error_kind for a in report.attempts] == ["empty_output", None]


def test_timeout_repeats_at_most_once_with_30s_backoff_and_then_moves_to_the_next_harness() -> None:
    # USR-114: A timeout is limited to at most 1 retry (2 attempts total per route, not 3)
    agent = _Agent(
        {"codex": [_fail("timeout", "codex", exit_code=None)], "claude": [_ok("claude", "sonnet")]}
    )
    picker = _Picker([("codex", None), ("claude", "sonnet")])
    sleeps: list[float] = []

    report = _run(agent, ("codex", None), picker, sleeps, has_changes_fn=lambda cwd: False)

    assert report.ok and report.route == ("claude", "sonnet")
    assert agent.harnesses == ["codex", "codex", "claude"]  # initial + 1 repeat, then fallback
    assert sleeps == [30.0]
    assert picker.calls[-1]["exclude"] == {("codex", None)}
    assert picker.calls[-1]["mode"] == "write" and picker.calls[-1]["stage"] == "development"


def test_timeout_proceeds_to_validate_when_worktree_has_changes() -> None:
    # USR-114: When implementation changes already exist, a timeout proceeds to validation immediately
    agent = _Agent({"codex": [_fail("timeout", "codex", exit_code=None)]})
    picker = _Picker([("codex", None), ("claude", "sonnet")])
    sleeps: list[float] = []

    report = _run(agent, ("codex", None), picker, sleeps, has_changes_fn=lambda cwd: True)

    assert report.ok is True
    assert report.route == ("codex", None)
    assert report.result is not None and report.result.error_kind == "timeout"
    assert agent.harnesses == ["codex"]  # stops at attempt 1 without repeating from scratch
    assert sleeps == []


def test_a_crash_is_transient_too_and_a_later_success_stops_the_loop() -> None:
    agent = _Agent({"codex": [_fail("crash", "codex"), _fail("crash", "codex"), _ok("codex")]})
    sleeps: list[float] = []

    report = _run(agent, ("codex", None), _Picker([("codex", None)]), sleeps)

    assert report.ok and len(report.attempts) == 3
    assert sleeps == [30.0, 120.0]


def test_a_missing_error_kind_is_treated_as_transient() -> None:
    assert classify_failure(AgentResult(ok=False, text="", harness="codex", duration_s=0.1)) == "transient"


# --------------------------------------------------------------------------
# Immediate exclusion: capability, binary, login
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["unsupported_mode", "not_installed", "auth_expired", "no_authenticated_harness"])
def test_capability_binary_and_login_failures_exclude_the_harness_without_waiting(kind: str) -> None:
    agent = _Agent({"grok": [_fail(kind, "grok")], "claude": [_ok("claude", "sonnet")]})
    picker = _Picker([("grok", None), ("claude", "sonnet")])
    sleeps: list[float] = []

    report = _run(agent, ("grok", None), picker, sleeps)

    assert report.ok and report.route == ("claude", "sonnet")
    assert agent.harnesses == ["grok", "claude"]
    assert sleeps == []  # retrying a harness that cannot work is pointless
    assert picker.calls[-1]["exclude"] == {("grok", None)}
    assert classify_failure(_fail(kind, "grok")) == "exclude"


def test_the_real_antigravity_write_refusal_is_skipped_immediately_in_favour_of_the_next_harness(
    tmp_path: Path, make_fake_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end over the real runners: `unsupported_mode` from run_agent, then a fake Claude CLI answers."""
    cmd_path, env_var = make_fake_cli("claude")
    monkeypatch.setattr(agent_cli, "find_claude_binary", lambda: cmd_path)
    monkeypatch.setattr(
        agent_cli, "find_antigravity_binary", lambda: (_ for _ in ()).throw(AssertionError("never looked up"))
    )
    set_fake_response(monkeypatch, env_var, {"stdout": json.dumps({"result": "implemented", "is_error": False})})
    sleeps: list[float] = []
    picker = _Picker([("antigravity", None), ("claude", "sonnet")])

    report = run_with_retry(
        lambda h, m: AgentRequest(prompt="x", cwd=tmp_path, mode="write", harness=h, model=m),
        ("antigravity", None), host_caps=_CAPS, run_func=run_agent, pick_func=picker, sleep_fn=sleeps.append,
        record_func=lambda *a, **k: None,
    )

    assert report.ok and report.route == ("claude", "sonnet")
    assert [a.error_kind for a in report.attempts] == ["unsupported_mode", None]
    assert sleeps == []
    assert report.attempts[0].harness == "antigravity" and report.attempts[0].exit_code is None


# --------------------------------------------------------------------------
# Quota: cooldown via routing.record_result (reset_at honoured), then next harness
# --------------------------------------------------------------------------


def test_rate_limit_records_a_cooldown_from_reset_at_and_moves_on_without_waiting(tmp_path: Path) -> None:
    cooldown = tmp_path / "cooldowns.json"
    reset_at = datetime.now(timezone.utc) + timedelta(hours=3)
    agent = _Agent(
        {
            "claude": [_fail("rate_limited", "claude", "sonnet", reset_at=reset_at)],
            "codex": [_ok("codex")],
        }
    )
    sleeps: list[float] = []

    report = _run(
        agent, ("claude", "sonnet"), _Picker([("claude", "sonnet"), ("codex", None)]), sleeps,
        record_func=lambda result, config=None: record_result(result, config=config, cooldown_path=cooldown),
    )

    assert report.ok and report.route == ("codex", None)
    assert sleeps == []
    entry = json.loads(cooldown.read_text(encoding="utf-8"))["claude"]
    assert entry["reason"] == "rate_limited"
    assert abs((datetime.fromisoformat(entry["until"]) - reset_at).total_seconds()) < 1  # `reset_at`, not the default
    assert classify_failure(_fail("rate_limited", "claude")) == "cooldown"


def test_a_crash_puts_the_harness_in_a_short_cooldown_through_record_result(tmp_path: Path) -> None:
    cooldown = tmp_path / "cooldowns.json"
    config = RoutingConfig(crash_cooldown_minutes=10)
    agent = _Agent({"codex": [_fail("crash", "codex")]})

    _run(
        agent, ("codex", None), _Picker([("codex", None)]), [],
        record_func=lambda result, config=None: record_result(result, config=config, cooldown_path=cooldown),
        config=config, pinned=True,
    )

    entry = json.loads(cooldown.read_text(encoding="utf-8"))["codex"]
    until = datetime.fromisoformat(entry["until"])
    assert entry["reason"] == "crash"
    assert 5 < (until - datetime.now(timezone.utc)).total_seconds() / 60 <= 10


def test_every_attempt_is_recorded_with_the_routing_config() -> None:
    recorded: list[tuple[str, Optional[str], object]] = []
    config = RoutingConfig()
    agent = _Agent({"codex": [_fail("empty_output", "codex"), _ok("codex")]})

    _run(
        agent, ("codex", None), _Picker([("codex", None)]), [],
        record_func=lambda result, config=None: recorded.append((result.harness, result.error_kind, config)),
        config=config,
    )

    assert recorded == [("codex", "empty_output", config), ("codex", None, config)]


# --------------------------------------------------------------------------
# Giving up: bounded, explicit, never a blank message
# --------------------------------------------------------------------------


def test_when_every_harness_fails_the_report_carries_kind_exit_code_duration_and_stderr() -> None:
    agent = _Agent(
        {
            "claude": [_fail("crash", "claude", "sonnet", exit_code=2, stderr=f"claude blew up {SECRET}", duration=3.25)],
            "codex": [_fail("timeout", "codex", exit_code=None, stderr="waiting for auth\nstuck", duration=90.0)],
        }
    )
    sleeps: list[float] = []

    report = _run(agent, ("claude", "sonnet"), _Picker([("claude", "sonnet"), ("codex", None)]), sleeps)

    assert report.ok is False and report.result is not None and report.result.error_kind == "timeout"
    assert agent.harnesses == ["claude"] * 3 + ["codex"] * 2  # USR-114: timeout retries at most once
    assert sleeps == [30.0, 120.0, 30.0]

    message = run_ticket.format_agent_failure(report)
    assert message.startswith("[FALHA NA EXECUÇÃO DO AGENTE]") and message.strip() != "[FALHA NA EXECUÇÃO DO AGENTE]:"
    assert "harness: codex" in message and "error_kind: timeout" in message
    assert "exit_code: n/d" in message  # a timeout has no exit code
    assert "duração: 90.0s" in message
    assert "waiting for auth" in message and "stuck" in message
    assert "claude/sonnet error_kind=crash exit_code=2 duração=3.25s" in message  # history keeps earlier routes
    assert SECRET not in message  # stderr carried a token: it must never reach the screen


def test_at_most_three_routes_are_tried() -> None:
    routes = [("claude", "sonnet"), ("codex", None), ("grok", None), ("antigravity", None)]
    agent = _Agent({h: [_fail("unsupported_mode", h, m)] for h, m in routes})

    report = _run(agent, routes[0], _Picker(routes), [])

    assert report.ok is False
    assert agent.harnesses == ["claude", "codex", "grok"]  # the fourth route is never attempted


def test_a_pinned_harness_retries_transient_failures_but_never_falls_through() -> None:
    agent = _Agent({"codex": [_fail("crash", "codex")], "claude": [_ok("claude")]})
    picker = _Picker([("codex", None), ("claude", "sonnet")])
    sleeps: list[float] = []

    report = _run(agent, ("codex", None), picker, sleeps, pinned=True)

    assert report.ok is False
    assert agent.harnesses == ["codex", "codex", "codex"]
    assert sleeps == [30.0, 120.0]
    assert picker.calls == []  # an explicit --harness is honoured: no other route is ever picked


def test_a_pinned_harness_that_cannot_work_fails_at_once() -> None:
    agent = _Agent({"codex": [_fail("not_installed", "codex")]})

    report = _run(agent, ("codex", None), _Picker([("codex", None)]), [], pinned=True)

    assert report.ok is False and len(report.attempts) == 1


def test_without_a_first_route_the_picker_chooses_one_in_write_mode() -> None:
    agent = _Agent({"claude": [_ok("claude", "sonnet")]})
    picker = _Picker([("claude", "sonnet")])

    report = _run(agent, None, picker, [])

    assert report.ok and picker.calls[0]["mode"] == "write" and picker.calls[0]["exclude"] == set()


def test_no_route_at_all_is_a_classified_failure_not_an_exception() -> None:
    report = _run(_Agent({}), None, _Picker([]), [])

    assert report.ok is False and report.attempts == []
    assert report.result is not None and report.result.error_kind == "no_authenticated_harness"
    assert "write" in report.result.text
    assert "[FALHA NA EXECUÇÃO DO AGENTE]" in run_ticket.format_agent_failure(report)


def test_on_event_narrates_each_failure_and_each_wait() -> None:
    events: list[str] = []
    agent = _Agent({"codex": [_fail("timeout", "codex", exit_code=None), _ok("codex")]})

    _run(agent, ("codex", None), _Picker([("codex", None)]), [], on_event=events.append)

    assert any("harness=codex" in e and "error_kind=timeout" in e for e in events)
    assert any("retrying the same route in 30s" in e for e in events)


# --------------------------------------------------------------------------
# Subprocess diagnostics on AgentResult (exit code + redacted stderr tail)
# --------------------------------------------------------------------------


def test_a_failing_cli_reports_exit_code_and_a_redacted_truncated_stderr_tail(
    tmp_path: Path, make_fake_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd_path, env_var = make_fake_cli("codex")
    monkeypatch.setattr(agent_cli, "find_codex_binary", lambda: cmd_path)
    noise = "x" * 3000
    set_fake_response(
        monkeypatch, env_var,
        {"stderr": f"{noise}\nfatal: sandbox unavailable {SECRET}", "returncode": 7},
    )

    result = run_agent(AgentRequest(prompt="work", cwd=tmp_path, mode="write", harness="codex"))

    assert result.ok is False and result.exit_code == 7
    assert result.text == ""  # codex prints nothing on failure: the tail is what makes it diagnosable
    assert result.stderr_tail.endswith("[REDACTED]") and "sandbox unavailable" in result.stderr_tail
    assert SECRET not in result.stderr_tail
    assert len(result.stderr_tail) <= STDERR_TAIL_CHARS


def test_a_successful_cli_reports_exit_code_zero_and_an_empty_tail(
    tmp_path: Path, make_fake_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd_path, env_var = make_fake_cli("claude")
    monkeypatch.setattr(agent_cli, "find_claude_binary", lambda: cmd_path)
    set_fake_response(monkeypatch, env_var, {"stdout": json.dumps({"result": "ok", "is_error": False})})

    result = run_agent(AgentRequest(prompt="work", cwd=tmp_path, mode="write", harness="claude"))

    assert result.ok and result.exit_code == 0 and result.stderr_tail == ""


@pytest.mark.parametrize("harness", ["claude", "codex", "grok", "antigravity"])
def test_a_timeout_keeps_the_partial_stderr_redacted_and_has_no_exit_code(
    harness: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for finder in ("find_claude_binary", "find_codex_binary", "find_grok_binary", "find_antigravity_binary"):
        monkeypatch.setattr(agent_cli, finder, lambda: "fake-cli")

    def _hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="fake-cli", timeout=1, output=b"", stderr=f"waiting for login {SECRET}".encode())

    monkeypatch.setattr(agent_cli.subprocess, "run", _hang)
    mode = "write" if harness in ("claude", "codex") else "read"

    result = run_agent(AgentRequest(prompt="work", cwd=tmp_path, mode=mode, harness=harness, timeout_s=1))  # type: ignore[arg-type]

    assert result.error_kind == "timeout" and result.exit_code is None
    assert "waiting for login" in result.stderr_tail and SECRET not in result.stderr_tail


def test_agent_result_equality_and_construction_are_unchanged_for_existing_callers() -> None:
    plain = AgentResult(ok=True, text="t", harness="claude", duration_s=1.0)

    assert plain.exit_code is None and plain.stderr_tail == ""
    assert plain == AgentResult(ok=True, text="t", harness="claude", duration_s=1.0)
    assert plain != AgentResult(ok=True, text="t", harness="claude", duration_s=1.0, exit_code=0)


# --------------------------------------------------------------------------
# One policy, not two copies
# --------------------------------------------------------------------------

_POLICY_LITERALS = ("rate_limited", "empty_output", "not_installed", "unsupported_mode", "auth_expired")


def test_stage_build_and_run_ticket_import_the_same_policy_module() -> None:
    assert stage_build.agent_retry is agent_retry
    assert run_ticket.agent_retry is agent_retry
    assert not hasattr(stage_build, "_TRANSIENT_AGENT_ERRORS")  # the old private copy is gone


@pytest.mark.parametrize("module", [stage_build, run_ticket], ids=["stage_build", "run_ticket"])
def test_neither_caller_re_implements_the_error_classification(module) -> None:
    source = Path(module.__file__).read_text(encoding="utf-8")

    assert [literal for literal in _POLICY_LITERALS if literal in source] == []


def test_development_stage_picks_its_route_through_the_shared_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _shared_pick_route(pick_func, stage, host_caps, config, excluded, **kwargs):
        seen.update(stage=stage, excluded=set(excluded), pick_func=pick_func)
        return ("sentinel", None)

    monkeypatch.setattr(agent_retry, "pick_route", _shared_pick_route)
    marker: Callable[..., None] = lambda *a, **k: None
    stage = stage_build.DevelopmentStage(pick_func=marker, routing_config=RoutingConfig())

    assert stage._pick_route({("codex", None)}) == ("sentinel", None)
    assert seen == {"stage": "development", "excluded": {("codex", None)}, "pick_func": marker}


def test_stage_retry_classification_is_owned_by_agent_retry() -> None:
    quota = _fail("rate_limited", "claude")
    crash = _fail("crash", "claude")

    assert agent_retry.is_stage_retryable(quota) is True
    assert agent_retry.is_stage_retryable(crash) is False
    assert agent_retry.is_stage_retryable(_ok("claude")) is False
