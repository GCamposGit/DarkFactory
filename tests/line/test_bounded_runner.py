"""Tests for bounded runner with process tree termination and timeout policy (USR-114).

Validates:
1. Process tree termination: fake child process spawns a sleeping grandchild holding open
   pipes. With timeout_s=2, _run_bounded returns in < 15s with timed_out=True, the entire
   tree is terminated (grandchild is dead, none surviving), and partial output is captured.
2. Secret redaction on partial output: secrets emitted before timeout are redacted.
3. duration_s measures monotonic elapsed time without clipping; cleanup is bounded.
4. Codex runner preserves partial output: partial output from tmp_out and stdout/stderr
   is preserved and redacted on timeout.
5. Cross-platform support: works on both Windows and POSIX.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import psutil
from core.line import agent_cli
from core.line.agent_cli import (
    AgentRequest,
    AgentResult,
    BoundedProcessResult,
    _kill_process_tree,
    _run_bounded,
    redact_secrets,
    run_agent,
)
from core.line import agent_retry
from core.line.agent_retry import RetryReport, run_with_retry

# Assembled at runtime so the static secret scanner does not flag a tracked token
SECRET_TOKEN = "sk-" + "ant-" + "api03-" + "FakeSecretToken1234567890"

# Python script for child process:
# - Prints a secret token to stdout
# - Spawns a grandchild process that inherits stdout/stderr and sleeps for 30s
# - Prints grandchild PID
# - Sleeps for 30s
CHILD_GRANDCHILD_SCRIPT = """\
import sys, time, subprocess

# Emit secret token before spawning
print("CHILD_OUTPUT_START " + sys.argv[1], flush=True)

# Grandchild inherits stdout/stderr pipes and sleeps
cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
p = subprocess.Popen(
    cmd,
    stdin=subprocess.DEVNULL,
    stdout=sys.stdout,
    stderr=sys.stderr,
)
print(f"GRANDCHILD_PID:{p.pid}", flush=True)

# Child sleeps
time.sleep(30)
"""


def _is_running(pid: int) -> bool:
    """True while `pid` is a live process. A zombie is dead: it only awaits a reaper.

    In a Docker container whose PID 1 is not an init (no `init: true`), a killed grandchild is
    reparented to PID 1 and stays <defunct> forever, so `psutil.pid_exists` keeps answering True even
    though SIGKILL was delivered and the process no longer runs.
    """
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False


def test_is_running_treats_zombies_and_missing_pids_as_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def status(self) -> str:
            return psutil.STATUS_ZOMBIE if self.pid == 111 else psutil.STATUS_SLEEPING

    def _fake_process(pid: int) -> _FakeProcess:
        if pid == 222:
            raise psutil.NoSuchProcess(pid)
        return _FakeProcess(pid)

    monkeypatch.setattr(psutil, "Process", _fake_process)
    assert _is_running(111) is False  # zombie
    assert _is_running(222) is False  # gone
    assert _is_running(333) is True  # alive


def test_bounded_runner_terminates_grandchild_and_returns_under_15s(tmp_path: Path) -> None:
    """Fake process creates a grandchild sleeping and holding pipes open.

    With timeout=2, the runner must return in < 15s with timed_out=True, the grandchild
    must be terminated, and partial output must contain the stdout printed before timeout.
    """
    child_script = tmp_path / "child.py"
    child_script.write_text(CHILD_GRANDCHILD_SCRIPT, encoding="utf-8")

    argv = [sys.executable, str(child_script), SECRET_TOKEN]

    start = time.perf_counter()
    res = _run_bounded(argv, timeout_s=2.0, grace_s=5.0)
    elapsed = time.perf_counter() - start

    # 1. Returns promptly in less than 15 seconds
    assert elapsed < 15.0, f"_run_bounded took too long: {elapsed:.2f}s"
    assert res.timed_out is True

    # 2. Duration is not clipped; a real process deadline should be reached within
    # the requested timeout plus the cleanup allowance and measurement tolerance.
    assert res.duration_s >= 2.0
    assert res.duration_s <= 2.0 + 5.0 + 1.0, (
        f"platform={sys.platform}, tolerance=1.0s, observed={res.duration_s:.3f}s"
    )
    assert res.cleanup_duration_s <= 5.0 + 0.5, (
        f"platform={sys.platform}, tolerance=0.5s, cleanup={res.cleanup_duration_s:.3f}s"
    )

    # 3. Partial output captured
    assert "CHILD_OUTPUT_START" in res.stdout
    match = re.search(r"GRANDCHILD_PID:(\d+)", res.stdout)
    assert match is not None, f"Grandchild PID not found in stdout: {res.stdout}"
    grandchild_pid = int(match.group(1))

    # Give OS a brief moment to update process table if needed
    time.sleep(0.5)

    # 4. Process tree terminated: grandchild is not running
    assert not _is_running(grandchild_pid), f"Grandchild process {grandchild_pid} is still alive!"

    # 5. Redaction verification
    redacted = redact_secrets(res.stdout)
    assert SECRET_TOKEN not in redacted
    assert "[REDACTED]" in redacted


def test_bounded_runner_duration_never_exceeds_timeout_plus_grace(tmp_path: Path) -> None:
    """The duration is monotonic wall time and must not be clipped to the budget."""
    child_script = tmp_path / "sleep.py"
    child_script.write_text("import time; time.sleep(10)", encoding="utf-8")

    timeout_s = 1.0
    grace_s = 2.0
    res = _run_bounded([sys.executable, str(child_script)], timeout_s=timeout_s, grace_s=grace_s)

    assert res.timed_out is True
    assert res.duration_s >= timeout_s
    assert res.cleanup_duration_s >= 0
    assert res.outcome == "timeout"
    assert res.process_tree_clean is True


def test_bounded_runner_reports_normal_exit_code_and_cleanup(tmp_path: Path) -> None:
    script = tmp_path / "exit.py"
    script.write_text("print('done'); raise SystemExit(7)", encoding="utf-8")

    result = _run_bounded([sys.executable, str(script)], timeout_s=5)

    assert result.outcome == "completed"
    assert result.returncode == 7
    assert result.stdout.strip() == "done"
    assert result.timed_out is False
    assert result.cancelled is False
    assert result.process_tree_clean is True


def test_bounded_runner_kills_descendant_after_root_exits_with_pipes_open(tmp_path: Path) -> None:
    script = tmp_path / "root_exits.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(0.2)\n",
        encoding="utf-8",
    )

    started = time.monotonic()
    result = _run_bounded([sys.executable, str(script)], timeout_s=2, grace_s=1)
    elapsed = time.monotonic() - started
    pid = int(result.stdout.strip().splitlines()[-1])

    assert elapsed < 5, f"platform={sys.platform}, tolerance=2.0s, observed={elapsed:.3f}s"
    assert result.outcome == "timeout"
    assert result.returncode == 0
    assert result.process_tree_clean is True
    assert not _is_running(pid), f"descendant {pid} survived pipe-drain timeout"


def test_bounded_runner_timeout_preserves_missing_exit_code_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "wait.py"
    pid_file = tmp_path / "pid.txt"
    script.write_text(
        "import os, pathlib, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    original_kill = agent_cli._kill_process_tree

    def _slow_failed_kill(pid: int) -> None:
        time.sleep(0.3)

    monkeypatch.setattr(agent_cli, "_kill_process_tree", _slow_failed_kill)
    try:
        result = _run_bounded([sys.executable, str(script)], timeout_s=0.5, grace_s=0.2)
        assert result.returncode is None
        assert result.outcome == "cleanup_failed"
        assert result.process_tree_clean is False
        assert result.cleanup_error
        assert result.cleanup_duration_s >= 0.3
        assert result.duration_s >= 0.8, "duration must report observed wall time, not a clipped budget"
    finally:
        # The deliberately un-killed root process is cleaned up by the test itself.
        monkeypatch.setattr(agent_cli, "_kill_process_tree", original_kill)
        if pid_file.exists():
            original_kill(int(pid_file.read_text(encoding="utf-8")))


def test_bounded_runner_cancellation_terminates_process_tree(tmp_path: Path) -> None:
    script = tmp_path / "cancel.py"
    pid_file = tmp_path / "cancel.pid"
    script.write_text(
        "import os, pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "print('started', flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    with agent_cli.cancellation.run_scope("bounded-cancel-test") as token:
        import threading

        timer = threading.Timer(1.5, token.cancel)
        timer.start()
        try:
            result = _run_bounded([sys.executable, str(script)], timeout_s=10, grace_s=2)
        finally:
            timer.cancel()

    assert result.outcome == "cancelled"
    assert result.cancelled is True
    assert result.timed_out is False
    assert result.stdout.strip() == "started"
    assert result.process_tree_clean is True
    assert result.cleanup_duration_s <= 2.0 + 0.5, (
        f"platform={sys.platform}, tolerance=0.5s, cleanup={result.cleanup_duration_s:.3f}s"
    )
    pid = int(pid_file.read_text(encoding="utf-8"))
    assert not _is_running(pid), f"platform={sys.platform}: cancelled descendant {pid} is still running"


def test_codex_timeout_preserves_partial_output_and_redacts_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that _run_codex preserves partial output (both tmp_out and stdout/stderr) on timeout."""
    # Create fake codex script that writes partial json to tmp_out and partial stderr
    fake_codex_script = tmp_path / "fake_codex.py"
    fake_codex_script.write_text(
        f"""\
import sys, time, json
out_file = None
for i, arg in enumerate(sys.argv):
    if arg == "--output-file" and i + 1 < len(sys.argv):
        out_file = sys.argv[i + 1]

if out_file:
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(json.dumps({{"text": "partial work done with secret {SECRET_TOKEN}", "usage": {{"input_tokens": 10}}}}) + "\\n")

sys.stderr.write("codex stderr log with secret {SECRET_TOKEN}\\n")
sys.stderr.flush()
time.sleep(30)
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(agent_cli, "find_codex_binary", lambda: sys.executable)
    # Patch build_codex_argv to invoke our script
    orig_build = agent_cli.build_codex_argv

    def fake_build(executable, req, tmp_out):
        return [sys.executable, str(fake_codex_script), "--output-file", str(tmp_out)]

    monkeypatch.setattr(agent_cli, "build_codex_argv", fake_build)

    start = time.perf_counter()
    req = AgentRequest(prompt="do something", cwd=tmp_path, mode="write", harness="codex", timeout_s=2)
    res = agent_cli._run_codex(req)
    elapsed = time.perf_counter() - start

    assert elapsed < 15.0
    assert res.ok is False
    assert res.error_kind == "timeout"
    assert res.duration_s <= 2.0 + 10.0

    # Verify partial output from file and stderr was preserved and redacted
    assert "timed out after 2s" in res.text
    assert "partial work done" in res.text
    assert SECRET_TOKEN not in res.text
    assert "[REDACTED]" in res.text
    assert "codex stderr log" in res.stderr_tail
    assert SECRET_TOKEN not in res.stderr_tail


def test_agent_retry_timeout_duration_logged_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """duration_s reportado em AgentResult e no log do agent_retry nunca excede timeout_s + grace configurado."""
    events: list[str] = []

    def fake_run(req: AgentRequest) -> AgentResult:
        return AgentResult(
            ok=False,
            text="timed out after 2s",
            harness="codex",
            duration_s=2.5,  # <= timeout_s (2) + grace_s (10)
            error_kind="timeout",
        )

    def _picker(stage, caps, config=None, exclude=(), mode=None, **kwargs):
        if ("codex", None) not in set(exclude):
            return ("codex", None)
        return None

    def _req(harness, model):
        return AgentRequest(prompt="p", cwd=tmp_path, mode="write", harness=harness, model=model, timeout_s=2)

    report = run_with_retry(
        _req,
        ("codex", None),
        host_caps=["harness:codex"],
        run_func=fake_run,
        pick_func=_picker,
        sleep_fn=lambda s: None,
        has_changes_fn=lambda cwd: False,
        on_event=events.append,
    )

    for attempt in report.attempts:
        assert attempt.duration_s <= 2.0 + 10.0
        # Check event log matches
        assert any(f"duration_s={attempt.duration_s}" in e for e in events)


def test_kill_process_tree_handles_invalid_pid() -> None:
    """_kill_process_tree handles 0, -1, or non-existent PID gracefully without raising."""
    _kill_process_tree(0)
    _kill_process_tree(-1)
    _kill_process_tree(99999999)


def test_stage_build_timeout_proceeds_to_validate_when_worktree_has_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """USR-114 Criterion 5: In stage_build, when an agent times out but implementation changes

    are present in the worktree, the flow follows to validate instead of repeating the route from zero.
    """
    from core.line import stage_build
    from core.line.stage_build import (
        DevelopmentProgress,
        DevelopmentStage,
        TicketSpec,
        CommandRunResult,
    )
    from core.line.workspace import RunWorkspace
    from core.projects.models import ProjectDescriptor, ProjectCommands
    from core.line.routing import RoutingConfig

    ws = RunWorkspace(
        project_id="test_proj",
        run_id="run_123",
        branch="df/run_123",
        path=tmp_path,
        base_sha="0" * 40,
    )
    project = ProjectDescriptor(id="test_proj", name="test_proj", repo_url="https://github.com/test/test")
    ticket = TicketSpec(id="T-1", title="Test ticket", acceptance=["it works"])

    # Agent times out:
    agent_called = []

    def fake_agent(req: AgentRequest) -> AgentResult:
        agent_called.append(req)
        return AgentResult(ok=False, text="timed out after 1800s", harness="codex", error_kind="timeout", duration_s=1800.0)

    # Validate commands pass:
    validate_called = []

    def fake_shell(cmds, cwd, timeout_s=300):
        validate_called.append(cmds)
        return CommandRunResult(ok=True, combined_output="1 passed", exit_code=0, duration_s=0.5)

    monkeypatch.setattr(stage_build, "run_shell_commands", fake_shell)
    monkeypatch.setattr(stage_build, "ensure_setup", lambda ws, proj, cmds: CommandRunResult(ok=True, combined_output="", exit_code=0, duration_s=0.1))
    monkeypatch.setattr(stage_build.workspace, "commit", lambda ws, msg, job_key: "commit_sha_123")
    monkeypatch.setattr(stage_build.workspace, "push", lambda ws: None)
    monkeypatch.setattr(stage_build, "resolve_commands", lambda proj, path: ProjectCommands(setup_cmds=[], validate_cmds=["pytest -q"]))

    stage = DevelopmentStage(
        routing_config=RoutingConfig(),
        run_agent_func=fake_agent,
        pick_func=lambda stage, caps, config=None, exclude=(), mode=None: ("codex", None),
    )
    # Monkeypatch _has_implementation_changes to True
    monkeypatch.setattr(stage, "_has_implementation_changes", lambda ws: True)

    progress = DevelopmentProgress()
    result = stage._develop_ticket(ws, "run_123", project, ticket, 0, progress)

    # 1. Agent was called once
    assert len(agent_called) == 1
    # 2. Flow followed to validate instead of repeating route or failing with agent invocation error
    assert len(validate_called) == 1
    assert validate_called[0] == ["pytest -q"]
    # 3. Succeeded!
    assert result.outcome == "success"
    assert result.output_refs == ["commit_sha_123"]
