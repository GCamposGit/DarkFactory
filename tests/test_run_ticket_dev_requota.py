"""USR-428: the first development agent after PLAN uses a fresh quota pick.

The preflight route is not a license to start an agent. Planning can drop a harness
to the 15% floor; development must pick again on the current reading, without
override and without that old tuple.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import run_ticket
from core.git.autonomy import GitAutonomyManager
from core.git.ticket_workspace import TicketWorkspace
from core.line import routing, ticket_engine, workspace as line_workspace
from core.line.agent_cli import AgentResult
from core.line.stage_planning import PlanningTicket
from run_ticket import EXIT_ROUTE_WAIT, main

_TICKET = run_ticket.UserTicket(
    id="USR-428",
    project_id="darkfac",
    title="Revalidar cota depois do plano",
    problem_statement="a cota do preflight nao inicia o development",
    acceptance_criteria=["pick fresco depois do PLAN"],
)

_PREFLIGHT = {"openai": 20.0, "anthropic": 10.0, "xai": 8.0, "google": 90.0}
_AFTER_CLAUDE = {"openai": 13.0, "anthropic": 40.0, "xai": 8.0, "google": 90.0}
_SLICE_CODEX = {"openai": 40.0, "anthropic": 16.0, "xai": 8.0, "google": 90.0}
_TWO_SLICES = [("USR-428-A", "fatia 1"), ("USR-428-B", "fatia 2")]


def _quotas_from(by_provider: dict[str, Optional[float]]) -> dict[str, dict[str, Any]]:
    providers = {
        "claude": "anthropic", "codex": "openai", "grok": "xai", "antigravity": "google",
    }
    quotas: dict[str, dict[str, Any]] = {}
    for harness, provider in providers.items():
        headroom = by_provider.get(provider)
        critical = headroom is None or headroom <= 15.0
        quotas[harness] = {
            "provider": provider,
            "headroom": headroom,
            "is_critical": critical,
            "status": "SAUDAVEL" if not critical else "CRITICO",
        }
    return quotas


def _write_plan_slices(rws: Any, slices: list[tuple[str, str]]) -> None:
    items = [
        PlanningTicket(id=slice_id, title=title, goal=title, acceptance=[title])
        for slice_id, title in slices
    ]
    line_workspace.write_context(rws, "SPEC.md", "# SPEC\n\n## Objetivo\nplano com fatias\n")
    line_workspace.write_context(
        rws, "tickets.json", json.dumps([item.model_dump() for item in items], indent=2, ensure_ascii=False),
    )


class _Store:
    def __init__(self, _path: Path) -> None:
        pass

    def get_ticket(self, ticket_id: str) -> Any:
        return _TICKET if ticket_id == _TICKET.id else None

    def list_tickets(self) -> list[Any]:
        return [_TICKET]


@pytest.fixture
def launcher(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Hermetic run_ticket: quotas flip when PLAN runs; the agent and delivery are recorded."""
    state = SimpleNamespace(
        table=dict(_PREFLIGHT),
        after=dict(_AFTER_CLAUDE),
        after_slice=None,
        slices=None,
        harnesses=[],
        picks=[],
    )
    workspace_dir = tmp_path / "ticket-workspace"
    workspace_dir.mkdir()
    workspace = TicketWorkspace(
        ticket_id=_TICKET.id, path=workspace_dir, branch="ticket/usr-428", main_root=tmp_path,
        base_ref="origin/main", base_sha="0" * 40,
    )
    state.workspace = workspace

    monkeypatch.setenv("DARKFAC_CODEX_HEADLESS_ALLOWED", "1")
    monkeypatch.setattr(run_ticket, "DemandsStore", _Store)
    monkeypatch.setattr(run_ticket, "inspect_quotas", lambda: _quotas_from(state.table))
    monkeypatch.setattr(routing, "_default_quota_headroom", lambda provider: state.table.get(provider))
    monkeypatch.setattr(routing, "_default_weekly_slack", lambda _provider: None)
    monkeypatch.setattr(routing, "default_cooldown_path", lambda: tmp_path / "cooldowns.json")
    monkeypatch.setattr(run_ticket, "check_optional_mcp_servers", lambda *a, **k: (True, []))
    monkeypatch.setattr(run_ticket, "_prepare_workspace", lambda args, ticket: workspace)
    monkeypatch.setattr(GitAutonomyManager, "changed_paths", lambda self, cwd: ["feature.py"])

    def plan(ticket: Any, _workspace: Any, rws: Any) -> None:
        state.table = dict(state.after)
        if state.slices:
            _write_plan_slices(rws, state.slices)
        else:
            ticket_engine.write_minimal_plan(rws, ticket)
        return None

    monkeypatch.setattr(run_ticket, "_plan_with_agent", plan)

    original_pick = run_ticket.pick

    def spy(stage: str, host_caps: Any, exclude: Any = (), **kwargs: Any) -> Any:
        result = original_pick(stage, host_caps, exclude, **kwargs)
        if stage == "development":
            state.picks.append({"kwargs": kwargs, "result": result})
        return result

    monkeypatch.setattr(run_ticket, "pick", spy)

    def agent(req: Any) -> AgentResult:
        state.harnesses.append(req.harness)
        (workspace_dir / "slice-work.txt").write_text("ok\n", encoding="utf-8")
        if state.after_slice is not None and len(state.harnesses) == 1:
            state.table = dict(state.after_slice)
        return AgentResult(ok=True, text="feito", harness=req.harness, duration_s=0.1, exit_code=0)

    monkeypatch.setattr(run_ticket, "run_agent", agent)
    monkeypatch.setattr(run_ticket, "resume_delivery", lambda **_k: {"exit_code": 0, "ok": True})
    state.delivery = run_ticket.resume_delivery
    return state


def _requota_calls(state: SimpleNamespace) -> list[dict[str, Any]]:
    return [call for call in state.picks if call["kwargs"].get("quota_lookup") is not None]


def test_floor_blocks_exactly_15_and_lets_anything_above_through() -> None:
    assert run_ticket._quota_above_floor(15.0) is False
    assert run_ticket._quota_above_floor(15) is False
    assert run_ticket._quota_above_floor(15.1) is True
    assert run_ticket._quota_above_floor(None) is False


def test_force_on_the_namespace_does_not_keep_a_harness_the_fresh_pick_refuses() -> None:
    args = SimpleNamespace(harness="codex", force=True, prompt="forçar execução neste harness", fleet=False)
    quotas = _quotas_from({"openai": 15.0, "anthropic": 40.0, "xai": 10.0, "google": 90.0})
    refused = run_ticket._development_route_after_plan(args, quotas, ("claude", "sonnet"))
    assert refused == ("claude", "sonnet", False)
    quotas["codex"]["headroom"] = 15.1
    kept = run_ticket._development_route_after_plan(args, quotas, ("claude", "sonnet"))
    assert kept == ("codex", None, True)
    quotas["codex"]["headroom"] = None
    unknown = run_ticket._development_route_after_plan(args, quotas, ("claude", "sonnet"))
    assert unknown == ("claude", "sonnet", False)
    assert run_ticket._development_route_after_plan(args, quotas, None) is None


def test_first_development_uses_the_fresh_pick_not_the_preflight_tuple(
    launcher: SimpleNamespace,
) -> None:
    assert main(["USR-428", "--skip-validation", "--no-commit", "--json"]) == 0

    assert launcher.picks[0]["kwargs"].get("quota_lookup") is None
    assert launcher.picks[0]["result"][0] == "codex"
    requota = _requota_calls(launcher)
    assert len(requota) == 1
    kwargs = requota[0]["kwargs"]
    assert kwargs["mode"] == "write"
    assert "override_granted" not in kwargs
    assert kwargs.get("implementing_harness") is None
    assert ("codex", None) not in kwargs.values()
    lookup = kwargs["quota_lookup"]
    assert lookup("openai") <= 15.0
    assert lookup("xai") <= 15.0
    assert lookup("anthropic") > 15.0
    assert requota[0]["result"][0] == "claude"
    assert launcher.harnesses[0] == "claude"


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        ({"openai": 15.0, "anthropic": 15.1, "xai": 10.0, "google": 90.0}, "claude"),
        ({"openai": 15.1, "anthropic": 10.0, "xai": 10.0, "google": 90.0}, "codex"),
    ],
)
def test_headroom_at_the_floor_is_ineligible_and_above_the_floor_is_not(
    after: dict[str, float], expected: str, launcher: SimpleNamespace,
) -> None:
    launcher.after = after
    assert main(["USR-428", "--skip-validation", "--no-commit", "--json"]) == 0
    assert launcher.harnesses[0] == expected


@pytest.mark.parametrize(
    "extra",
    [
        ["--force"],
        ["--prompt", "forçar execução neste harness"],
    ],
)
def test_force_or_prompt_authorization_does_not_start_the_refused_harness(
    extra: list[str], launcher: SimpleNamespace,
) -> None:
    critical = {"openai": 10.0, "anthropic": 40.0, "xai": 8.0, "google": 90.0}
    launcher.table = dict(critical)
    launcher.after = dict(critical)
    assert main(["USR-428", "--harness", "codex", *extra, "--skip-validation", "--no-commit", "--json"]) == 0
    requota = _requota_calls(launcher)
    assert len(requota) == 1
    assert requota[0]["kwargs"]["mode"] == "write"
    assert "override_granted" not in requota[0]["kwargs"]
    assert requota[0]["result"][0] == "claude"
    assert launcher.harnesses[0] == "claude"


@pytest.mark.parametrize(
    ("headroom", "expected"),
    [
        (15.1, "codex"),
        (15.0, "claude"),
        (None, "claude"),
    ],
)
def test_explicit_harness_follows_only_a_known_headroom_above_the_floor(
    headroom: Optional[float], expected: str, launcher: SimpleNamespace,
) -> None:
    launcher.after = {"openai": headroom, "anthropic": 40.0, "xai": 8.0, "google": 90.0}
    assert main(["USR-428", "--harness", "codex", "--skip-validation", "--no-commit", "--json"]) == 0
    assert _requota_calls(launcher), "revalidation must call pick even when a pin is present"
    assert launcher.harnesses[0] == expected


def test_no_fresh_route_waits_without_agent_delivery_or_deleting_the_worktree(
    launcher: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    launcher.after = {"openai": 10.0, "anthropic": 15.0, "xai": 8.0, "google": 90.0}

    def preserve(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a rota ausente nao descarta a worktree")

    monkeypatch.setattr(run_ticket, "_report_workspace_fate", preserve)
    monkeypatch.setattr(run_ticket, "_discard_untouched_workspace", preserve)
    called = {"delivery": False}

    def resume(**_kwargs: Any) -> dict[str, Any]:
        called["delivery"] = True
        return {"exit_code": 0, "ok": True}

    monkeypatch.setattr(run_ticket, "resume_delivery", resume)

    code = main(["USR-428", "--skip-validation", "--no-commit", "--json"])

    assert code == EXIT_ROUTE_WAIT
    assert launcher.harnesses == []
    assert called["delivery"] is False
    assert launcher.workspace.path.is_dir()
    requota = _requota_calls(launcher)
    assert len(requota) == 1 and requota[0]["result"] is None
    assert "route_wait" in capsys.readouterr().out


def test_each_slice_picks_development_again_before_its_agent(launcher: SimpleNamespace) -> None:
    launcher.slices = list(_TWO_SLICES)
    launcher.after = dict(_SLICE_CODEX)
    launcher.after_slice = {"openai": 15.0, "anthropic": 40.0, "xai": 8.0, "google": 90.0}

    assert main(["USR-428", "--skip-validation", "--no-commit", "--json"]) == 0

    assert launcher.harnesses == ["codex", "claude"]
    requota = _requota_calls(launcher)
    assert len(requota) == 2
    assert requota[0]["result"][0] == "codex"
    assert requota[0]["kwargs"]["quota_lookup"]("openai") > 15.0
    assert requota[1]["kwargs"]["mode"] == "write"
    assert "override_granted" not in requota[1]["kwargs"]
    assert requota[1]["kwargs"]["quota_lookup"]("openai") <= 15.0
    assert requota[1]["kwargs"]["quota_lookup"]("anthropic") > 15.0
    assert requota[1]["result"][0] == "claude"


def test_a_pin_that_stays_above_the_floor_is_kept_on_every_slice(launcher: SimpleNamespace) -> None:
    launcher.slices = list(_TWO_SLICES)
    launcher.after = {"openai": 20.0, "anthropic": 40.0, "xai": 8.0, "google": 90.0}
    launcher.after_slice = {"openai": 30.0, "anthropic": 90.0, "xai": 8.0, "google": 90.0}

    assert main(["USR-428", "--harness", "codex", "--skip-validation", "--no-commit", "--json"]) == 0

    assert launcher.harnesses == ["codex", "codex"]
    requota = _requota_calls(launcher)
    assert len(requota) == 2
    assert all(call["kwargs"]["mode"] == "write" and "override_granted" not in call["kwargs"] for call in requota)


def test_second_slice_without_a_route_does_not_start_the_agent_or_delivery(
    launcher: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    launcher.slices = list(_TWO_SLICES)
    launcher.after = dict(_SLICE_CODEX)
    launcher.after_slice = {"openai": 10.0, "anthropic": 15.0, "xai": 8.0, "google": 90.0}

    def preserve(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("uma fatia sem rota nao descarta a worktree")

    monkeypatch.setattr(run_ticket, "_report_workspace_fate", preserve)
    monkeypatch.setattr(run_ticket, "_discard_untouched_workspace", preserve)
    called = {"delivery": False}

    def resume(**_kwargs: Any) -> dict[str, Any]:
        called["delivery"] = True
        return {"exit_code": 0, "ok": True}

    monkeypatch.setattr(run_ticket, "resume_delivery", resume)

    code = main(["USR-428", "--skip-validation", "--no-commit", "--json"])

    assert code == EXIT_ROUTE_WAIT
    assert launcher.harnesses == ["codex"]
    assert called["delivery"] is False
    assert launcher.workspace.path.is_dir()
    assert (launcher.workspace.path / "slice-work.txt").is_file()
    requota = _requota_calls(launcher)
    assert len(requota) == 2
    assert requota[0]["result"][0] == "codex"
    assert requota[1]["result"] is None
    assert "route_wait" in capsys.readouterr().out
