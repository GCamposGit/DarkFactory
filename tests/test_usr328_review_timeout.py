"""USR-328: review deadline kills the process tree and becomes a structured failure, never an approval."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from core.line import workspace as ws_mod
from core.line.agent_cli import AgentRequest, AgentResult, _run_bounded
from core.line.routing import load_routing_config
from core.line.stage_review import ReviewStage
from tests.line.test_stage_review import _init_bare_origin, _isolated_pick, _prepare_run, _project

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_SLOW_CLI = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], creationflags={flag})
open(sys.argv[1], 'w').write(str(child.pid))
time.sleep(120)
"""


def test_deadline_kills_whole_tree_and_reports_duration(tmp_path: Path) -> None:
    script = tmp_path / "slow.py"
    script.write_text(_SLOW_CLI.format(flag=_NO_WINDOW), encoding="utf-8")
    pidfile = tmp_path / "child.pid"
    start = time.monotonic()
    res = _run_bounded([sys.executable, str(script), str(pidfile)], timeout_s=3, grace_s=3)
    elapsed = time.monotonic() - start
    assert res.timed_out is True
    assert res.returncode != 0
    assert res.duration_s < 3 + 3 + 1 and elapsed < 15
    deadline = time.monotonic() + 5
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    child_pid = int(pidfile.read_text())
    assert _wait_gone(child_pid), "ghost grandchild survived the deadline"


def _wait_gone(pid: int, timeout_s: float = 5.0) -> bool:
    """Dead or zombie (a killed orphan may stay a zombie in containers without an init reaper)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.1)
    return False


def test_review_timeout_is_structured_failure_with_next_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(str(_init_bare_origin(tmp_path)))
    ws = _prepare_run(tmp_path, monkeypatch, project, "run-t", harness="claude")
    calls: list[AgentRequest] = []

    def timing_out(req: AgentRequest) -> AgentResult:
        calls.append(req)
        return AgentResult(
            ok=False, text="timed out after 5s", harness=req.harness, model=req.model,
            duration_s=5.2, error_kind="timeout",
        )

    stage = ReviewStage(
        run_agent_func=timing_out, pick_func=_isolated_pick(tmp_path), routing_config=load_routing_config()
    )
    result = stage.run(project, "run-t")
    assert result.outcome == "retry"
    assert result.cause_code.startswith("review_timeout:")
    assert "duration_s=5.2" in result.cause_code
    assert calls[0].harness != "claude"
    state = json.loads((ws_mod.context_dir(ws) / "review_state.json").read_text(encoding="utf-8"))
    assert calls[0].harness in state["timed_out_harnesses"]
    assert not any(r["verdict"] == "approve" for r in state["rounds"])

    # The next attempt must not re-elect the harness that timed out, and stays another family.
    stage.run(project, "run-t")
    assert len(calls) == 2
    assert calls[1].harness != calls[0].harness
    assert calls[1].harness != "claude"
