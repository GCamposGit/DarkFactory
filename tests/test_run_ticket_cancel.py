"""USR-166: a running ``run_ticket.py`` can be cancelled per ticket, killing the agent's process tree.

USR-152 made cancelling a *line* run kill the agent and block commit/push. The headless launcher has no
store and no per-run worker, so it gets the same mechanism (``core.line.cancellation`` scope, token and
guards) fed by a per-ticket control file or a termination signal (``core.line.local_cancel``).

Hermetic: the state root is a temp directory, the agent is a fake long-running script (it spawns a
sleeping child), the progress sink is in memory or a scratch SQLite file; no network, no real ``.factory``.
"""

from __future__ import annotations

import json
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import MagicMock

import psutil
import pytest

import run_ticket
from core.git import autonomy
from core.git.autonomy import GitAutonomyManager, TicketCompletionReport
from core.line import agent_cli, cancellation, local_cancel
from core.line.agent_cli import AgentRequest, AgentResult, _run_bounded
from core.line.cancellation import RunCancelledError, run_scope
from core.line.local_progress import ProgressPublisher, SqliteSink
from core.workflow.line_live import read_line_live
from run_ticket import EXIT_CANCELLED, main
from tests.fixtures.line_live_seed import seed_line_live_demo
from tests.line.conftest import write_python_shim
from tests.test_local_progress import RecordingSink, _Launcher, launcher  # noqa: F401  (fixture)

# Prints "pids=<agent>,<child>" into the file given as argv[1], then sleeps (a real agent CLI also has
# tool subprocesses that inherit its pipes).
FAKE_AGENT = """\
import os, subprocess, sys, time

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    fh.write("%d,%d" % (os.getpid(), child.pid))
time.sleep(120)
"""


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False


def _wait_dead(pid: int, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return not _alive(pid)


def _wait_for(path: Path, timeout_s: float = 20.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text(encoding="utf-8").count(",") == 1:
            return path.read_text(encoding="utf-8")
        time.sleep(0.05)
    raise AssertionError(f"{path} was never written")


@pytest.fixture(autouse=True)
def _state_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Control files, cost history and every other state file land in the temp directory."""
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(root))
    return root


@pytest.fixture
def fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cancellation, "RUNNER_POLL_INTERVAL_S", 0.2)


@pytest.fixture(autouse=True)
def _reap_leftovers() -> Iterator[None]:
    """Whatever a failing test leaves behind (the fake agent sleeps 120 s) is killed at teardown."""
    before = {p.pid for p in psutil.Process().children(recursive=True)}
    yield
    for proc in psutil.Process().children(recursive=True):
        if proc.pid in before:
            continue
        try:
            cmdline = " ".join(proc.cmdline())
        except psutil.Error:
            continue
        if "time.sleep(120)" in cmdline or "fake_long_agent" in cmdline:
            try:
                proc.kill()
            except psutil.Error:
                pass


# ------------------------------------------------------------------ the control file and signals


def test_request_cancel_writes_a_sanitised_per_ticket_file(_state_root: Path) -> None:
    path = local_cancel.request_cancel("USR-166")
    assert path == _state_root / "local_cancel" / "USR-166.cancel"
    assert json.loads(path.read_text(encoding="utf-8"))["ticket_id"] == "USR-166"
    assert local_cancel.cancel_requested("USR-166")
    assert not local_cancel.cancel_requested("USR-167")

    hostile = local_cancel.control_file("..\\..\\evil/../x")
    assert hostile.parent == _state_root / "local_cancel"

    assert local_cancel.clear_request("USR-166") is True
    assert local_cancel.clear_request("USR-166") is False
    assert not local_cancel.cancel_requested("USR-166")


def test_watch_sets_the_token_when_the_file_appears_and_consumes_it(fast_polls: None) -> None:
    with local_cancel.watch("USR-166", "local-USR-166-x", poll_s=0.05) as token:
        assert not token.is_cancelled()
        assert cancellation.current_token() is token
        cancellation.ensure_not_cancelled()  # no request: no-op
        local_cancel.request_cancel("USR-166")
        deadline = time.monotonic() + 5
        while not token.is_cancelled() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert token.is_cancelled()
        with pytest.raises(RunCancelledError):
            cancellation.ensure_not_cancelled()
    assert cancellation.current_token() is None
    assert not local_cancel.cancel_requested("USR-166")  # the owner consumed the request


def test_a_stale_request_does_not_cancel_a_new_run_but_a_joined_run_keeps_it() -> None:
    local_cancel.request_cancel("USR-166")
    with local_cancel.watch("USR-166", "run-a", not_before=time.time() + 5, poll_s=0.05) as token:
        assert not token.refresh()  # written before this run started: the previous cancellation was cleared

    local_cancel.request_cancel("USR-166")
    with local_cancel.watch("USR-166", "run-c", not_before=time.time() - 60, poll_s=0.05) as token:
        assert token.refresh()  # written after the launcher started (during its preflight): honoured

    local_cancel.request_cancel("USR-166")
    with local_cancel.watch("USR-166", "run-b", fresh=False, poll_s=0.05) as token:
        assert token.refresh()  # a delivery subprocess joining the run honours the parent's request
    assert local_cancel.cancel_requested("USR-166")  # ... and does not erase it


def test_a_termination_signal_cancels_and_the_handler_is_restored() -> None:
    previous = signal.getsignal(signal.SIGTERM)
    with local_cancel.watch("USR-166", "run-sig", poll_s=0.05) as token:
        signal.raise_signal(signal.SIGTERM)
        assert token.is_cancelled()
        assert token.reason == local_cancel.SIGNAL_REASON
    assert signal.getsignal(signal.SIGTERM) == previous


def test_the_cli_option_writes_the_control_file(
    capsys: pytest.CaptureFixture[str], _state_root: Path
) -> None:
    assert main(["--cancel", "USR-166"]) == 0
    assert (_state_root / "local_cancel" / "USR-166.cancel").is_file()
    assert "USR-166" in capsys.readouterr().out

    assert main(["--cancel"]) == 1
    assert "TICKET_ID" in capsys.readouterr().err


# ------------------------------------------------------------------ the guards on git and gh


def test_commit_and_push_are_refused_once_the_run_is_cancelled(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.name", "T"], ["config", "user.email", "t@x.test"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "a.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True, capture_output=True)

    with run_scope("run-guard", lambda: False) as token:
        assert autonomy._run_git(["commit", "-m", "before cancel"], cwd=repo).returncode == 0
        head = autonomy._run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
        (repo / "b.txt").write_text("b", encoding="utf-8")
        subprocess.run(["git", "add", "b.txt"], cwd=repo, check=True, capture_output=True)

        token.cancel()
        with pytest.raises(RunCancelledError):
            autonomy._run_git(["commit", "-m", "after cancel"], cwd=repo)
        with pytest.raises(RunCancelledError):
            autonomy._run_git(["-c", "core.editor=true", "push", "origin", "main"], cwd=repo)
        with pytest.raises(RunCancelledError):
            autonomy._run_gh(["pr", "merge", "1", "--squash"], cwd=repo)
        # reading stays possible: diagnosis is not publishing
        assert autonomy._run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip() == head

    # outside any scope nothing changes
    assert autonomy._run_git(["commit", "-m", "outside scope"], cwd=repo).returncode == 0


# ------------------------------------------------------------------ the live board


def test_a_cancelled_run_shows_on_the_board_as_cancelada(tmp_path: Path) -> None:
    now = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)
    db = tmp_path / "control.db"
    seed_line_live_demo(db, now)
    start = now - timedelta(minutes=30)
    tick = [start]
    publisher = ProgressPublisher(
        ticket_id="USR-166", project_id="darkfac", title="Cancelar run local", run_id="local-USR-166-x",
        sink=SqliteSink(db), worker="DESKTOP-TEST", clock=lambda: tick[0],
    )
    for minutes, phase, status in (
        (0, "preflight", "running"), (1, "preflight", "succeeded"), (1, "workspace", "succeeded"),
        (2, "agent", "running"),
    ):
        tick[0] = start + timedelta(minutes=minutes)
        publisher.phase(phase, status, "x", harness="claude")  # type: ignore[arg-type]
    tick[0] = start + timedelta(minutes=9)
    publisher.cancel("cancelada pelo owner")

    snapshot = read_line_live(db, database_url="", now=now)
    run = next(item for item in snapshot.runs if item.run_id == "local-USR-166-x")
    assert run.state == "cancelled" and run.run_status == "cancelled"
    agent = next(stage for stage in run.stages if stage.stage == "agent")
    assert agent.status == "cancelled" and agent.cause_code == "owner_cancelled"
    assert agent.evidence_refs == ["cancelada pelo owner"]
    assert [event.status for event in publisher.events][-2:] == ["cancelled", "cancelled"]


def test_cancel_before_any_phase_is_active_still_closes_the_run_as_cancelled(tmp_path: Path) -> None:
    sink = RecordingSink()
    publisher = ProgressPublisher(ticket_id="USR-166", sink=sink, worker="H")
    publisher.phase("agent", "succeeded", "ok")
    publisher.cancel()
    publisher.cancel()  # idempotent
    publisher.finish(False)  # a later close never overrides the cancellation
    assert sink.steps == [("agent", "succeeded"), ("run", "cancelled")]

    joined = ProgressPublisher(ticket_id="USR-166", sink=RecordingSink(), worker="H", owns_run=False)
    joined.phase("gate", "running", "x")
    joined.cancel()
    assert [(e.phase, e.status) for e in joined.sink.events] == [("gate", "running"), ("gate", "cancelled")]  # type: ignore[union-attr]


# ------------------------------------------------------------------ the launcher end to end (fake agent)


def _wire_long_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pidfile: Path
) -> None:
    """Real ``run_agent`` -> real ``_run_bounded`` -> a shim around a fake agent that sleeps 120 s."""
    script = tmp_path / "fake_long_agent.py"
    script.write_text(FAKE_AGENT, encoding="utf-8")
    shim = write_python_shim(tmp_path / "fake_long_agent", script)

    def runner(req: AgentRequest) -> AgentResult:
        res = _run_bounded([str(shim), str(pidfile)], cwd=req.cwd, timeout_s=300.0, grace_s=5.0)
        return AgentResult(
            ok=False, text=res.stderr or "agent killed", harness=req.harness, model=req.model,
            duration_s=res.duration_s, error_kind="crash",
        )

    monkeypatch.setitem(agent_cli._HARNESS_RUNNERS, "codex", runner)
    monkeypatch.setattr(agent_cli, "check_optional_mcp_servers", lambda *a, **k: (True, []))
    monkeypatch.setattr(run_ticket, "run_agent", agent_cli.run_agent)


def _guard_delivery(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    complete = MagicMock(name="complete_ticket")
    complete.side_effect = lambda **kw: TicketCompletionReport(ok=True, ticket_id=kw["ticket_id"], commit_sha="c0ffee")
    commit = MagicMock(name="commit_ticket", return_value="deadbeef")
    monkeypatch.setattr(GitAutonomyManager, "complete_ticket", lambda self, **kw: complete(**kw))
    monkeypatch.setattr(GitAutonomyManager, "commit_ticket", lambda self, *a, **kw: commit(*a, **kw))
    return complete, commit


def test_cancelling_a_running_launcher_kills_the_agent_tree_and_delivers_nothing(
    launcher: Callable[[Any], _Launcher],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fast_polls: None,
) -> None:
    sink = RecordingSink()
    launcher(sink)
    pidfile = tmp_path / "pids.txt"
    _wire_long_agent(monkeypatch, tmp_path, pidfile)
    complete, commit = _guard_delivery(monkeypatch)

    def cancel_when_the_agent_runs() -> None:
        _wait_for(pidfile)
        local_cancel.request_cancel("USR-99")

    thread = threading.Thread(target=cancel_when_the_agent_runs, daemon=True)
    thread.start()
    started = time.perf_counter()
    code = main(["USR-99", "--harness", "codex", "--skip-validation", "--json"])
    elapsed = time.perf_counter() - started
    thread.join(timeout=5)

    assert code == EXIT_CANCELLED
    assert elapsed < 30, f"cancellation took {elapsed:.1f}s"
    agent_pid, child_pid = (int(part) for part in pidfile.read_text(encoding="utf-8").split(","))
    assert _wait_dead(agent_pid), "the agent survived the cancellation"
    assert _wait_dead(child_pid), "the agent's child survived the cancellation"

    complete.assert_not_called()
    commit.assert_not_called()
    assert sink.steps[-2:] == [("agent", "cancelled"), ("run", "cancelled")]
    assert {event.phase for event in sink.events}.isdisjoint({"gate", "commit", "pr", "ci", "merge", "deploy"})
    assert sink.events[-1].cause_code == "owner_cancelled"

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["cause"] == "cancelled" and payload["ticket_id"] == "USR-99"
    assert not local_cancel.cancel_requested("USR-99")  # the request was consumed


def test_cancelling_during_the_delivery_phase_never_commits_or_pushes(
    launcher: Callable[[Any], _Launcher],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sink = RecordingSink()
    env = launcher(sink)
    complete, commit = _guard_delivery(monkeypatch)

    def rebase_then_cancel(cwd: Path) -> tuple[bool, list[str], str]:
        local_cancel.request_cancel("USR-99")  # the owner cancels while the delivery is syncing
        return True, [], "already up to date"

    monkeypatch.setattr(run_ticket, "rebase_on_origin_main", rebase_then_cancel)

    code = main(["USR-99", "--harness", "codex", "--skip-validation", "--json"])

    assert code == EXIT_CANCELLED
    env.agent.assert_called()  # the agent had finished; only the delivery was stopped
    complete.assert_not_called()
    commit.assert_not_called()
    assert sink.steps[-2:] == [("gate", "cancelled"), ("run", "cancelled")]
    assert json.loads(capsys.readouterr().out)["cause"] == "cancelled"


def test_a_cancel_that_arrives_before_the_agent_starts_never_spawns_it(
    launcher: Callable[[Any], _Launcher],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sink = RecordingSink()
    env = launcher(sink)
    complete, _ = _guard_delivery(monkeypatch)
    real_prepare = run_ticket._prepare_workspace

    def prepare_then_cancel(args: Any, ticket: Any) -> Any:
        workspace = real_prepare(args, ticket)
        local_cancel.request_cancel("USR-99")
        return workspace

    monkeypatch.setattr(run_ticket, "_prepare_workspace", prepare_then_cancel)

    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--json"]) == EXIT_CANCELLED

    env.agent.assert_not_called()
    complete.assert_not_called()
    assert sink.steps[-2:] == [("workspace", "succeeded"), ("run", "cancelled")]
    assert ("agent", "running") not in sink.steps
    capsys.readouterr()


def test_a_run_without_a_cancel_request_is_unchanged(
    launcher: Callable[[Any], _Launcher],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sink = RecordingSink()
    launcher(sink)
    complete, _ = _guard_delivery(monkeypatch)

    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--json"]) == 0

    complete.assert_called_once()
    assert sink.steps[-1] == ("run", "succeeded")
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cancellation.current_token() is None


def test_cancelling_the_launcher_also_kills_the_delivery_subprocess_tree(
    launcher: Callable[[Any], _Launcher],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fast_polls: None,
) -> None:
    """A signal reaches only the parent launcher: the fresh delivery process must die with it."""
    sink = RecordingSink()
    launcher(sink)
    complete, _ = _guard_delivery(monkeypatch)
    pidfile = tmp_path / "delivery_pids.txt"
    fake_root = tmp_path / "fake_root"
    fake_root.mkdir()
    # Stands in for `run_ticket.py --resume-delivery`: records the cancel env var, spawns a child, sleeps.
    (fake_root / "run_ticket.py").write_text(
        FAKE_AGENT.replace(
            'fh.write("%d,%d" % (os.getpid(), child.pid))',
            'fh.write("%d,%d" % (os.getpid(), child.pid)); '
            'open(sys.argv[1] + ".env", "w").write(os.environ.get("DARKFAC_LOCAL_CANCEL_PARENT", ""))',
        ).replace("sys.argv[1]", "os.environ['FAKE_PIDFILE']"),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_PIDFILE", str(pidfile))
    monkeypatch.setattr(run_ticket, "PROJECT_ROOT", fake_root)
    monkeypatch.delenv("PYTEST_CURRENT_TEST")  # the launcher only spawns the delivery outside pytest

    # the scope is a context variable of the main thread: capture it from inside the run
    holder: dict[str, cancellation.CancelToken] = {}
    real_watch = local_cancel.watch

    def spying_watch(*args: Any, **kwargs: Any) -> Any:
        manager = real_watch(*args, **kwargs)

        class _Spy:
            def __enter__(self) -> cancellation.CancelToken:
                token = manager.__enter__()
                holder["token"] = token
                return token

            def __exit__(self, *exc: Any) -> Any:
                return manager.__exit__(*exc)

        return _Spy()

    monkeypatch.setattr(local_cancel, "watch", spying_watch)

    def cancel_by_signal() -> None:
        _wait_for(pidfile)
        holder["token"].cancel(local_cancel.SIGNAL_REASON)

    thread = threading.Thread(target=cancel_by_signal, daemon=True)
    thread.start()
    code = main(["USR-99", "--harness", "codex", "--skip-validation", "--json"])
    thread.join(timeout=5)

    assert code == EXIT_CANCELLED
    delivery_pid, child_pid = (int(part) for part in pidfile.read_text(encoding="utf-8").split(","))
    assert _wait_dead(delivery_pid) and _wait_dead(child_pid)
    assert Path(str(pidfile) + ".env").read_text(encoding="utf-8").startswith("local-USR-99")  # joined, not owner
    complete.assert_not_called()
    assert sink.steps[-1] == ("run", "cancelled")
    assert json.loads(capsys.readouterr().out)["cause"] == "cancelled"
