"""Cancelling a line run stops its agent and blocks any later commit/push (USR-152).

Evidence (07/10/2026): run-6594bec531c2 was cancelled by the owner but its development agent kept
running and committed 18 minutes later. The tests use a fake long-running agent (a Python script that
sleeps and spawns a sleeping child), real local git repositories and the real worker dispatch; no network.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import psutil
import pytest

from core.line import agent_cli, cancellation
from core.line import workspace as ws_mod
from core.line.agent_cli import AgentRequest, _run_bounded, run_agent
from core.line.cancellation import RunCancelledError, run_scope
from core.line.workspace import checkout, commit, push, write_context
from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.orchestrator.cloud_worker import CloudWorker
from core.workflow.control_contracts import IntakeCommand, RuntimeOwner, StageResult
from core.workflow.handlers import HandlerRegistry
from tests.line.test_workspace import _git, _init_bare_origin, _project

# The agent prints its own pid and its child's pid, then sleeps. A real agent CLI behaves the same way
# (node/python wrapper plus tool subprocesses), and the child inherits the pipes.
FAKE_AGENT_SCRIPT = """\
import subprocess, sys, time

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
    stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr,
)
print("AGENT_PID:%d" % __import__("os").getpid(), flush=True)
print("CHILD_PID:%d" % child.pid, flush=True)
time.sleep(120)
"""

QUICK_AGENT_SCRIPT = """\
import sys
print("QUICK_OK", flush=True)
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


def _pids(output: str) -> tuple[int, int]:
    agent = re.search(r"AGENT_PID:(\d+)", output)
    child = re.search(r"CHILD_PID:(\d+)", output)
    assert agent is not None and child is not None, f"pids not found in output: {output!r}"
    return int(agent.group(1)), int(child.group(1))


@pytest.fixture
def fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cancellation, "RUNNER_POLL_INTERVAL_S", 0.2)
    monkeypatch.setattr(cancellation, "CANCEL_POLL_INTERVAL_S", 0.3)


@pytest.fixture
def fake_agent(tmp_path: Path) -> Iterator[list[str]]:
    script = tmp_path / "fake_agent.py"
    script.write_text(FAKE_AGENT_SCRIPT, encoding="utf-8")
    yield [sys.executable, str(script)]


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
        if "time.sleep(120)" in cmdline or "fake_agent.py" in cmdline:
            try:
                proc.kill()
            except psutil.Error:
                pass


# --------------------------------------------------------------------------
# Bounded runner: cancel kills the agent process tree
# --------------------------------------------------------------------------


def test_cancel_kills_agent_and_child_quickly(fake_agent: list[str], fast_polls: None) -> None:
    with run_scope("run-cancel-1") as token:
        timer = threading.Timer(1.5, token.cancel)
        timer.start()
        try:
            start = time.perf_counter()
            res = _run_bounded(fake_agent, timeout_s=300.0, grace_s=5.0)
            elapsed = time.perf_counter() - start
        finally:
            timer.cancel()

    assert res.cancelled is True
    assert res.timed_out is False
    assert elapsed < 20.0, f"cancel took {elapsed:.1f}s"
    agent_pid, child_pid = _pids(res.stdout)
    assert _wait_dead(agent_pid), "agent process survived the cancel"
    assert _wait_dead(child_pid), "agent child process survived the cancel"


def test_run_agent_reports_cancelled_and_never_retries(
    fake_agent: list[str], fast_polls: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    def runner(req: AgentRequest) -> Any:
        res = _run_bounded(fake_agent, cwd=req.cwd, timeout_s=300.0, grace_s=5.0)
        captured["out"] = res.stdout
        return agent_cli.AgentResult(
            ok=False, text=res.stderr or "killed", harness="claude", duration_s=res.duration_s, error_kind="crash"
        )

    monkeypatch.setitem(agent_cli._HARNESS_RUNNERS, "claude", runner)
    monkeypatch.setattr(agent_cli, "check_optional_mcp_servers", lambda *a, **k: (True, []))
    req = AgentRequest(prompt="x", cwd=tmp_path, mode="write", harness="claude")

    with run_scope("run-cancel-2") as token:
        threading.Timer(1.0, token.cancel).start()
        result = run_agent(req)
        # A call made after the cancel does not even start the process.
        again = run_agent(req)

    assert result.ok is False
    assert result.error_kind == "cancelled"
    assert again.error_kind == "cancelled"
    assert again.duration_s == 0.0
    agent_pid, child_pid = _pids(captured["out"])
    assert _wait_dead(agent_pid) and _wait_dead(child_pid)


def test_without_cancel_nothing_changes(tmp_path: Path, fast_polls: None) -> None:
    script = tmp_path / "quick.py"
    script.write_text(QUICK_AGENT_SCRIPT, encoding="utf-8")
    argv = [sys.executable, str(script)]

    outside = _run_bounded(argv, input_text="prompt", timeout_s=30.0)
    with run_scope("run-nocancel") as token:
        inside = _run_bounded(argv, input_text="prompt", timeout_s=30.0)
        assert token.refresh() is False

    for res in (outside, inside):
        assert res.returncode == 0
        assert res.cancelled is False and res.timed_out is False
        assert "QUICK_OK" in res.stdout


def test_timeout_still_applies_inside_a_scope(fake_agent: list[str], fast_polls: None) -> None:
    with run_scope("run-timeout"):
        res = _run_bounded(fake_agent, timeout_s=1.5, grace_s=5.0)
    assert res.timed_out is True and res.cancelled is False
    agent_pid, child_pid = _pids(res.stdout)
    assert _wait_dead(agent_pid) and _wait_dead(child_pid)


def test_store_probe_is_polled_by_refresh() -> None:
    state = {"status": "active"}

    class _Store:
        def get_run_status(self, run_id: str) -> dict[str, Any]:
            return {"run_id": run_id, "status": state["status"]}

    with run_scope("run-probe", cancellation.store_probe(_Store(), "run-probe")) as token:
        assert token.refresh() is False
        cancellation.ensure_not_cancelled("run-probe")
        state["status"] = "cancelled"
        with pytest.raises(RunCancelledError):
            cancellation.ensure_not_cancelled("run-probe")
        assert token.is_cancelled()
    # Outside any scope the guard is a no-op.
    cancellation.ensure_not_cancelled("run-probe")


def test_probe_failure_fails_open() -> None:
    def broken() -> bool:
        raise RuntimeError("database down")

    with run_scope("run-flaky", broken) as token:
        assert token.refresh() is False
        cancellation.ensure_not_cancelled("run-flaky")


# --------------------------------------------------------------------------
# Workspace: no commit / push after the cancel
# --------------------------------------------------------------------------


def _remote_branches(origin: Path) -> str:
    return _git(["branch", "--list"], cwd=origin).stdout


def test_cancelled_run_cannot_commit_or_push(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    origin = _init_bare_origin(tmp_path)
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    ws = checkout(_project(str(origin)), "run-ws-1")

    # Not cancelled: the normal path still commits and pushes.
    with run_scope("run-ws-1", lambda: False):
        write_context(ws, "A.md", "a\n")
        first = commit(ws, "first", job_key="job-a")
        push(ws)
    assert "df/run-ws-1" in _remote_branches(origin)
    remote_tip = _git(["rev-parse", "df/run-ws-1"], cwd=origin).stdout.strip()
    assert remote_tip == first

    # Cancelled while the "agent" was still editing: commit and push are refused.
    write_context(ws, "B.md", "b\n")
    cancelled = {"flag": False}
    with run_scope("run-ws-1", lambda: cancelled["flag"]):
        cancelled["flag"] = True
        with pytest.raises(RunCancelledError):
            commit(ws, "late work", job_key="job-b")
        with pytest.raises(RunCancelledError):
            push(ws)

    head = _git(["rev-parse", "HEAD"], cwd=ws.path).stdout.strip()
    assert head == first, "a commit was created for a cancelled run"
    assert _git(["rev-parse", "df/run-ws-1"], cwd=origin).stdout.strip() == first

    # The refusal marked the worktree: even a later process with no scope at all cannot publish.
    assert ws_mod.is_cancelled(ws)
    with pytest.raises(RunCancelledError):
        commit(ws, "after restart", job_key="job-c")
    with pytest.raises(RunCancelledError):
        push(ws)
    assert _git(["rev-parse", "HEAD"], cwd=ws.path).stdout.strip() == first
    # The marker lives in the private git dir, never in the tree.
    assert "darkfac-run-cancelled" not in _git(["status", "--porcelain"], cwd=ws.path).stdout


def test_mark_run_cancelled_blocks_publishing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    origin = _init_bare_origin(tmp_path)
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    project = _project(str(origin))
    ws = checkout(project, "run-ws-2")
    assert ws_mod.mark_run_cancelled(project.id, "run-ws-2") is True
    assert ws_mod.mark_run_cancelled(project.id, "run-missing") is False

    write_context(ws, "A.md", "a\n")
    with pytest.raises(RunCancelledError):
        commit(ws, "x", job_key="job-x")
    assert "df/run-ws-2" not in _remote_branches(origin)


# --------------------------------------------------------------------------
# Worker: /cancelar while the agent runs
# --------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> PostgresControlStore:
    s = PostgresControlStore(mock_mode=True, runtime_owner=RuntimeOwner.HF05_SQLITE.value, lease_duration_sec=30)
    s._backend.db_path = tmp_path / "control.db"
    return s


def _accept_grill(store: PostgresControlStore) -> str:
    cmd = IntakeCommand(
        project_id="darkfac", channel="test", external_id="usr-152", mode="autonomous",
        policy_ref="darkfac://line/v1",
        payload={"title": "t", "problem": "p", "journey": "j", "non_goals": [], "criteria": []},
    )
    receipt = store.accept(cmd, datetime.now(UTC))
    assert receipt.run_id is not None
    return receipt.run_id


class _AgentThenPublishHandler:
    """Stands in for DevelopmentStage: run a long agent, then try to commit what it produced."""

    def __init__(self, agent_argv: list[str], pid_sink: dict[str, Any]) -> None:
        self.agent_argv = agent_argv
        self.sink = pid_sink

    def handle(self, context: Any) -> StageResult:
        res = _run_bounded(self.agent_argv, timeout_s=300.0, grace_s=5.0)
        self.sink["stdout"] = res.stdout
        self.sink["cancelled"] = res.cancelled
        try:
            cancellation.ensure_not_cancelled(context.claim.job_key.run_id)
        except RunCancelledError:
            self.sink["publish_blocked"] = True
            raise
        self.sink["published"] = True
        return StageResult(outcome="success", output_refs=["sha-never"])


def test_worker_stops_agent_when_run_is_cancelled(
    store: PostgresControlStore, fake_agent: list[str], fast_polls: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink: dict[str, Any] = {}
    registry = HandlerRegistry()
    registry[("grill", "v1")] = _AgentThenPublishHandler(fake_agent, sink)
    worker = CloudWorker(
        worker_id="w1", max_slots=1, store=store, capabilities=["grill_engine", "git", "harness:any"],
        registry=registry,
    )
    notified: list[str] = []
    monkeypatch.setattr(worker, "_notify_stage_outcome", lambda claim, result: notified.append("x"))
    marked: list[tuple[str, str]] = []
    monkeypatch.setattr(ws_mod, "mark_run_cancelled", lambda pid, rid, reason="": marked.append((pid, rid)) or True)

    run_id = _accept_grill(store)

    def cancel_when_agent_started() -> None:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            time.sleep(0.2)
            status = store.get_run_status(run_id)
            job = next((j for j in status["jobs"] if j["stage"] == "grill"), None)
            if job and job["status"] == "running":
                time.sleep(1.5)  # let the fake agent spawn its child
                store.cancel_run(run_id, reason="owner_cancelled", actor="owner")
                return

    canceller = threading.Thread(target=cancel_when_agent_started, daemon=True)
    canceller.start()
    start = time.perf_counter()
    executed = worker.poll_and_execute_once()
    elapsed = time.perf_counter() - start
    canceller.join(timeout=5.0)

    assert executed is True
    assert elapsed < 60.0, f"worker stayed {elapsed:.1f}s on a cancelled run"
    assert sink["cancelled"] is True
    assert sink.get("publish_blocked") is True and "published" not in sink
    agent_pid, child_pid = _pids(sink["stdout"])
    assert _wait_dead(agent_pid) and _wait_dead(child_pid)

    status = store.get_run_status(run_id)
    assert status["status"] == "cancelled"
    for job in status["jobs"]:
        assert job["status"] == "cancelled"
        assert job["cause_code"] == "owner_cancelled"
    assert len(status["jobs"]) == 1, "a successor job was scheduled for a cancelled run"
    assert notified == [], "the owner was notified about a discarded result"
    assert marked and marked[0][1] == run_id, "the run workspace was not marked cancelled"


def test_worker_without_cancel_finishes_normally(store: PostgresControlStore, tmp_path: Path, fast_polls: None) -> None:
    class _Handler:
        def handle(self, context: Any) -> StageResult:
            script = tmp_path / "quick.py"
            script.write_text(QUICK_AGENT_SCRIPT, encoding="utf-8")
            res = _run_bounded([sys.executable, str(script)], timeout_s=30.0)
            assert res.cancelled is False and "QUICK_OK" in res.stdout
            cancellation.ensure_not_cancelled(context.claim.job_key.run_id)
            return StageResult(outcome="failed", cause_code="plain_failure")

    registry = HandlerRegistry()
    registry[("grill", "v1")] = _Handler()
    worker = CloudWorker(
        worker_id="w1", max_slots=1, store=store, capabilities=["grill_engine", "git", "harness:any"],
        registry=registry,
    )
    run_id = _accept_grill(store)
    assert worker.poll_and_execute_once() is True

    status = store.get_run_status(run_id)
    job = next(j for j in status["jobs"] if j["stage"] == "grill")
    assert job["status"] == "failed"
    assert job["cause_code"] == "plain_failure"
    assert status["status"] != "cancelled"


def test_no_scope_leaks_between_tests() -> None:
    assert cancellation.current_token() is None
