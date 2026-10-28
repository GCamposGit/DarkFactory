"""run_ticket.py wired to the exclusive line lock (USR-250).

Root cause (2026-10-12): two `run_ticket.py` launches of the SAME ticket (USR-239) ran at once, seven
minutes apart, and both ended up fighting over pytest, the Desktop test worker and RAM until both timed
out. `core/line/line_lock.py` carries the actual lock mechanics (see `tests/test_line_lock.py`); these
tests cover the wiring into the launcher itself: a second launch is refused before it creates a worktree
or probes quota, a dead owner's lock is recovered and the run proceeds, the launcher's own delivery
subprocess inherits the lock, and a failed run still frees the lock for the next launch.

Hermetic: `DARKFAC_STATE_ROOT` (and therefore the line lock file) points at a fresh `tmp_path` for every
test, the ledger/quotas/workspace/agent are all doubles, and nothing here touches the real checkout.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import run_ticket
from core.git.autonomy import GitAutonomyManager, SweepReport, TicketCompletionReport
from core.git.ticket_workspace import TicketWorkspace
from core.line import line_lock, routing
from core.line.agent_cli import AgentResult
from core.line.routing import _HARNESS_TO_PROVIDER
from run_ticket import main

_TICKET = run_ticket.UserTicket(
    id="USR-99", project_id="darkfac", title="Ticket de teste", problem_statement="problema de teste"
)


def _quotas() -> dict[str, Any]:
    return {
        harness: {"provider": provider, "headroom": 60.0, "is_critical": False, "status": "SAUDÁVEL"}
        for harness, provider in _HARNESS_TO_PROVIDER.items()
    }


class _Store:
    def __init__(self, _path: Path) -> None:
        pass

    def get_ticket(self, ticket_id: str) -> Any:
        return _TICKET if ticket_id == _TICKET.id else None

    def list_tickets(self) -> list[Any]:
        return [_TICKET]


@pytest.fixture(autouse=True)
def _isolated_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(state_dir))
    monkeypatch.delenv(line_lock.ENV_INHERITED, raising=False)
    return state_dir


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Hermetic launcher: fake backlog/quotas/workspace, a successful agent by default."""
    monkeypatch.setattr(run_ticket, "DemandsStore", _Store)
    monkeypatch.setattr(run_ticket, "inspect_quotas", lambda: _quotas())
    monkeypatch.setattr(routing, "default_cooldown_path", lambda: tmp_path / "cooldowns.json")
    monkeypatch.setattr(run_ticket, "time", SimpleNamespace(sleep=lambda *_a, **_k: None))
    monkeypatch.setattr(run_ticket, "check_optional_mcp_servers", lambda *a, **k: (True, []))
    monkeypatch.setattr(run_ticket, "rebase_on_origin_main", lambda cwd, **_k: (True, [], "already up to date"))
    monkeypatch.setattr(GitAutonomyManager, "sweep_stale", lambda self, *a, **k: SweepReport())
    monkeypatch.setattr(GitAutonomyManager, "changed_paths", lambda self, cwd: ["feature.py"])
    monkeypatch.setattr(
        GitAutonomyManager, "complete_ticket",
        lambda self, **kw: TicketCompletionReport(ok=True, ticket_id=kw["ticket_id"], commit_sha="c0ffee"),
    )
    monkeypatch.setattr(GitAutonomyManager, "commit_ticket", lambda self, *a, **kw: "deadbeef")
    agent = MagicMock(name="run_agent")
    agent.side_effect = lambda req: AgentResult(
        ok=True, text="done", harness=req.harness, model=req.model, duration_s=1.0, exit_code=0
    )
    monkeypatch.setattr(run_ticket, "run_agent", agent)
    workspace_dir = tmp_path / "ticket-workspace"
    workspace_dir.mkdir()
    workspace = TicketWorkspace(
        ticket_id=_TICKET.id, path=workspace_dir, branch="ticket/usr-99", main_root=tmp_path,
        base_ref="origin/main", base_sha="0" * 40,
    )
    prepared = MagicMock(side_effect=AssertionError("must not create a workspace once the lock refuses"))
    monkeypatch.setattr(run_ticket, "_prepare_workspace", lambda *a, **k: workspace)
    return {"agent": agent, "workspace": workspace, "prepare_workspace_spy": prepared}


def _write_lock(info: line_lock.LineLockInfo) -> Path:
    path = line_lock.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info.to_dict()), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Scenario 1: a second launch is refused before any side effect
# --------------------------------------------------------------------------


def test_second_launch_is_refused_without_probing_quota_or_creating_a_workspace(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lock_path = _write_lock(line_lock.LineLockInfo(
        pid=os.getpid(), ticket_id="USR-01", hostname="OTHER-HOST",
        started_at=time.time() - 60.0, heartbeat_at=time.time(),
    ))

    def _must_not_run() -> Any:
        raise AssertionError("inspect_quotas must not run once the line lock refuses the launch")

    monkeypatch.setattr(run_ticket, "inspect_quotas", _must_not_run)
    monkeypatch.setattr(run_ticket, "_prepare_workspace", env["prepare_workspace_spy"])

    code = main(["USR-99", "--harness", "codex", "--skip-validation", "--json"])

    err = capsys.readouterr().err
    assert code == 2
    assert "TRAVA DE LINHA" in err
    assert "USR-01" in err and "OTHER-HOST" in err
    assert "--cancel USR-01" in err
    env["agent"].assert_not_called()
    env["prepare_workspace_spy"].assert_not_called()
    # the lock belongs to the refused attempt's would-be victim, not to USR-99: left untouched
    assert json.loads(lock_path.read_text(encoding="utf-8"))["ticket_id"] == "USR-01"


def test_second_launch_of_the_same_ticket_is_also_refused(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The USR-239 incident: the SAME ticket launched twice is refused exactly like a different one."""
    _write_lock(line_lock.LineLockInfo(
        pid=os.getpid(), ticket_id="USR-99", hostname="NOTEBOOK-GUI",
        started_at=time.time(), heartbeat_at=time.time(),
    ))

    code = main(["USR-99", "--harness", "codex", "--skip-validation", "--json"])

    assert code == 2
    assert "TRAVA DE LINHA" in capsys.readouterr().err
    env["agent"].assert_not_called()


def test_dry_run_and_queue_only_create_are_exempt_from_the_lock(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Read-only/lightweight invocations never contend for pytest/the agent: no lock needed."""
    _write_lock(line_lock.LineLockInfo(
        pid=os.getpid(), ticket_id="USR-01", hostname="OTHER-HOST",
        started_at=time.time(), heartbeat_at=time.time(),
    ))
    capsys.readouterr()

    assert main(["USR-99", "--harness", "codex", "--dry-run"]) == 0
    assert "TRAVA DE LINHA" not in capsys.readouterr().err


# --------------------------------------------------------------------------
# Scenario 2: a dead owner's lock is recovered and the run proceeds
# --------------------------------------------------------------------------


def test_a_dead_owners_lock_is_recovered_and_the_run_succeeds(
    env: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    lock_path = _write_lock(line_lock.LineLockInfo(
        pid=999_999_999,  # far above anything this (or any) host hands out: reliably dead
        ticket_id="USR-01", hostname="NOTEBOOK-GUI",
        started_at=time.time() - 3600.0, heartbeat_at=time.time() - 3600.0,
    ))

    with caplog.at_level(logging.WARNING, logger="core.line.line_lock"):
        code = main(["USR-99", "--harness", "codex", "--skip-validation", "--json"])

    assert code == 0
    assert "USR-01" in caplog.text  # names the recovered (dead) holder
    env["agent"].assert_called_once()
    assert not lock_path.exists()  # released again once this (successful) run finished


# --------------------------------------------------------------------------
# Scenario 3: the launcher's own delivery subprocess inherits the lock
# --------------------------------------------------------------------------


def test_resume_delivery_invoked_as_the_runs_own_child_inherits_the_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Simulates the delivery subprocess the launcher spawns for ITSELF: ENV_INHERITED is set on its
    environment (see the `child_env` wiring in `_run`), so it must never refuse itself even though the
    parent (this test process, genuinely alive) still holds the lock for the same ticket."""
    owner = line_lock.LineLockInfo(
        pid=os.getpid(), ticket_id="USR-99", hostname="NOTEBOOK-GUI",
        started_at=time.time(), heartbeat_at=time.time(),
    )
    lock_path = _write_lock(owner)
    monkeypatch.setenv(line_lock.ENV_INHERITED, "1")

    resume = MagicMock(return_value=0)
    monkeypatch.setattr(run_ticket, "resume_delivery", resume)

    code = main(["--resume-delivery", "USR-99", "--worktree", str(tmp_path), "--json"])

    assert code == 0
    resume.assert_called_once()
    # the child never wrote or deleted the parent's lock
    assert json.loads(lock_path.read_text(encoding="utf-8")) == owner.to_dict()


def test_the_delivery_subprocess_environment_is_marked_as_inherited(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launcher spawns `run_ticket.py --resume-delivery` for its OWN ticket in a fresh process
    (USR-116); that child must inherit the line lock (USR-250) rather than refuse itself. This checks
    the actual subprocess environment the launcher builds for it, forcing the real subprocess path
    (not the in-process one pytest runs under) the same way `test_run_ticket_cancel.py` does."""
    fake_root = tmp_path / "fake_root"
    fake_root.mkdir()
    env_dump = tmp_path / "child_env.json"
    (fake_root / "run_ticket.py").write_text(
        "import json, os\n"
        f"json.dump(dict(os.environ), open({str(env_dump)!r}, 'w'))\n"
        "print(json.dumps({'ok': True, 'exit_code': 0}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(run_ticket, "PROJECT_ROOT", fake_root)
    monkeypatch.delenv("PYTEST_CURRENT_TEST")  # force the real subprocess path (USR-116)

    code = main(["USR-99", "--harness", "codex", "--skip-validation", "--json"])

    assert code == 0
    child_env = json.loads(env_dump.read_text(encoding="utf-8"))
    assert child_env.get(line_lock.ENV_INHERITED) == "1"


# --------------------------------------------------------------------------
# Scenario 4: the lock is released after a failure, not only on a clean exit
# --------------------------------------------------------------------------


def test_the_lock_is_released_after_a_failed_run_and_a_new_launch_can_proceed(
    env: dict[str, Any]
) -> None:
    env["agent"].side_effect = lambda req: AgentResult(
        ok=False, harness=req.harness, model=req.model, duration_s=1.0, exit_code=1,
        error_kind="crash", text="boom", stderr_tail="boom",
    )

    code = main(["USR-99", "--harness", "codex", "--skip-validation", "--json"])

    assert code == 1
    assert not line_lock.lock_path().exists()

    env["agent"].side_effect = lambda req: AgentResult(
        ok=True, text="done", harness=req.harness, model=req.model, duration_s=1.0, exit_code=0
    )
    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--json"]) == 0


# --------------------------------------------------------------------------
# Exempt invocations never take the lock
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["USR-99"], True),
        (["--resume-delivery", "USR-99"], True),
        (["USR-99", "--dry-run"], False),
        (["--create", "--queue-only", "--title", "x"], False),
        ([], False),
    ],
)
def test_needs_line_lock_only_for_invocations_that_contend(argv: list[str], expected: bool) -> None:
    args = run_ticket.build_parser().parse_args(argv)
    assert run_ticket._needs_line_lock(args) is expected


def test_cancel_never_takes_nor_is_refused_by_the_lock(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    _write_lock(line_lock.LineLockInfo(
        pid=os.getpid(), ticket_id="USR-01", hostname="OTHER-HOST",
        started_at=time.time(), heartbeat_at=time.time(),
    ))
    capsys.readouterr()
    main(["--cancel", "USR-01"])
    assert "TRAVA DE LINHA" not in capsys.readouterr().err
    # the holder's lock file is untouched by the cancel request
    assert json.loads(line_lock.lock_path().read_text(encoding="utf-8"))["ticket_id"] == "USR-01"
