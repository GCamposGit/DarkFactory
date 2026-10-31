"""USR-428: the first development agent after PLAN uses a fresh quota pick.

The preflight route is not a license to start an agent. Planning can drop a harness
to the 15% floor; development must pick again on the current reading, without
override and without that old tuple.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import run_ticket
from core.git.autonomy import GitAutonomyManager
from core.git.ticket_workspace import TicketWorkspace
from core.line import routing, ticket_engine, workspace as line_workspace
from core.line.agent_cli import AgentResult
from core.line.routing import harness_cli_pin, load_routing_config
from core.line.stage_planning import PlanningTicket
from core.models.registry import QUOTA_FLOOR_MIN
from core.roadmap.models import DeliveryStatus
from run_ticket import EXIT_ROUTE_WAIT, main

_TICKET = run_ticket.UserTicket(
    id="USR-428",
    project_id="darkfac",
    title="Revalidar cota depois do plano",
    problem_statement="a cota do preflight nao inicia o development",
    acceptance_criteria=["pick fresco depois do PLAN"],
)

_PREFLIGHT = {"openai": 20.0, "anthropic": 10.0, "xai": 8.0, "google": 90.0}
# Incident USR-427/428: after PLAN, OpenAI is at 12% and xAI at 7.97%.
_AFTER_CLAUDE = {"openai": 12.0, "anthropic": 40.0, "xai": 7.97, "google": 90.0}
_SLICE_CODEX = {"openai": 40.0, "anthropic": 16.0, "xai": 8.0, "google": 90.0}
_SLICE_CLAUDE = {"openai": 15.0, "anthropic": 40.0, "xai": 8.0, "google": 90.0}
_SLICE_NONE = {"openai": 10.0, "anthropic": 15.0, "xai": 8.0, "google": 90.0}
_THREE_SLICES = [("USR-428-A", "fatia 1"), ("USR-428-B", "fatia 2"), ("USR-428-C", "fatia 3")]
_LEDGER: dict[str, Any] = {"ticket": _TICKET.model_copy()}


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
        ticket = _LEDGER.get("ticket")
        if ticket is None or ticket_id != ticket.id:
            return None
        return ticket

    def list_tickets(self) -> list[Any]:
        ticket = self.get_ticket(_TICKET.id)
        return [] if ticket is None else [ticket]

    def requeue_ticket(self, ticket_id: str, reason: str = "", *, reopen: bool = False) -> tuple[Any, bool]:
        ticket = self.get_ticket(ticket_id)
        if ticket is None:
            raise KeyError(ticket_id)
        if ticket.status in (DeliveryStatus.COMPLETED, DeliveryStatus.CANCELLED) and not reopen:
            raise ValueError(ticket.status.value)
        if ticket.status == DeliveryStatus.PLANNED:
            return ticket, False
        updated = ticket.model_copy(update={"status": DeliveryStatus.PLANNED})
        _LEDGER["ticket"] = updated
        return updated, True


def _set_ticket_status(status: DeliveryStatus) -> None:
    ticket = _LEDGER["ticket"]
    _LEDGER["ticket"] = ticket.model_copy(update={"status": status})


def _git(args: list[str], cwd: Path) -> str:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kwargs,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _git_ticket_workspace(tmp_path: Path) -> TicketWorkspace:
    """Real repo plus worktree. An untouched tree would be removable; a PLAN file is not."""
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(["init"], root)
    _git(["config", "user.email", "seed@example.com"], root)
    _git(["config", "user.name", "Seed"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "README.md"], root)
    _git(["commit", "-m", "seed"], root)
    sha = _git(["rev-parse", "HEAD"], root)
    worktree = tmp_path / "ticket-workspace"
    _git(["worktree", "add", "-b", "ticket/usr-428", str(worktree), "HEAD"], root)
    return TicketWorkspace(
        ticket_id=_TICKET.id, path=worktree, branch="ticket/usr-428",
        main_root=root, base_ref="HEAD", base_sha=sha,
    )


@pytest.fixture
def launcher(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Hermetic run_ticket: quotas flip when PLAN runs; the agent and delivery are recorded."""
    _LEDGER["ticket"] = _TICKET.model_copy()
    state = SimpleNamespace(
        table=dict(_PREFLIGHT),
        after=dict(_AFTER_CLAUDE),
        after_slices=None,
        slices=None,
        harnesses=[],
        models=[],
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
    state.real_changed_paths = GitAutonomyManager.changed_paths
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
            state.picks.append({
                "stage": stage, "exclude": exclude, "kwargs": kwargs, "result": result,
            })
        return result

    monkeypatch.setattr(run_ticket, "pick", spy)

    def agent(req: Any) -> AgentResult:
        state.harnesses.append(req.harness)
        state.models.append(req.model)
        done = len(state.harnesses)
        (workspace_dir / f"slice-{done}.txt").write_text(req.harness + "\n", encoding="utf-8")
        (workspace_dir / "slice-work.txt").write_text("ok\n", encoding="utf-8")
        schedule = state.after_slices
        if schedule and done <= len(schedule):
            state.table = dict(schedule[done - 1])
        return AgentResult(ok=True, text="feito", harness=req.harness, duration_s=0.1, exit_code=0)

    monkeypatch.setattr(run_ticket, "run_agent", agent)
    monkeypatch.setattr(run_ticket, "resume_delivery", lambda **_k: {"exit_code": 0, "ok": True})
    state.delivery = run_ticket.resume_delivery
    return state


def _requota_calls(state: SimpleNamespace) -> list[dict[str, Any]]:
    return [call for call in state.picks if call["kwargs"].get("quota_lookup") is not None]


def test_floor_blocks_exactly_15_and_lets_anything_above_through() -> None:
    assert QUOTA_FLOOR_MIN == 15.0
    assert run_ticket.QUOTA_FLOOR_MIN == QUOTA_FLOOR_MIN
    assert run_ticket._quota_above_floor(QUOTA_FLOOR_MIN) is False
    assert run_ticket._quota_above_floor(15) is False
    assert run_ticket._quota_above_floor(QUOTA_FLOOR_MIN + 0.1) is True
    assert run_ticket._quota_above_floor(None) is False
    assert run_ticket._quota_above_floor(True) is False
    assert run_ticket._quota_above_floor(False) is False


def test_bool_headroom_is_unknown_to_the_fresh_lookup() -> None:
    lookup = run_ticket._quota_lookup_from_reading({
        "codex": {"provider": "openai", "headroom": True},
        "claude": {"provider": "anthropic", "headroom": False},
        "grok": {"headroom": "20"},
    })
    assert lookup("openai") is None
    assert lookup("anthropic") is None
    assert lookup("xai") is None
    args = SimpleNamespace(harness="codex", force=True, prompt="forçar execução neste harness", fleet=False)
    refused = run_ticket._development_route_after_plan(
        args, {"codex": {"provider": "openai", "headroom": True}}, ("claude", "sonnet"),
    )
    assert refused == ("claude", "sonnet", False)


def test_force_on_the_namespace_does_not_keep_a_harness_the_fresh_pick_refuses() -> None:
    args = SimpleNamespace(harness="codex", force=True, prompt="forçar execução neste harness", fleet=False)
    quotas = _quotas_from({"openai": 15.0, "anthropic": 40.0, "xai": 10.0, "google": 90.0})
    refused = run_ticket._development_route_after_plan(args, quotas, ("claude", "sonnet"))
    assert refused == ("claude", "sonnet", False)
    quotas["codex"]["headroom"] = 15.1
    kept = run_ticket._development_route_after_plan(args, quotas, ("claude", "sonnet"))
    codex_model = run_ticket._development_harness_model("codex")
    assert codex_model not in (None, "sonnet")
    assert kept == ("codex", codex_model, True)
    assert kept[1] == harness_cli_pin("codex", None, stage="development", config=load_routing_config())[0]
    quotas["codex"]["headroom"] = None
    unknown = run_ticket._development_route_after_plan(args, quotas, ("claude", "sonnet"))
    assert unknown == ("claude", "sonnet", False)
    assert run_ticket._development_route_after_plan(args, quotas, None) is None
    grok_args = SimpleNamespace(harness="grok", force=True, prompt="", fleet=False)
    grok_quotas = _quotas_from({"openai": 40.0, "anthropic": 50.0, "xai": 20.0, "google": 90.0})
    grok_kept = run_ticket._development_route_after_plan(grok_args, grok_quotas, ("claude", "sonnet"))
    assert grok_kept == ("grok", run_ticket._development_harness_model("grok"), True)
    assert grok_kept[1] not in (None, "sonnet")


def test_first_development_uses_the_fresh_pick_not_the_preflight_tuple(
    launcher: SimpleNamespace,
) -> None:
    assert main(["USR-428", "--skip-validation", "--no-commit", "--json"]) == 0

    assert launcher.picks[0]["kwargs"].get("quota_lookup") is None
    assert launcher.picks[0]["result"][0] == "codex"
    requota = _requota_calls(launcher)
    assert len(requota) == 1
    kwargs = requota[0]["kwargs"]
    assert requota[0]["stage"] == "development"
    assert kwargs["mode"] == "write"
    assert "override_granted" not in kwargs
    assert kwargs.get("implementing_harness") is None
    preflight_route = launcher.picks[0]["result"]
    assert preflight_route not in kwargs.values()
    assert preflight_route not in (requota[0]["exclude"] or ())
    lookup = kwargs["quota_lookup"]
    assert lookup("openai") == 12.0
    assert lookup("xai") == 7.97
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


def test_force_after_a_healthy_preflight_does_not_start_the_post_plan_critical_pin(
    launcher: SimpleNamespace,
) -> None:
    """Preflight codex is above the floor; after PLAN it is not. --force does not start it."""
    launcher.table = {"openai": 40.0, "anthropic": 40.0, "xai": 8.0, "google": 90.0}
    launcher.after = {"openai": 15.0, "anthropic": 40.0, "xai": 8.0, "google": 90.0}
    assert main(
        ["USR-428", "--harness", "codex", "--force", "--skip-validation", "--no-commit", "--json"]
    ) == 0
    requota = _requota_calls(launcher)
    assert len(requota) == 1
    assert requota[0]["kwargs"]["quota_lookup"]("openai") == 15.0
    assert "override_granted" not in requota[0]["kwargs"]
    assert requota[0]["result"][0] == "claude"
    assert launcher.harnesses == ["claude"]


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
    launcher: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Exit 75 keeps a real worktree, requeues to planned, and the real discard refuses to remove it."""
    workspace = _git_ticket_workspace(tmp_path / "kept")
    launcher.workspace = workspace
    monkeypatch.setattr(run_ticket, "_prepare_workspace", lambda _args, _ticket, _ws=workspace: _ws)
    monkeypatch.setattr(GitAutonomyManager, "changed_paths", launcher.real_changed_paths)
    launcher.after = {"openai": 10.0, "anthropic": 15.0, "xai": 8.0, "google": 90.0}
    _set_ticket_status(DeliveryStatus.IMPLEMENTING)
    plan_note = workspace.path / "from-plan.txt"
    original_plan = run_ticket._plan_with_agent

    def plan(ticket: Any, workspace_arg: Any, rws: Any) -> None:
        (workspace_arg.path / "from-plan.txt").write_text("escrito no PLAN\n", encoding="utf-8")
        return original_plan(ticket, workspace_arg, rws)

    monkeypatch.setattr(run_ticket, "_plan_with_agent", plan)
    called = {"agent": False, "delivery": False}

    def agent(_req: Any) -> AgentResult:
        called["agent"] = True
        raise AssertionError("run_agent nao deve ser chamado")

    def resume(**_kwargs: Any) -> dict[str, Any]:
        called["delivery"] = True
        return {"exit_code": 0, "ok": True}

    monkeypatch.setattr(run_ticket, "run_agent", agent)
    monkeypatch.setattr(run_ticket, "resume_delivery", resume)

    code = main(["USR-428", "--skip-validation", "--no-commit", "--json"])

    assert code == EXIT_ROUTE_WAIT
    assert called["agent"] is False
    assert launcher.harnesses == []
    assert called["delivery"] is False
    assert workspace.path.is_dir()
    assert plan_note.is_file()
    assert plan_note.read_text(encoding="utf-8") == "escrito no PLAN\n"
    assert _LEDGER["ticket"].status == DeliveryStatus.PLANNED
    assert run_ticket._discard_untouched_workspace(workspace) is False
    assert workspace.path.is_dir()
    assert plan_note.is_file()
    requota = _requota_calls(launcher)
    assert len(requota) == 1 and requota[0]["result"] is None
    payload = json.loads(capsys.readouterr().out)
    assert payload["cause"] == "route_wait"
    assert payload["stage"] == "BUILD"
    assert payload["workspace"] == str(workspace.path)


def test_minimal_one_slice_plan_rereads_quota_before_the_agent(
    launcher: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fresh pick closes over the reading from the moment the minimal plan is written."""
    monkeypatch.setattr(run_ticket, "estimate_ticket_size", lambda _ticket: ("small", []))
    original_write = ticket_engine.write_minimal_plan

    def write_minimal(rws: Any, ticket: Any) -> None:
        original_write(rws, ticket)
        launcher.table = dict(_AFTER_CLAUDE)

    monkeypatch.setattr(ticket_engine, "write_minimal_plan", write_minimal)

    assert main(["USR-428", "--skip-validation", "--no-commit", "--json"]) == 0

    assert launcher.picks[0]["result"][0] == "codex"
    requota = _requota_calls(launcher)
    assert len(requota) == 1
    assert requota[0]["stage"] == "development"
    assert requota[0]["kwargs"]["mode"] == "write"
    lookup = requota[0]["kwargs"]["quota_lookup"]
    assert lookup("openai") == _AFTER_CLAUDE["openai"]
    assert lookup("xai") == _AFTER_CLAUDE["xai"]
    assert lookup("anthropic") == _AFTER_CLAUDE["anthropic"]
    launcher.table = dict(_PREFLIGHT)
    assert lookup("openai") == _AFTER_CLAUDE["openai"]
    assert requota[0]["result"][0] == "claude"
    assert launcher.harnesses == ["claude"]


def test_fleet_slice_pick_is_headless_and_does_not_start_antigravity(
    launcher: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--fleet re-picks in headless context, so the operating Antigravity harness does not start."""
    monkeypatch.setenv("DARKFAC_OPERATING_HARNESS", "antigravity")
    launcher.after = {"openai": 12.0, "anthropic": 15.0, "xai": 7.97, "google": 90.0}

    code = main(["USR-428", "--fleet", "--skip-validation", "--no-commit", "--json"])

    assert code == EXIT_ROUTE_WAIT
    assert launcher.harnesses == []
    requota = _requota_calls(launcher)
    assert len(requota) == 1
    assert requota[0]["stage"] == "development"
    assert requota[0]["kwargs"]["execution_context"] == "headless"
    assert requota[0]["kwargs"]["mode"] == "write"
    assert "override_granted" not in requota[0]["kwargs"]
    assert requota[0]["result"] is None


def test_three_slices_pick_before_each_agent_and_the_third_without_a_route_waits(
    launcher: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Three slices, three development picks. The third with no writable headroom waits."""
    launcher.slices = list(_THREE_SLICES)
    launcher.after = dict(_SLICE_CODEX)
    launcher.after_slices = [dict(_SLICE_CLAUDE), dict(_SLICE_NONE)]
    _set_ticket_status(DeliveryStatus.IMPLEMENTING)
    called = {"delivery": False}

    def resume(**_kwargs: Any) -> dict[str, Any]:
        called["delivery"] = True
        return {"exit_code": 0, "ok": True}

    monkeypatch.setattr(run_ticket, "resume_delivery", resume)

    code = main(["USR-428", "--skip-validation", "--no-commit", "--json"])

    assert code == EXIT_ROUTE_WAIT
    assert launcher.harnesses == ["codex", "claude"]
    assert called["delivery"] is False
    assert _LEDGER["ticket"].status == DeliveryStatus.PLANNED
    assert launcher.workspace.path.is_dir()
    assert (launcher.workspace.path / "slice-1.txt").read_text(encoding="utf-8") == "codex\n"
    assert (launcher.workspace.path / "slice-2.txt").read_text(encoding="utf-8") == "claude\n"
    assert not (launcher.workspace.path / "slice-3.txt").exists()
    requota = _requota_calls(launcher)
    assert len(requota) == 3
    assert all(
        call["stage"] == "development"
        and call["kwargs"]["mode"] == "write"
        and call["kwargs"].get("quota_lookup") is not None
        and "override_granted" not in call["kwargs"]
        for call in requota
    )
    assert requota[0]["result"][0] == "codex"
    assert requota[0]["kwargs"]["quota_lookup"]("openai") > 15.0
    assert requota[1]["kwargs"]["quota_lookup"]("openai") <= 15.0
    assert requota[1]["kwargs"]["quota_lookup"]("anthropic") > 15.0
    assert requota[1]["result"][0] == "claude"
    assert requota[2]["kwargs"]["quota_lookup"]("openai") <= 15.0
    assert requota[2]["kwargs"]["quota_lookup"]("anthropic") <= 15.0
    assert requota[2]["kwargs"]["quota_lookup"]("xai") <= 15.0
    assert requota[2]["result"] is None
    payload = json.loads(capsys.readouterr().out)
    assert payload["cause"] == "route_wait"
    assert payload["stage"] == "BUILD"
    assert payload["workspace"] == str(launcher.workspace.path)


def test_a_pin_that_stays_above_the_floor_is_kept_on_every_slice(
    launcher: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DARKFAC_OPERATING_HARNESS", "claude")
    launcher.slices = list(_THREE_SLICES)
    launcher.after = {"openai": 20.0, "anthropic": 40.0, "xai": 8.0, "google": 90.0}
    launcher.after_slices = [
        {"openai": 30.0, "anthropic": 90.0, "xai": 8.0, "google": 90.0},
        {"openai": 16.0, "anthropic": 80.0, "xai": 8.0, "google": 90.0},
    ]

    assert main(["USR-428", "--harness", "codex", "--skip-validation", "--no-commit", "--json"]) == 0

    codex_model = harness_cli_pin("codex", None, stage="development", config=load_routing_config())[0]
    assert codex_model not in (None, "sonnet")
    assert launcher.harnesses == ["codex", "codex", "codex"]
    assert launcher.models == [codex_model, codex_model, codex_model]
    requota = _requota_calls(launcher)
    assert all(call["result"][0] == "claude" for call in requota)
    assert len(requota) == 3
    assert all(
        call["stage"] == "development"
        and call["kwargs"]["mode"] == "write"
        and call["kwargs"].get("quota_lookup") is not None
        and call["kwargs"]["quota_lookup"]("openai") > 15.0
        and "override_granted" not in call["kwargs"]
        for call in requota
    )
