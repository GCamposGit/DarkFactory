"""USR-326: a cancelled run ends the agent tree and its subagent executions stop beating."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.line import agent_cli, cancellation, local_cancel
from core.line.local_progress import SqliteSink
from core.workflow import execution_registry as er
from scripts import execution as execution_cli

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TICKET = "USR-326"


def _registry(tmp_path: Path) -> er.ExecutionRegistry:
    sink = SqliteSink(tmp_path / "progress.sqlite3")
    return er.ExecutionRegistry(sink=sink, state_dir=tmp_path / "exec")



@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(local_cancel, "state_root", lambda: tmp_path / "state")
    return tmp_path / "state"


def test_parent_and_child_heartbeat_then_cancel_is_refused(tmp_path: Path, root: Path) -> None:
    reg = _registry(tmp_path)
    parent = reg.start("claude_session", "claude", "development", TICKET)
    child = reg.start("subagent", "claude", "development", TICKET, parent)
    other = reg.start("subagent", "claude", "development", "USR-999")
    assert reg.heartbeat(parent) is True and reg.heartbeat(child) is True

    local_cancel.request_cancel(TICKET)
    assert reg.heartbeat(child) is False  # refused + closed as cancelled
    assert reg.heartbeat(other) is True  # other tickets untouched
    assert not (tmp_path / "exec" / "active" / f"{child}.json").exists()


def test_cancel_ticket_finishes_parent_and_children_only_for_that_ticket(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    a = reg.start("claude_session", "claude", "development", TICKET)
    b = reg.start("subagent", "claude", "development", TICKET, a)
    c = reg.start("subagent", "claude", "development", "USR-999")
    assert sorted(reg.cancel_ticket(TICKET)) == sorted([a, b])
    active = {p.stem for p in (tmp_path / "exec" / "active").glob("*.json")}
    assert active == {c}


def test_watch_exit_after_cancel_finishes_executions(tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = _registry(tmp_path)
    er.set_registry(reg)
    try:
        eid = reg.start("subagent", "claude", "development", TICKET)
        with local_cancel.watch(TICKET, "run-x", install_signals=False, poll_s=0.05) as token:
            local_cancel.request_cancel(TICKET)
            assert token.refresh() is True
        assert not (tmp_path / "exec" / "active" / f"{eid}.json").exists()
    finally:
        er.set_registry(None)


def test_cli_heartbeat_exit_code_stops_subagent(tmp_path: Path, root: Path) -> None:
    reg = _registry(tmp_path)
    eid = reg.start("subagent", "claude", "development", TICKET)
    assert execution_cli.main(["heartbeat", eid], registry=reg) == 0
    local_cancel.request_cancel(TICKET)
    assert execution_cli.main(["heartbeat", eid], registry=reg) == execution_cli.CANCELLED_EXIT


def test_store_failure_does_not_hide_cancel_or_leak(tmp_path: Path, root: Path, caplog: pytest.LogCaptureFixture) -> None:
    class Broken:
        def write(self, *a, **k):
            raise RuntimeError("token=sk-SECRET boom")

        def __getattr__(self, name):
            raise RuntimeError("token=sk-SECRET boom")

    reg = er.ExecutionRegistry(sink=Broken(), state_dir=tmp_path / "exec")  # type: ignore[arg-type]
    eid = reg.start("subagent", "claude", "development", TICKET)
    local_cancel.request_cancel(TICKET)
    assert reg.heartbeat(eid) is False
    assert "sk-SECRET" not in caplog.text


def test_cancel_kills_whole_tree_including_grandchild(tmp_path: Path) -> None:
    pidfile = tmp_path / "pids.txt"
    child = (
        "import subprocess,sys,time,pathlib;"
        f"g=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],creationflags={NO_WINDOW});"
        f"pathlib.Path(r'{pidfile}').write_text(str(g.pid));time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", child], creationflags=NO_WINDOW)
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        time.sleep(0.1)
    grand = int(pidfile.read_text())
    import psutil

    assert psutil.pid_exists(grand)
    agent_cli._kill_process_tree(proc.pid)
    proc.wait(timeout=10)
    for _ in range(50):
        if not psutil.pid_exists(grand):
            break
        time.sleep(0.1)
    assert not psutil.pid_exists(grand)


def test_publish_guard_refuses_after_cancel(tmp_path: Path, root: Path) -> None:
    from core.git import autonomy

    with local_cancel.watch(TICKET, "run-y", install_signals=False, poll_s=0.05):
        local_cancel.request_cancel(TICKET)
        with pytest.raises(cancellation.RunCancelledError):
            autonomy._refuse_if_cancelled(["push", "origin", "x"], autonomy._PUBLISHING_GIT)
