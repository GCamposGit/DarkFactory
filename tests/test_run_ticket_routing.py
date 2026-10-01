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
import subprocess
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

import run_ticket
from core.line import agent_retry, routing
from core.git.autonomy import GitAutonomyManager, TicketCompletionReport
from core.demands.store import DemandsStore
from core.roadmap.models import DeliveryStatus
from core.git.ticket_workspace import TicketWorkspace
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
    # USR-69: the agent runs in the ticket's own worktree. Creating a real one would touch this
    # repository, so the launcher's workspace seam hands out a plain directory instead.
    workspace_dir = tmp_path / "ticket-workspace"
    workspace_dir.mkdir()
    workspace = TicketWorkspace(
        ticket_id=_TICKET.id, path=workspace_dir, branch="ticket/usr-99", main_root=tmp_path,
        base_ref="origin/main", base_sha="0" * 40,
    )
    prepared: list[str] = []
    monkeypatch.setattr(run_ticket, "_prepare_workspace", lambda args, ticket: prepared.append(ticket.id) or workspace)
    # Existing routing tests use a plain directory, not a git repository. They exercise
    # successful routing with a synthetic implementation path; the real-git cases below
    # cover the no-changes boundary.
    monkeypatch.setattr(GitAutonomyManager, "changed_paths", lambda self, cwd: ["feature.py"])
    return {
        "agent": agent, "sleeps": sleeps, "cooldowns": tmp_path / "cooldowns.json",
        "workspace": workspace, "prepared": prepared,
    }


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
    assert all(f"harness {r.harness}" in r.prompt.splitlines()[0] for r in requests)


def test_a_rate_limited_harness_gets_a_cooldown_recorded_by_the_launcher(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_ticket, "pick", MagicMock(return_value=("claude", "sonnet")))
    env["agent"].side_effect = lambda req: _fail("rate_limited", req.harness, model=req.model, text="usage limit")

    assert main(["USR-99", "--harness", "claude", "--skip-validation", "--no-commit"]) == 1

    entry = json.loads(env["cooldowns"].read_text(encoding="utf-8"))["claude"]
    assert entry["reason"] == "rate_limited"  # the next run (or stage) will skip it until the limit lifts


# --------------------------------------------------------------------------
# USR-69: the agent works in the ticket's own worktree, never in the shared checkout
# --------------------------------------------------------------------------


def test_the_agent_runs_in_the_ticket_worktree_not_the_shared_checkout(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    env["agent"].side_effect = lambda req: AgentResult(
        ok=True, text="done", harness=req.harness, model=req.model, duration_s=1.0, exit_code=0
    )

    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--no-commit", "--json"]) == 0

    request: AgentRequest = env["agent"].call_args.args[0]
    assert request.cwd == env["workspace"].path and request.cwd != run_ticket.PROJECT_ROOT
    assert "ticket/usr-99" in request.prompt  # the agent is told where it works
    assert env["prepared"] == ["USR-99"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["workspace"] == str(env["workspace"].path) and payload["branch"] == "ticket/usr-99"


def test_a_dirty_shared_checkout_stops_the_run_before_any_agent_is_consumed(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(args: Any, ticket: Any) -> None:
        raise run_ticket.SharedCheckoutDirty(["core/other_ticket.py", "tests/test_other_ticket.py"])

    monkeypatch.setattr(run_ticket, "_prepare_workspace", refuse)

    assert main(["USR-99", "--harness", "codex", "--skip-validation"]) == 3

    err = capsys.readouterr().err
    assert "core/other_ticket.py" in err and "checkout compartilhado" in err and "--allow-dirty-shared-checkout" in err
    env["agent"].assert_not_called()


def test_a_blocked_delivery_is_an_error_not_a_success(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A red CI (or any blocked delivery) leaves the PR open: the launcher must not print [SUCESSO]."""
    env["agent"].side_effect = lambda req: AgentResult(
        ok=True, text="done", harness=req.harness, model=req.model, duration_s=1.0, exit_code=0
    )
    seen: dict[str, Any] = {}

    def blocked(self: GitAutonomyManager, **kwargs: Any) -> TicketCompletionReport:
        seen.update(kwargs)
        return TicketCompletionReport(ok=False, ticket_id=kwargs["ticket_id"], message="CI failed on build")

    monkeypatch.setattr(GitAutonomyManager, "complete_ticket", blocked)

    assert main(["USR-99", "--harness", "codex", "--skip-validation"]) == 1

    captured = capsys.readouterr()
    assert "[SUCESSO]" not in captured.out
    assert "CI failed on build" in captured.err and str(env["workspace"].path) in captured.err
    assert seen["cwd"] == env["workspace"].path  # completion also happens inside the worktree


def _git(repo: Path, *args: str) -> str:
    """Run local git against a disposable repository."""
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def real_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TicketWorkspace:
    """Use a local bare origin and ticket worktree; never touch the checkout ledger."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "--quiet", "-b", "main")
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    _git(main_repo, "init", "--quiet", "-b", "main")
    _git(main_repo, "config", "user.name", "DarkFac Test")
    _git(main_repo, "config", "user.email", "test@darkfac.internal")
    _git(main_repo, "config", "commit.gpgsign", "false")
    store = DemandsStore(main_repo / ".factory" / "demands" / "demands.json")
    store.save_ticket(_TICKET.model_copy(deep=True))
    _git(main_repo, "add", "-A")
    _git(main_repo, "commit", "--quiet", "-m", "seed")
    _git(main_repo, "remote", "add", "origin", str(bare))
    _git(main_repo, "push", "--quiet", "-u", "origin", "main")
    path = tmp_path / "ticket"
    _git(main_repo, "worktree", "add", "--quiet", "-b", "ticket/usr-99", str(path), "main")
    workspace = TicketWorkspace(
        ticket_id=_TICKET.id, path=path, branch="ticket/usr-99", main_root=main_repo,
        base_ref="origin/main", base_sha=_git(main_repo, "rev-parse", "HEAD"),
    )
    monkeypatch.setattr(run_ticket, "PROJECT_ROOT", main_repo)
    monkeypatch.setattr(run_ticket, "DemandsStore", DemandsStore)
    monkeypatch.setattr(run_ticket, "inspect_quotas", lambda: _quotas(codex=64.0))
    monkeypatch.setattr(run_ticket, "_prepare_workspace", lambda args, ticket: workspace)
    return workspace


@pytest.mark.parametrize("ledger_only", [False, True])
def test_ok_agent_without_implementation_is_rejected_before_delivery(
    real_workspace: TicketWorkspace, ledger_only: bool, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An ok agent cannot complete a ticket with no code, even if it edits the ledger."""
    def fake_agent(req: AgentRequest) -> AgentResult:
        if ledger_only:
            store = DemandsStore(req.cwd / ".factory" / "demands" / "demands.json")
            ticket = store.get_ticket(_TICKET.id)
            assert ticket is not None
            ticket.problem_statement = "ledger-only edit"
            store.save_ticket(ticket)
        return AgentResult(
            ok=True, text="sk-ant-api03-SECRETSECRETSECRET nao implementei", harness=req.harness,
            duration_s=1.0,
        )

    monkeypatch.setattr(run_ticket, "run_agent", fake_agent)
    delivery = MagicMock(side_effect=AssertionError("delivery must not run"))
    monkeypatch.setattr(GitAutonomyManager, "complete_ticket", delivery)
    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--json"]) == 4
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False and payload["cause"] == "agent_no_changes"
    assert "sem alterar nenhum arquivo de implementacao" in captured.err
    assert "[REDACTED]" in captured.err and "SECRETSECRETSECRET" not in captured.err
    assert not real_workspace.path.exists()
    assert DemandsStore(real_workspace.main_root / ".factory" / "demands" / "demands.json").get_ticket(_TICKET.id).status == DeliveryStatus.PLANNED
    delivery.assert_not_called()


def test_ok_agent_with_implementation_reaches_normal_completion(
    real_workspace: TicketWorkspace, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A real implementation is committed and the ticket can be completed."""
    def fake_agent(req: AgentRequest) -> AgentResult:
        assert req.prompt.startswith("CONTEXTO DE EXECUCAO HEADLESS")
        assert "codex (headroom 64.0%)" in req.prompt
        (req.cwd / "feature.py").write_text("value = 1\n", encoding="utf-8")
        return AgentResult(
            ok=True, text="Implemented feature.py sk-ant-api03-SECRETSECRETSECRET",
            harness=req.harness, duration_s=1.0,
        )

    monkeypatch.setattr(run_ticket, "run_agent", fake_agent)
    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--no-push", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["agent_summary"] == "Implemented feature.py [REDACTED]"
    assert (real_workspace.path / "feature.py").is_file()
    assert DemandsStore(real_workspace.path / ".factory" / "demands" / "demands.json").get_ticket(_TICKET.id).status == DeliveryStatus.COMPLETED
