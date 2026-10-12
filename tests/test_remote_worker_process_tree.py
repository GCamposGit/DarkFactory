"""USR-183 / USR-186: the worker must end the runner's whole process tree and restart without
leaving the port (and the worker) down.

USR-183 -- the watchdog and ``POST /system/restart`` used to kill only the top process. A runner
whose pytest-xdist children (grandchildren of the worker) kept the stdout pipe open left the log
reader blocked forever. Here a REAL grandchild holds the pipe.

USR-186 -- ``_wait_for_released_port`` gave up after ~10 s and left the worker down with
``[Errno 10048]``. Here the port is really occupied (a listening socket) or simulated with a
virtual clock.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import psutil
import pytest

from core.harness import remote_worker
from core.harness.remote_worker import HarnessJob, JobManager, _ProcessTree

# The "runner": starts a grandchild that inherits the stdout pipe, prints its pid and then
# either exits (the runner died, the grandchild lives on) or keeps running (a hung runner).
_RUNNER_SCRIPT = """
import subprocess, sys, time
grandchild = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
    stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=subprocess.STDOUT,
)
print(grandchild.pid, flush=True)
time.sleep(1.2 if sys.argv[1] == "exit" else 120)
"""


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_WORKER_STATE_DIR", str(tmp_path / "worker_state"))
    monkeypatch.setenv("DARKFAC_SUITE_LOCK", "off")
    monkeypatch.delenv("DARKFAC_WORKER_TOKEN", raising=False)
    monkeypatch.delenv(remote_worker.ENV_WORKER_PORT_WAIT_SEC, raising=False)


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _kill_quietly(pids: list[int]) -> None:
    for pid in pids:
        try:
            psutil.Process(pid).kill()
        except psutil.Error:
            pass


def _job(job_id: str = "t" * 32) -> HarnessJob:
    return HarnessJob(
        job_id=job_id,
        candidate_sha="c" * 40,
        tree_sha="d" * 40,
        quick=True,
        include_holdout=False,
        requesting_host="client-host",
        ref_name=f"refs/darkfac/validate/{'c' * 40}",
    )


def _manager(tmp_path: Path) -> JobManager:
    (tmp_path / "root").mkdir(exist_ok=True)
    return JobManager(tmp_path / "root", state_dir=tmp_path / "worker_state")


def _run_job_with_real_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mode: str, timeout_sec: float
) -> tuple[JobManager, HarnessJob, float, list[int]]:
    """Drive ``JobManager._run_job`` with a real runner process (``mode``: exit | hang)."""

    real_popen = subprocess.Popen

    def _popen(cmd: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        kwargs["cwd"] = str(tmp_path)
        return real_popen([sys.executable, "-c", _RUNNER_SCRIPT, mode], **kwargs)

    fake_subprocess = types.SimpleNamespace(
        run=lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
        Popen=_popen,
        PIPE=subprocess.PIPE,
        STDOUT=subprocess.STDOUT,
    )
    if hasattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS"):
        fake_subprocess.BELOW_NORMAL_PRIORITY_CLASS = subprocess.BELOW_NORMAL_PRIORITY_CLASS
    monkeypatch.setattr(remote_worker, "subprocess", fake_subprocess)
    monkeypatch.setattr(remote_worker, "_job_timeout_sec", lambda *a, **k: timeout_sec)
    monkeypatch.setattr(remote_worker, "JOB_WATCHDOG_POLL_SEC", 0.1)
    monkeypatch.setattr(remote_worker, "JOB_TREE_SNAPSHOT_SEC", 0.1)
    monkeypatch.setattr(remote_worker, "JOB_PIPE_DRAIN_GRACE_SEC", 1.0)

    manager = _manager(tmp_path)
    job = _job()
    manager._register(job)
    started = time.monotonic()
    thread = threading.Thread(target=manager._run_job, args=(job,), daemon=True)
    thread.start()
    thread.join(40.0)
    elapsed = time.monotonic() - started
    pids = [int(line) for line in "".join(job.log_chunks).split() if line.isdigit()]
    if job.child_pid:
        pids.append(job.child_pid)
    if thread.is_alive():
        _kill_quietly(pids)  # do not leave the sleeping processes behind when the assertion fails
    assert not thread.is_alive(), "_run_job stayed blocked on the stdout pipe held by a grandchild"
    return manager, job, elapsed, pids


# --- USR-183: the process tree ------------------------------------------------------


def test_run_job_does_not_block_when_the_runner_died_but_a_grandchild_holds_the_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pids: list[int] = []
    try:
        _, job, elapsed, pids = _run_job_with_real_runner(tmp_path, monkeypatch, mode="exit", timeout_sec=120.0)
        log = "".join(job.log_chunks)
        assert "leftover process" in log
        assert elapsed < 30.0
        assert job.is_terminal()
        time.sleep(0.5)
        assert not any(_alive(pid) for pid in pids), "the grandchild that held the pipe must be gone"
    finally:
        _kill_quietly(pids)


def test_watchdog_timeout_kills_the_whole_tree_not_only_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pids: list[int] = []
    try:
        _, job, elapsed, pids = _run_job_with_real_runner(tmp_path, monkeypatch, mode="hang", timeout_sec=1.5)
        assert elapsed < 30.0
        assert job.is_terminal()
        assert len(pids) >= 2, "expected the grandchild pid (from the log) and the runner pid"
        time.sleep(0.5)
        assert not any(_alive(pid) for pid in pids), "runner and grandchild must both be gone"
    finally:
        _kill_quietly(pids)


def test_process_tree_reaches_grandchildren_after_the_runner_exited() -> None:
    runner = subprocess.Popen(
        [sys.executable, "-c", _RUNNER_SCRIPT, "hang"], stdout=subprocess.PIPE, text=True, encoding="utf-8"
    )
    grandchild_pid = 0
    try:
        assert runner.stdout is not None
        grandchild_pid = int(runner.stdout.readline())
        tree = _ProcessTree(runner.pid, remote_worker._process_started_at(runner.pid))
        tree.refresh()
        runner.kill()
        runner.wait(timeout=10)
        assert _alive(grandchild_pid), "the orphaned grandchild outlives the runner (the USR-183 situation)"
        assert tree.kill(force=True) >= 1
        deadline = time.monotonic() + 10
        while _alive(grandchild_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(grandchild_pid)
    finally:
        runner.kill()
        runner.wait(timeout=10)
        if grandchild_pid:
            _kill_quietly([grandchild_pid])


def test_process_tree_never_touches_a_recycled_pid() -> None:
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        # Same pid, but a start time that does not match: not "our" runner.
        tree = _ProcessTree(victim.pid, remote_worker._process_started_at(victim.pid) - 3600.0)
        assert tree.kill(force=True) == 0
        assert victim.poll() is None
    finally:
        victim.kill()
        victim.wait(timeout=10)


def test_restart_endpoint_kills_leftovers_of_a_runner_that_already_exited(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    job = _job()
    manager._register(job)
    manager._set_status(job, "running")
    runner = subprocess.Popen(
        [sys.executable, "-c", _RUNNER_SCRIPT, "exit"], stdout=subprocess.PIPE, text=True, encoding="utf-8"
    )
    grandchild_pid = 0
    try:
        assert runner.stdout is not None
        grandchild_pid = int(runner.stdout.readline())
        tree = _ProcessTree(runner.pid, remote_worker._process_started_at(runner.pid))
        tree.refresh()
        runner.wait(timeout=15)  # the runner is gone; poll() is no longer None
        job.process = runner
        job.process_tree = tree
        assert _alive(grandchild_pid)

        assert manager.mark_interrupted("test restart") == [job.job_id]

        deadline = time.monotonic() + 10
        while _alive(grandchild_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(grandchild_pid)
    finally:
        runner.kill()
        runner.wait(timeout=10)
        if grandchild_pid:
            _kill_quietly([grandchild_pid])


# --- USR-186: waiting for the port ------------------------------------------------------


class _VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_port_wait_default_is_much_longer_than_the_old_ten_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    assert remote_worker._port_wait_timeout_sec() == 60.0
    monkeypatch.setenv(remote_worker.ENV_WORKER_PORT_WAIT_SEC, "5")
    assert remote_worker._port_wait_timeout_sec() == 5.0
    for bad in ("abc", "0", "-3"):
        monkeypatch.setenv(remote_worker.ENV_WORKER_PORT_WAIT_SEC, bad)
        assert remote_worker._port_wait_timeout_sec() == 60.0


def test_wait_keeps_waiting_past_the_old_limit_with_bounded_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _VirtualClock()
    monkeypatch.setattr(remote_worker, "_port_is_free", lambda host, port: clock.now >= 30.0)
    assert remote_worker._wait_for_released_port("127.0.0.1", 8080, sleep=clock.sleep, clock=clock) is True
    assert sum(clock.sleeps) >= 30.0, "must wait well past the old 10 s limit"
    assert clock.sleeps[0] == remote_worker.PORT_WAIT_INITIAL_DELAY_SEC
    assert max(clock.sleeps) <= remote_worker.PORT_WAIT_MAX_DELAY_SEC


def test_wait_with_a_really_occupied_port_gives_up_only_at_the_limit_and_succeeds_when_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(remote_worker, "_worker_answers_health", lambda port: False)
    monkeypatch.setattr(remote_worker, "_reap_stale_worker_listener", lambda port: 0)
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]
        assert remote_worker._port_is_free("127.0.0.1", port) is False

        started = time.monotonic()
        assert remote_worker._wait_for_released_port("127.0.0.1", port, timeout_sec=0.6) is False
        assert time.monotonic() - started >= 0.5

        timer = threading.Timer(0.5, occupied.close)
        timer.start()
        try:
            assert remote_worker._wait_for_released_port("127.0.0.1", port, timeout_sec=30.0) is True
        finally:
            timer.join()
    finally:
        occupied.close()


def test_wait_does_not_relaunch_when_a_worker_already_serves_the_port(monkeypatch: pytest.MonkeyPatch) -> None:
    reaped: list[int] = []
    monkeypatch.setattr(remote_worker, "_port_is_free", lambda host, port: False)
    monkeypatch.setattr(remote_worker, "_worker_answers_health", lambda port: True)
    monkeypatch.setattr(remote_worker, "_reap_stale_worker_listener", lambda port: reaped.append(port) or 1)
    clock = _VirtualClock()
    assert remote_worker._wait_for_released_port("0.0.0.0", 8080, timeout_sec=3.0, sleep=clock.sleep, clock=clock) is False
    assert reaped == [], "a healthy worker must never be terminated"


def test_wait_terminates_a_hung_predecessor_and_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"reaped": False}
    clock = _VirtualClock()
    monkeypatch.setattr(remote_worker, "_port_is_free", lambda host, port: state["reaped"])
    monkeypatch.setattr(remote_worker, "_worker_answers_health", lambda port: False)

    def _reap(port: int) -> int:
        state["reaped"] = True
        return 1

    monkeypatch.setattr(remote_worker, "_reap_stale_worker_listener", _reap)
    assert remote_worker._wait_for_released_port("0.0.0.0", 8080, timeout_sec=2.0, sleep=clock.sleep, clock=clock) is True


def test_restart_helper_launches_once_after_a_slow_port_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scenario of the incident: the old instance releases port 8080 only after ~25 s."""

    clock = _VirtualClock()
    real_wait = remote_worker._wait_for_released_port
    monkeypatch.setattr(
        remote_worker, "_wait_for_released_port", lambda host, port: real_wait(host, port, sleep=clock.sleep, clock=clock)
    )
    monkeypatch.setattr(remote_worker, "_port_is_free", lambda host, port: clock.now >= 25.0)
    launched: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(remote_worker.sys, "platform", "linux")
    monkeypatch.setattr(remote_worker.subprocess, "Popen", lambda cmd, **kw: launched.append((cmd, kw)))

    remote_worker._restart_after_exit(tmp_path, "0.0.0.0", 8080, "desktop-x")

    assert len(launched) == 1
    command, kwargs = launched[0]
    assert command[command.index("--port") + 1] == "8080"
    assert kwargs["close_fds"] is True


# --- USR-186: the child must not inherit the listening socket ----------------------


def test_detached_child_does_not_inherit_the_listening_socket(tmp_path: Path) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    child = None
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.set_inheritable(True)  # worst case: even an inheritable descriptor must not leak
        port = listener.getsockname()[1]
        kwargs = remote_worker._detached_popen_kwargs(tmp_path)
        assert kwargs["close_fds"] is True
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        listener.close()  # the old daemon exits; the new child is still running
        assert child.poll() is None
        assert remote_worker._port_is_free("127.0.0.1", port), "the child kept the old listening socket alive"
    finally:
        listener.close()
        if child is not None:
            child.kill()
            child.wait(timeout=10)


def test_every_daemon_launch_uses_the_detached_kwargs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(remote_worker.subprocess, "Popen", lambda cmd, **kw: calls.append(kw))
    monkeypatch.setattr(remote_worker, "_port_is_free", lambda host, port: True)
    monkeypatch.setattr(remote_worker.sys, "platform", "linux")
    remote_worker._restart_after_exit(tmp_path, "127.0.0.1", 8181, "n1")

    started = threading.Event()
    monkeypatch.setattr(remote_worker.time, "sleep", lambda s: None)
    monkeypatch.setattr(remote_worker.os, "_exit", lambda code: started.set())
    remote_worker.trigger_daemon_restart(tmp_path, "127.0.0.1", 8181, "n1")
    assert started.wait(10.0)

    assert len(calls) == 2
    assert all(call["close_fds"] is True and call["stdin"] is subprocess.DEVNULL for call in calls)
