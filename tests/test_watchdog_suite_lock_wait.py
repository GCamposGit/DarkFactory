"""USR-253: the worker watchdog must not count suite_lock queue time against the job deadline.

The runner prints ``[SUITE_LOCK] acquired slot N after waiting Xs`` when it had to queue; the worker
extends the job deadline by X (capped, ``DARKFAC_WORKER_SUITE_LOCK_WAIT_CAP_SEC``, default 3600).

These tests skip until the proposal patch ``docs/proposals/USR-253-watchdog-suite-lock.patch`` is applied
to ``core/harness/remote_worker.py`` (a protected file), and are plain Python (no OS assumptions).
"""

from __future__ import annotations

import subprocess
import threading
import types
from pathlib import Path

import pytest

from core.harness import remote_worker

pytestmark = pytest.mark.skipif(
    not hasattr(remote_worker, "_JobDeadline"),
    reason="USR-253 patch (watchdog suite_lock deadline extension) not applied yet",
)

ACQUIRED = "[SUITE_LOCK] acquired slot 0 after waiting {sec}s (queue time is not counted against any step timeout)\n"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DARKFAC_WORKER_SUITE_LOCK_WAIT_CAP_SEC", raising=False)


def test_parse_wait_from_real_line_format() -> None:
    assert remote_worker.parse_suite_lock_wait_sec(ACQUIRED.format(sec="1234.5")) == 1234.5
    assert remote_worker.parse_suite_lock_wait_sec(ACQUIRED.format(sec="7").rstrip()) == 7.0
    assert remote_worker.parse_suite_lock_wait_sec("2026 [SUITE_LOCK] acquired slot 3 after waiting 0.0s") == 0.0


@pytest.mark.parametrize(
    "line",
    [
        "",
        "[SUITE_LOCK] waiting for slot (queue position 2)\n",
        "[SUITE_LOCK] holder appears stalled: pid 1 after waiting 90s\n",
        "[SUITE_LOCK] acquired slot 0 after waiting soon\n",
        "[SUITE_LOCK] acquired slot 0 after waiting -5s\n",
        "[SUITE_LOCK] acquired slot 0 after waiting nans\n",
    ],
)
def test_parse_ignores_unrelated_or_malformed_lines(line: str) -> None:
    assert remote_worker.parse_suite_lock_wait_sec(line) is None


def test_queue_longer_than_step_timeouts_does_not_expire_deadline() -> None:
    clock = _Clock()
    deadline = remote_worker._JobDeadline(100.0, clock=clock, cap_sec=3600.0)
    clock.now += 90.0
    assert not deadline.expired()
    # Queue time (500 s) is far above the 100 s of step timeouts.
    assert deadline.extend_for_line(ACQUIRED.format(sec="500.0")) == 500.0
    clock.now += 500.0  # 590 s since start > 100 s
    assert not deadline.expired()
    clock.now += 11.0  # 601 s > 100 + 500
    assert deadline.expired()


def test_deadline_without_lock_line_still_expires() -> None:
    clock = _Clock()
    deadline = remote_worker._JobDeadline(100.0, clock=clock, cap_sec=3600.0)
    assert deadline.extend_for_line("some pytest output\n") == 0.0
    clock.now += 101.0
    assert deadline.expired()


def test_extension_is_capped_in_total() -> None:
    clock = _Clock()
    deadline = remote_worker._JobDeadline(10.0, clock=clock, cap_sec=3600.0)
    assert deadline.extend_for_line(ACQUIRED.format(sec="99999")) == 3600.0
    assert deadline.extend_for_line(ACQUIRED.format(sec="50")) == 0.0
    clock.now += 3609.0
    assert not deadline.expired()
    clock.now += 2.0
    assert deadline.expired()


def test_cap_is_configurable_through_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert remote_worker._suite_lock_wait_cap_sec() == 3600.0
    monkeypatch.setenv("DARKFAC_WORKER_SUITE_LOCK_WAIT_CAP_SEC", "120")
    assert remote_worker._suite_lock_wait_cap_sec() == 120.0
    clock = _Clock()
    deadline = remote_worker._JobDeadline(10.0, clock=clock)
    assert deadline.extend_for_line(ACQUIRED.format(sec="500")) == 120.0
    monkeypatch.setenv("DARKFAC_WORKER_SUITE_LOCK_WAIT_CAP_SEC", "0")
    assert remote_worker._suite_lock_wait_cap_sec() == 0.0
    for bad in ("abc", "-3", "inf", "nan"):
        monkeypatch.setenv("DARKFAC_WORKER_SUITE_LOCK_WAIT_CAP_SEC", bad)
        assert remote_worker._suite_lock_wait_cap_sec() == 3600.0


class _QueueingPopen:
    """Fake runner: reports a lock wait, then stays 'busy' longer than the job timeout."""

    instances: list["_QueueingPopen"] = []
    report_wait = True
    busy_sec = 1.5

    def __init__(self, cmd: list[str], **kwargs: object) -> None:
        self.killed = threading.Event()
        self.returncode = 0
        self.pid = None
        type(self).instances.append(self)
        owner = self

        class _Stream:
            def __iter__(self_inner):  # noqa: N805
                if owner.report_wait:
                    yield ACQUIRED.format(sec="30.0")
                # Busy for longer than the (tiny) job timeout; a kill ends it early.
                owner.killed.wait(owner.busy_sec)
                if not owner.killed.is_set():
                    yield '[HARNESS_RESULT] {"ok": true}\n'

        self.stdout = _Stream()

    def wait(self) -> int:
        self.killed.wait(self.busy_sec + 0.5)
        if self.killed.is_set():
            self.returncode = -9
        return self.returncode

    def kill(self) -> None:
        self.killed.set()


def _run_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, report_wait: bool) -> _QueueingPopen:
    _QueueingPopen.instances.clear()
    _QueueingPopen.report_wait = report_wait
    fake_subprocess = types.SimpleNamespace(
        run=lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
        Popen=_QueueingPopen,
        PIPE=subprocess.PIPE,
        STDOUT=subprocess.STDOUT,
    )
    if hasattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS"):
        fake_subprocess.BELOW_NORMAL_PRIORITY_CLASS = subprocess.BELOW_NORMAL_PRIORITY_CLASS
    monkeypatch.setattr(remote_worker, "subprocess", fake_subprocess)
    monkeypatch.setattr(remote_worker, "_job_timeout_sec", lambda *a, **k: 0.4)  # << busy_sec
    monkeypatch.setattr(remote_worker, "JOB_WATCHDOG_POLL_SEC", 0.05)
    monkeypatch.setenv("DARKFAC_WORKER_STATE_DIR", str(tmp_path / "worker_state"))
    monkeypatch.setenv("DARKFAC_SUITE_LOCK", "off")
    (tmp_path / "root").mkdir()
    manager = remote_worker.JobManager(tmp_path / "root", state_dir=tmp_path / "worker_state")
    job = remote_worker.HarnessJob(
        job_id="job-lock-wait",
        candidate_sha="a" * 40,
        tree_sha="b" * 40,
        quick=True,
        include_holdout=False,
        requesting_host="test-host",
        ref_name="refs/darkfac/jobs/job-lock-wait",
    )
    manager._run_job(job)
    assert len(_QueueingPopen.instances) == 1, job.error
    return _QueueingPopen.instances[0]


def test_run_job_survives_long_lock_queue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    popen = _run_job(monkeypatch, tmp_path, report_wait=True)
    assert not popen.killed.is_set(), "watchdog killed a job that only waited in the suite_lock queue"


def test_run_job_without_lock_line_is_still_killed_at_deadline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    popen = _run_job(monkeypatch, tmp_path, report_wait=False)
    assert popen.killed.is_set()
