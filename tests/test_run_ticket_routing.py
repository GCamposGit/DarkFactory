"""run_ticket routing and failure diagnostics (USR-67 / USR-68).

Root cause: `run_ticket.py` asked the router for a harness by name and quota only, so the default path
elected Antigravity (most headroom, no headless write mode) and then failed with a blank message. The
launcher now asks for a `write` route, rejects an explicit harness that cannot write before consuming
anything, and runs the agent through the same retry policy as the production line.

No test here depends on the real `.factory/usage` snapshots, the demands backlog or the network: quotas,
tickets, cooldowns and agents are all injected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

import run_ticket
from core.line import agent_retry, routing
from core.line.agent_cli import AgentRequest, AgentResult
from core.line.routing import _HARNESS_TO_PROVIDER
from run_ticket import main

_TICKET = run_ticket.UserTicket(
    id="USR-99", project_id="darkfac", title="Ticket de teste", problem_statement="problema de teste"
)


def _quotas(**overrides: float) -> dict[str, dict[str, Any]]:
    """`inspect_quotas()` double: every harness healthy unless overridden (harness=headroom)."""
    quotas: dict[str, dict[str, Any]] = {}
    for harness, provider in _HARNESS_TO_PROVIDER.items():
        headroom = overrides.get(harness, 60.0)
        quotas[harness] = {
            "provider": provider, "headroom": headroom, "is_critical": headroom <= 15.0,
            "status": "SAUDÁVEL" if headroom > 15.0 else "CRÍTICO (<= 15%)",
        }
    return quotas


class _Store:
    def __init__(self, _path: Path) -> None:
        pass

    def get_ticket(self, ticket_id: str):
        return _TICKET if ticket_id == _TICKET.id else None

    def list_tickets(self):
        return [_TICKET]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Hermetic launcher environment: fake backlog and quotas, cooldowns in tmp, agent + sleep recorded."""
    monkeypatch.setattr(run_ticket, "DemandsStore", _Store)
    monkeypatch.setattr(run_ticket, "inspect_quotas", lambda: _quotas())
    monkeypatch.setattr(routing, "default_cooldown_path", lambda: tmp_path / "cooldowns.json")
    sleeps: list[float] = []
    monkeypatch.setattr(run_ticket.time, "sleep", sleeps.append)
    agent = MagicMock(name="run_agent")
    monkeypatch.setattr(run_ticket, "run_agent", agent)
    return {"agent": agent, "sleeps": sleeps, "cooldowns": tmp_path / "cooldowns.json"}


def _fail(kind: str, harness: str, **kwargs: Any) -> AgentResult:
    defaults: dict[str, Any] = {"exit_code": 1, "stderr_tail": "", "duration_s": 1.0, "text": ""}
    defaults.update(kwargs)
    return AgentResult(ok=False, harness=harness, error_kind=kind, **defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Routing: the launcher asks for a `write` route
# --------------------------------------------------------------------------


def test_dry_run_asks_the_router_for_write_mode_and_shows_it(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pick = MagicMock(return_value=("claude", "sonnet"))
    monkeypatch.setattr(run_ticket, "pick", pick)

    assert main(["USR-99", "--dry-run"]) == 0

    assert pick.call_args.args[0] == "development" and pick.call_args.kwargs["mode"] == "write"
    out = capsys.readouterr().out
    assert "modo: write" in out and "CLAUDE" in out  # the auto-selection line
    assert "[DRY RUN]" in out and "Rota validada: claude (modo: write)" in out
    env["agent"].assert_not_called()


def test_dry_run_json_payload_carries_the_mode(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run_ticket, "pick", MagicMock(return_value=("codex", None)))

    assert main(["USR-99", "--dry-run", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "write" and payload["selected_harness"] == "codex" and payload["dry_run"] is True


def test_the_real_router_no_longer_elects_antigravity_for_development(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The original defect: Antigravity has the most headroom, but it cannot write."""
    headroom = {"google": 90.0, "xai": 80.0, "anthropic": 40.0, "openai": 30.0}
    monkeypatch.setattr(routing, "_default_quota_headroom", lambda provider: headroom.get(provider))

    assert main(["USR-99", "--dry-run", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert (payload["selected_harness"], payload["selected_model"]) == ("claude", "sonnet")


def test_the_recommendation_for_a_blocked_critical_harness_is_also_write_capable(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run_ticket, "inspect_quotas", lambda: _quotas(codex=2.0))
    pick = MagicMock(return_value=("claude", "sonnet"))
    monkeypatch.setattr(run_ticket, "pick", pick)

    assert main(["USR-99", "--harness", "codex", "--dry-run"]) == 2

    assert pick.call_args.kwargs["mode"] == "write"
    assert "Recomendação do Roteador: CLAUDE" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Explicit --harness that cannot write fails early, consuming nothing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("harness", ["antigravity", "Antigravity", "grok", "openrouter", "mystery"])
def test_an_explicit_harness_without_write_fails_early_with_exit_2_and_consumes_nothing(
    harness: str, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    quotas = MagicMock(side_effect=AssertionError("no quota probe before the capability check"))
    store = MagicMock(side_effect=AssertionError("no backlog access before the capability check"))
    monkeypatch.setattr(run_ticket, "inspect_quotas", quotas)
    monkeypatch.setattr(run_ticket, "DemandsStore", store)

    exit_code = main(["USR-99", "--harness", harness])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert f"'{harness.lower()}'" in err and "não suporta o modo 'write'" in err
    assert "claude, codex" in err  # tells the operator which harnesses can
    env["agent"].assert_not_called()


def test_antigravity_is_rejected_even_with_an_explicit_quota_override(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["USR-99", "--harness", "antigravity", "--force", "--dry-run"]) == 2
    assert "não suporta o modo 'write'" in capsys.readouterr().err  # --force overrides quota, never capability


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_a_write_capable_explicit_harness_is_accepted(
    harness: str, env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["USR-99", "--harness", harness, "--dry-run"]) == 0
    assert f"Rota validada: {harness} (modo: write)" in capsys.readouterr().out


def test_the_capability_check_does_not_block_queue_only_ticket_creation(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = MagicMock(return_value=_TICKET)
    monkeypatch.setattr(run_ticket, "_queue_ticket_isolated", queue)

    assert main(["--create", "--queue-only", "--title", "Defeito", "--harness", "antigravity"]) == 0
    queue.assert_called_once()


# --------------------------------------------------------------------------
# Execution: shared retry policy and a failure message that is never blank
# --------------------------------------------------------------------------


def test_run_ticket_executes_the_agent_through_the_shared_run_with_retry(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failing = agent_retry.RetryReport(
        ok=False,
        result=_fail("timeout", "codex", exit_code=None, stderr_tail="stuck on login", duration_s=90.0),
        route=("codex", None),
        attempts=[agent_retry.AgentAttempt.from_result(1, _fail("timeout", "codex", exit_code=None, duration_s=90.0))],
    )
    shared = MagicMock(return_value=failing)
    monkeypatch.setattr(agent_retry, "run_with_retry", shared)

    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--no-commit"]) == 1

    shared.assert_called_once()
    kwargs = shared.call_args.kwargs
    assert kwargs["mode"] == "write" and kwargs["stage"] == "development" and kwargs["pinned"] is True
    assert shared.call_args.args[1] == ("codex", None)
    env["agent"].assert_not_called()  # the launcher never calls the agent outside the shared loop


def test_a_final_failure_prints_harness_model_kind_exit_code_duration_and_stderr_and_exits_nonzero(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    env["agent"].side_effect = lambda req: _fail(
        "crash", req.harness, model=req.model, exit_code=3, duration_s=2.5,
        stderr_tail="panic: sandbox unavailable\nat exec.rs:42",
    )

    exit_code = main(["USR-99", "--harness", "codex", "--skip-validation", "--no-commit"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "[FALHA NA EXECUÇÃO DO AGENTE]" in err and err.count("[FALHA NA EXECUÇÃO DO AGENTE]:") == 0
    for expected in (
        "harness: codex", "modelo: padrão", "error_kind: crash", "exit_code: 3", "duração: 2.5s",
        "panic: sandbox unavailable", "at exec.rs:42",
    ):
        assert expected in err
    assert env["sleeps"] == [30.0, 120.0]  # the explicit harness was retried with backoff, never swapped
    assert [c.args[0].harness for c in env["agent"].call_args_list] == ["codex"] * 3


def test_a_blank_codex_failure_is_no_longer_blank(env: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    """Codex returns empty text on failure; the report must still say what happened."""
    env["agent"].side_effect = lambda req: _fail("crash", req.harness, exit_code=1, stderr_tail="", text="")

    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--no-commit"]) == 1

    err = capsys.readouterr().err
    assert "error_kind: crash" in err and "exit_code: 1" in err and "(vazio)" in err and "(vazia)" in err


def test_the_failure_is_also_reported_as_json_when_requested(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    env["agent"].side_effect = lambda req: _fail("not_installed", req.harness, exit_code=None, text="missing")

    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--no-commit", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["ticket_id"] == "USR-99"
    assert payload["attempts"][0]["error_kind"] == "not_installed"


def test_an_auto_routed_ticket_falls_back_to_the_next_healthy_harness(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    routes = [("claude", "sonnet"), ("codex", None)]
    picks: list[dict[str, Any]] = []

    def _pick(stage, caps, exclude=(), mode=None, **kwargs):
        picks.append({"exclude": set(exclude), "mode": mode})
        return next((r for r in routes if r not in set(exclude)), None)

    monkeypatch.setattr(run_ticket, "pick", _pick)
    env["agent"].side_effect = lambda req: (
        _fail("not_installed", req.harness, exit_code=None)
        if req.harness == "claude"
        else AgentResult(ok=True, text="done", harness=req.harness, model=req.model, duration_s=4.0, exit_code=0)
    )

    assert main(["USR-99", "--skip-validation", "--no-commit", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["harness"] == "codex"  # the harness that actually did the work
    assert picks[-1] == {"exclude": {("claude", "sonnet")}, "mode": "write"}
    assert env["sleeps"] == []  # `not_installed` is skipped at once, without backoff
    requests: list[AgentRequest] = [c.args[0] for c in env["agent"].call_args_list]
    assert [r.harness for r in requests] == ["claude", "codex"] and all(r.mode == "write" for r in requests)


def test_a_rate_limited_harness_gets_a_cooldown_recorded_by_the_launcher(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_ticket, "pick", MagicMock(return_value=("claude", "sonnet")))
    env["agent"].side_effect = lambda req: _fail("rate_limited", req.harness, model=req.model, text="usage limit")

    assert main(["USR-99", "--harness", "claude", "--skip-validation", "--no-commit"]) == 1

    entry = json.loads(env["cooldowns"].read_text(encoding="utf-8"))["claude"]
    assert entry["reason"] == "rate_limited"  # the next run (or stage) will skip it until the limit lifts
