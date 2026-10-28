"""Tests for F5 (USR-284): Admissão compartilhada do CLI e testes no host atribuído.

Acceptance criteria:
1. Launcher manual (run_ticket.py) e worker continuo respeitam a mesma reserva de host/ticket,
   preservando trava local e impedindo um quarto ciclo ou lancamento duplicado.
2. No modo frota, DarkFac executa portao oficial local no host reservado usando interfaces existentes;
   projeto externo usa seus proprios comandos, sem alterar core/harness protegido.
3. python -m pytest tests/test_line_fleet_execution.py tests/test_run_ticket_line_lock.py -q passa;
   subagentes permanecem permitidos e duas suites completas nao executam juntas na mesma maquina.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import run_ticket
from core.git.autonomy import GitAutonomyManager, SweepReport, TicketCompletionReport
from core.git.ticket_workspace import TicketWorkspace
from core.line import line_lock, routing
from core.line.agent_cli import AgentResult
from core.line.stage_build import CommandRunResult, DevelopmentProgress, DevelopmentStage, TicketSpec, ValidationStage
from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.orchestrator.cloud_worker import CloudWorker
from core.projects.models import ProjectCommands, ProjectDescriptor, ProjectKind
from core.workflow.control_contracts import (
    Claim,
    FleetCapacityExceededError,
    FleetReservation,
    HostBusyError,
    IntakeCommand,
    JobKey,
    RuntimeOwner,
    StageContext,
    StageResult,
    TicketAlreadyActiveError,
)
from core.workflow.control_store import SQLiteControlStore
from run_ticket import main

_TICKET = run_ticket.UserTicket(
    id="USR-99", project_id="darkfac", title="Ticket de teste frota", problem_statement="problema de teste"
)


def _quotas() -> dict[str, Any]:
    return {
        harness: {"provider": provider, "headroom": 60.0, "is_critical": False, "status": "SAUDÁVEL"}
        for harness, provider in routing._HARNESS_TO_PROVIDER.items()
    }


class _Store:
    def __init__(self, _path: Path) -> None:
        pass

    def get_ticket(self, ticket_id: str) -> Any:
        return _TICKET if ticket_id == _TICKET.id else None

    def list_tickets(self) -> list[Any]:
        return [_TICKET]


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(state_dir))
    monkeypatch.delenv(line_lock.ENV_INHERITED, raising=False)
    monkeypatch.delenv("DARKFAC_FLEET_MODE", raising=False)
    return state_dir


@pytest.fixture
def control_store(tmp_path: Path) -> SQLiteControlStore:
    db_path = tmp_path / "control.db"
    return SQLiteControlStore(db_path=db_path, lease_duration_sec=30)


@pytest.fixture
def launcher_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, control_store: SQLiteControlStore) -> dict[str, Any]:
    monkeypatch.setattr(run_ticket, "DemandsStore", _Store)
    monkeypatch.setattr(run_ticket, "inspect_quotas", lambda: _quotas())
    monkeypatch.setattr(routing, "default_cooldown_path", lambda: tmp_path / "cooldowns.json")
    monkeypatch.setattr(run_ticket, "time", SimpleNamespace(sleep=lambda *_a, **_k: None))
    monkeypatch.setattr(run_ticket, "check_optional_mcp_servers", lambda *a, **k: (True, []))
    monkeypatch.setattr(run_ticket, "rebase_on_origin_main", lambda cwd, **_k: (True, [], "already up to date"))
    monkeypatch.setattr(GitAutonomyManager, "sweep_stale", lambda self, *a, **k: SweepReport())
    monkeypatch.setattr(GitAutonomyManager, "changed_paths", lambda self, cwd: ["feature.py"])
    monkeypatch.setattr(
        GitAutonomyManager,
        "complete_ticket",
        lambda self, **kw: TicketCompletionReport(ok=True, ticket_id=kw["ticket_id"], commit_sha="c0ffee"),
    )
    monkeypatch.setattr(GitAutonomyManager, "commit_ticket", lambda self, *a, **kw: "deadbeef")

    # Patch default_control_store so line_lock uses our test store
    monkeypatch.setattr("core.line.store_selection.default_control_store", lambda *a, **k: control_store)

    agent = MagicMock(name="run_agent")
    agent.side_effect = lambda req: AgentResult(
        ok=True, text="done", harness=req.harness, model=req.model, duration_s=1.0, exit_code=0
    )
    monkeypatch.setattr(run_ticket, "run_agent", agent)

    workspace_dir = tmp_path / "ticket-workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    workspace = TicketWorkspace(
        ticket_id=_TICKET.id,
        path=workspace_dir,
        branch="ticket/usr-99",
        main_root=tmp_path,
        base_ref="origin/main",
        base_sha="0" * 40,
    )
    prepared = MagicMock(side_effect=AssertionError("must not create a workspace when rejected"))
    monkeypatch.setattr(run_ticket, "_prepare_workspace", lambda *a, **k: workspace)
    return {
        "agent": agent,
        "workspace": workspace,
        "prepare_workspace_spy": prepared,
        "store": control_store,
    }


# ==============================================================================
# 1. Manual launcher & fleet cycle reservations
# ==============================================================================


def test_manual_launcher_refused_when_ticket_already_active_in_fleet(
    launcher_env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    store: SQLiteControlStore = launcher_env["store"]
    now = datetime.now(UTC)
    store.reserve_cycle(
        project_id="darkfac",
        ticket_id="USR-99",
        run_id="run-other-01",
        host_id="grokbox",
        now=now,
    )

    code = main(["USR-99", "--fleet", "--host-id", "notebook", "--harness", "codex", "--skip-validation", "--json"])

    err = capsys.readouterr().err
    assert code == 2
    assert "[RESERVA DE FROTA]" in err
    assert "USR-99" in err
    assert "grokbox" in err
    launcher_env["agent"].assert_not_called()


def test_manual_launcher_refused_when_host_busy_in_fleet(
    launcher_env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    store: SQLiteControlStore = launcher_env["store"]
    now = datetime.now(UTC)
    store.reserve_cycle(
        project_id="darkfac",
        ticket_id="USR-01",
        run_id="run-other-02",
        host_id="notebook",
        now=now,
    )

    code = main(["USR-99", "--fleet", "--host-id", "notebook", "--harness", "codex", "--skip-validation", "--json"])

    err = capsys.readouterr().err
    assert code == 2
    assert "[RESERVA DE FROTA]" in err
    assert "notebook" in err
    assert "USR-01" in err
    launcher_env["agent"].assert_not_called()


def test_manual_launcher_refused_when_fleet_capacity_exceeded(
    launcher_env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    store: SQLiteControlStore = launcher_env["store"]
    now = datetime.now(UTC)
    # Fill all 3 fleet slots
    store.reserve_cycle(project_id="darkfac", ticket_id="USR-01", run_id="r1", host_id="grokbox", now=now)
    store.reserve_cycle(project_id="darkfac", ticket_id="USR-02", run_id="r2", host_id="notebook", now=now)
    store.reserve_cycle(project_id="darkfac", ticket_id="USR-03", run_id="r3", host_id="desktop", now=now)

    # Attempt 4th launch on host4
    code = main(["USR-99", "--fleet", "--host-id", "host4", "--harness", "codex", "--skip-validation", "--json"])

    err = capsys.readouterr().err
    assert code == 2
    assert "[RESERVA DE FROTA]" in err
    assert "Fleet capacity" in err
    launcher_env["agent"].assert_not_called()


def test_manual_launcher_acquires_and_releases_fleet_reservation(launcher_env: dict[str, Any]) -> None:
    store: SQLiteControlStore = launcher_env["store"]

    assert len(store.list_active_reservations()) == 0

    code = main(["USR-99", "--fleet", "--host-id", "notebook", "--harness", "codex", "--skip-validation", "--json"])

    assert code == 0
    launcher_env["agent"].assert_called_once()
    # Reservation was released on clean exit
    active = store.list_active_reservations()
    assert len(active) == 0
    # And local lock file is gone
    assert not line_lock.lock_path().exists()


def test_manual_launcher_releases_reservation_on_failure(launcher_env: dict[str, Any]) -> None:
    store: SQLiteControlStore = launcher_env["store"]
    launcher_env["agent"].side_effect = lambda req: AgentResult(
        ok=False, harness=req.harness, model=req.model, duration_s=1.0, exit_code=1, text="crash"
    )

    code = main(["USR-99", "--fleet", "--host-id", "notebook", "--harness", "codex", "--skip-validation", "--json"])

    assert code == 1
    # Reservation released on failure
    assert len(store.list_active_reservations()) == 0
    assert not line_lock.lock_path().exists()


# ==============================================================================
# 2. Continuous worker & local line lock interaction
# ==============================================================================


def test_cloud_worker_skips_poll_when_local_line_lock_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_file = tmp_path / "worker_control.db"
    store = PostgresControlStore(mock_mode=True, runtime_owner=RuntimeOwner.HF05_SQLITE.value, lease_duration_sec=30)
    store._backend.db_path = db_file

    worker = CloudWorker(worker_id="cloud-worker-notebook", host_id="notebook", store=store)

    # Hold local line lock from a different (simulated) process
    other_info = line_lock.LineLockInfo(
        pid=999_999_999,
        ticket_id="USR-MANUAL",
        hostname="NOTEBOOK-GUI",
        started_at=time.time(),
        heartbeat_at=time.time(),
    )
    path = line_lock.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(other_info.to_dict()), encoding="utf-8")
    monkeypatch.setattr(line_lock, "_pid_alive", lambda pid: True)

    # Worker poll must be refused / skipped due to held local lock
    claimed = worker.poll_and_execute_once()
    assert claimed is False


def test_cloud_worker_holds_local_line_lock_during_dispatch(tmp_path: Path) -> None:
    db_file = tmp_path / "worker_control.db"
    store = PostgresControlStore(mock_mode=True, runtime_owner=RuntimeOwner.HF05_SQLITE.value, lease_duration_sec=30)
    store._backend.db_path = db_file

    worker = CloudWorker(worker_id="cloud-worker-notebook", host_id="notebook", store=store)

    cmd = IntakeCommand(
        project_id="darkfac",
        channel="test",
        external_id="ext-dispatch-1",
        mode="autonomous",
        policy_ref="policy-1",
        payload={
            "ticket_id": "USR-44",
            "title": "Title USR-44",
            "problem": "Problem",
            "journey": ["j"],
            "non_goals": [],
            "criteria": ["c"],
        },
    )
    receipt = store.accept(cmd, now=datetime.now(UTC))
    claim = store.claim(worker="cloud-worker-notebook:notebook", capabilities=list(worker.capabilities), now=datetime.now(UTC))
    assert claim is not None

    observed_lock_during_stage = False

    class _MockHandler:
        def handle(self, context: StageContext) -> StageResult:
            nonlocal observed_lock_during_stage
            # Check if line_lock file exists and is held by current process
            observed_lock_during_stage = line_lock.lock_path().is_file()
            return StageResult(outcome="success", output_refs=["ref://stage-output"])

    from core.workflow.handlers import build_handlers

    worker._registry = build_handlers(bindings={claim.job_key.stage: _MockHandler()})
    worker._registry_caps = tuple(worker.capabilities)

    res = worker.dispatch_claimed_job(claim)

    assert res.success is True
    assert observed_lock_during_stage is True
    # Once dispatch finishes, local lock is released
    assert not line_lock.lock_path().exists()


def test_subagents_permitted_under_inherited_line_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Subagents spawned in the run's process tree inherit the lock via ENV_INHERITED."""
    monkeypatch.setenv(line_lock.ENV_INHERITED, "1")

    # Acquiring guard under inheritance is an immediate no-op, never raising collision
    with line_lock.guard("USR-SUBAGENT", fleet_mode=True) as lock:
        assert lock.inherited is True
        assert lock.acquired is False

    # Still no physical file created or touched by child subagent
    assert not line_lock.lock_path().exists()


# ==============================================================================
# 3. Fleet mode validation: DarkFac local execution vs External project commands
# ==============================================================================


def test_fleet_mode_darkfac_validation_runs_local_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """DarkFac in fleet mode executes runner.py --quick with --local and DARKFAC_REMOTE_HARNESS=off."""
    monkeypatch.setenv("DARKFAC_FLEET_MODE", "1")

    captured_cmds: list[list[str]] = []
    captured_envs: list[dict[str, str] | None] = []

    def _fake_run_shell_commands(
        commands: Any, cwd: Path, *, timeout_s: int = 1800, env: dict[str, str] | None = None
    ) -> CommandRunResult:
        captured_cmds.append(list(commands))
        captured_envs.append(env)
        return CommandRunResult(ok=True, combined_output="[HARNESS_PASS]", exit_code=0, duration_s=1.0)

    monkeypatch.setattr("core.line.stage_build.run_shell_commands", _fake_run_shell_commands)
    monkeypatch.setattr("core.line.stage_build._distill_with_test_subagent", lambda r: "passed")
    monkeypatch.setattr("core.line.workspace.commit", lambda *a, **k: "c0ffee")
    monkeypatch.setattr("core.line.workspace.push", lambda *a, **k: None)

    stage = DevelopmentStage(
        run_agent_func=lambda req: AgentResult(ok=True, text="done", harness=req.harness, duration_s=1.0)
    )

    project = ProjectDescriptor(
        id="darkfac",
        name="Dark Factory (Core)",
        kind=ProjectKind.CORE,
        commands=ProjectCommands(validate_cmds=["python core/harness/runner.py --quick"]),
    )
    ws = MagicMock()
    ws.path = tmp_path

    monkeypatch.setattr("core.projects.registry.resolve_commands", lambda p, w: p.commands)
    monkeypatch.setattr("core.line.stage_build.ensure_setup", lambda w, p, c: CommandRunResult(ok=True, combined_output="", exit_code=0, duration_s=0.1))
    monkeypatch.setattr(stage, "_pick_route", lambda excluded: ("codex", "gpt-5"))
    monkeypatch.setattr(stage, "_read_base_red_marker", lambda *a: None)
    monkeypatch.setattr(stage, "_clear_base_red_marker", lambda *a: None)

    ticket = TicketSpec(id="USR-88", title="Teste gate local", acceptance=["crit 1"])
    progress = DevelopmentProgress()

    res = stage._develop_ticket(ws, "run-10", project, ticket, ticket_index=0, progress=progress)

    assert res.outcome == "success"
    assert len(captured_cmds) == 1
    # Verified: --local was added to runner.py --quick
    cmd = captured_cmds[0][0]
    assert "runner.py" in cmd
    assert "--local" in cmd
    # Verified: DARKFAC_REMOTE_HARNESS=off was injected in environment
    assert captured_envs[0] is not None
    assert captured_envs[0].get("DARKFAC_REMOTE_HARNESS") == "off"


def test_fleet_mode_external_project_runs_own_commands_without_remote_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """External project in fleet mode runs its own validate commands as-is without touching core/harness."""
    monkeypatch.setenv("DARKFAC_FLEET_MODE", "1")

    captured_cmds: list[list[str]] = []
    captured_envs: list[dict[str, str] | None] = []

    def _fake_run_shell_commands(
        commands: Any, cwd: Path, *, timeout_s: int = 1800, env: dict[str, str] | None = None
    ) -> CommandRunResult:
        captured_cmds.append(list(commands))
        captured_envs.append(env)
        return CommandRunResult(ok=True, combined_output="All 10 tests passed", exit_code=0, duration_s=1.0)

    monkeypatch.setattr("core.line.stage_build.run_shell_commands", _fake_run_shell_commands)
    monkeypatch.setattr("core.line.stage_build._distill_with_test_subagent", lambda r: "passed")
    monkeypatch.setattr("core.line.workspace.commit", lambda *a, **k: "c0ffee")
    monkeypatch.setattr("core.line.workspace.push", lambda *a, **k: None)

    stage = DevelopmentStage(
        run_agent_func=lambda req: AgentResult(ok=True, text="done", harness=req.harness, duration_s=1.0)
    )

    project = ProjectDescriptor(
        id="client_portal",
        name="Client Portal",
        kind=ProjectKind.CLIENT_PORTFOLIO,
        commands=ProjectCommands(validate_cmds=["npm test", "pytest tests/client"]),
    )
    ws = MagicMock()
    ws.path = tmp_path

    monkeypatch.setattr("core.projects.registry.resolve_commands", lambda p, w: p.commands)
    monkeypatch.setattr("core.line.stage_build.ensure_setup", lambda w, p, c: CommandRunResult(ok=True, combined_output="", exit_code=0, duration_s=0.1))
    monkeypatch.setattr(stage, "_pick_route", lambda excluded: ("codex", "gpt-5"))
    monkeypatch.setattr(stage, "_read_base_red_marker", lambda *a: None)
    monkeypatch.setattr(stage, "_clear_base_red_marker", lambda *a: None)

    ticket = TicketSpec(id="CP-01", title="Teste projeto externo", acceptance=["crit 1"])
    progress = DevelopmentProgress()

    res = stage._develop_ticket(ws, "run-20", project, ticket, ticket_index=0, progress=progress)

    assert res.outcome == "success"
    assert len(captured_cmds) == 1
    # Verified: External project commands run unmodified
    assert captured_cmds[0] == ["npm test", "pytest tests/client"]
    # Verified: DARKFAC_REMOTE_HARNESS is NOT forced to off for external project
    env = captured_envs[0]
    assert env is None or env.get("DARKFAC_REMOTE_HARNESS") != "off"


def test_validation_stage_clean_checkout_respects_fleet_local_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ValidationStage.run in fleet mode applies --local and DARKFAC_REMOTE_HARNESS=off for DarkFac."""
    monkeypatch.setenv("DARKFAC_FLEET_MODE", "1")

    captured_cmds: list[list[str]] = []
    captured_envs: list[dict[str, str] | None] = []

    def _fake_run_shell_commands(
        commands: Any, cwd: Path, *, timeout_s: int = 1800, env: dict[str, str] | None = None
    ) -> CommandRunResult:
        captured_cmds.append(list(commands))
        captured_envs.append(env)
        return CommandRunResult(ok=True, combined_output="[HARNESS_PASS]", exit_code=0, duration_s=1.0)

    monkeypatch.setattr("core.line.stage_build.run_shell_commands", _fake_run_shell_commands)
    monkeypatch.setattr("core.line.stage_build._distill_with_test_subagent", lambda r: "passed")
    monkeypatch.setattr("core.line.workspace.checkout", lambda p, r: MagicMock(path=tmp_path))
    monkeypatch.setattr("core.line.workspace.write_context", lambda *a, **k: None)
    monkeypatch.setattr("core.line.workspace.commit", lambda *a, **k: "c0ffee")
    monkeypatch.setattr("core.line.workspace.push", lambda *a, **k: None)

    val_stage = ValidationStage()
    monkeypatch.setattr(val_stage, "_clean_clone", lambda p, r, d: "c0ffee")

    project = ProjectDescriptor(
        id="darkfac",
        name="Dark Factory (Core)",
        kind=ProjectKind.CORE,
        commands=ProjectCommands(validate_cmds=["python core/harness/runner.py --quick"]),
    )
    monkeypatch.setattr("core.projects.registry.resolve_commands", lambda p, d: p.commands)

    res = val_stage.run(project, "run-clean-01")

    assert res.outcome == "success"
    # Find validate call
    validate_calls = [cmd for cmd in captured_cmds if any("runner.py" in c for c in cmd)]
    assert len(validate_calls) == 1
    assert "--local" in validate_calls[0][0]
    assert captured_envs[0].get("DARKFAC_REMOTE_HARNESS") == "off"
