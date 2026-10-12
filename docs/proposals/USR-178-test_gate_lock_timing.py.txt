"""USR-178 (criteria 1 and 3): suite-lock queue time is not step time, and every run
records the lock wait separately from the suite execution time.

Real ``SuiteLock`` on a per-test state directory (never the machine-global one),
real ``run_with_cache``/``execute``/``run_step`` with tiny ``python <script>`` steps:
no network, no git (clean-tree and candidate-SHA checks are stubbed), no real suite.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from core.harness import runner, suite_lock
from core.harness.models import HarnessConfig, HarnessStepConfig

HOLD_SEC = 3.0
STEP_TIMEOUT_SEC = 2  # shorter than HOLD_SEC on purpose


@pytest.fixture(autouse=True)
def _isolated_runner_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_HARNESS_STATE_DIR", str(tmp_path / "state"))
    for name in (
        "DARKFAC_SUITE_LOCK_HELD",
        "DARKFAC_SUITE_LOCK",
        "DARKFAC_SUITE_SLOTS",
        "DARKFAC_SUITE_LOCK_TIMEOUT_SEC",
        "DARKFAC_HARNESS_WORKER_JOB",
        "CI",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runner, "_ensure_clean_worktree", lambda: None)
    monkeypatch.setattr(runner, "_candidate_sha", lambda: "c" * 40)
    monkeypatch.setattr(runner, "_notify_hub_on_pass", lambda: None)


def _config(tmp_path: Path, *, sleep_sec: float, timeout_sec: int) -> HarnessConfig:
    script = tmp_path / "step_ok.py"
    script.write_text(
        "import sys, time\n"
        "time.sleep(float(sys.argv[1]))\n"
        "print('1 passed in 0.01s')\n",
        encoding="utf-8",
    )
    cmd = f'"{sys.executable}" "{script}" {sleep_sec}'
    return HarnessConfig(
        steps=[HarnessStepConfig(name="probe", cmd=cmd, quick=True, kind="test", timeout_sec=timeout_sec)]
    )


def _run(config: HarnessConfig) -> bool:
    return runner.run_with_cache(
        config,
        config_hash="a" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
        no_cache=True,
        local_only=True,
    )


def _timing_records(tmp_path: Path) -> list[dict]:
    log = tmp_path / "state" / runner.RUN_TIMINGS_LOG_NAME
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _hold_lock(seconds: float) -> tuple[suite_lock.SuiteLock, threading.Timer]:
    holder = suite_lock.SuiteLock(timeout_sec=30)
    holder.acquire()
    timer = threading.Timer(seconds, holder.release)
    timer.start()
    return holder, timer


def test_waiting_for_the_suite_lock_is_not_charged_to_the_step_timeout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Criterion 1: the queue (3 s) is longer than the step timeout (2 s) and the run still passes."""

    holder, timer = _hold_lock(HOLD_SEC)
    try:
        passed = _run(_config(tmp_path, sleep_sec=0.2, timeout_sec=STEP_TIMEOUT_SEC))
    finally:
        timer.cancel()
        holder.release()

    out = capsys.readouterr().out
    assert passed is True
    assert "[STEP_PASS] probe" in out
    assert "timeout exceeded" not in out
    assert "[SUITE_LOCK] waiting" in out
    assert "[SUITE_LOCK] acquired slot" in out

    (record,) = _timing_records(tmp_path)
    assert record["lock_wait_sec"] >= HOLD_SEC - 0.5
    assert record["lock_wait_sec"] > STEP_TIMEOUT_SEC
    assert record["exec_sec"] < STEP_TIMEOUT_SEC
    assert record["lock_timed_out"] is False


def test_run_timing_is_printed_once_and_logged_with_lock_wait_separate_from_exec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Criterion 3: one record per execution, queue time and execution time in separate fields."""

    passed = _run(_config(tmp_path, sleep_sec=0.4, timeout_sec=30))

    out = capsys.readouterr().out
    assert passed is True
    assert out.count(runner.MARKER_RUN_TIMING) == 1
    printed = json.loads(out.split(runner.MARKER_RUN_TIMING + " ", 1)[1].splitlines()[0])
    (logged,) = _timing_records(tmp_path)
    assert printed == {**logged}
    assert logged["success"] is True
    assert logged["path"] == "local"
    assert logged["lock_wait_sec"] < 1.0  # free lock: no queue
    assert logged["exec_sec"] >= 0.4
    assert logged["total_sec"] >= logged["exec_sec"]
    assert logged["remote_lost_sec"] == 0
    assert logged["remote_attempts"] == []
    assert logged["steps_timeout_sum_sec"] == 30.0
    assert logged["worker_job"] is False


def test_step_timeout_still_bounds_the_execution_itself(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Not a free pass: a step that really runs past its limit still fails closed."""

    passed = _run(_config(tmp_path, sleep_sec=4.0, timeout_sec=1))

    out = capsys.readouterr().out
    assert passed is False
    assert "[STEP_FAIL] probe (timeout exceeded)" in out
    (record,) = _timing_records(tmp_path)
    assert record["success"] is False
    assert record["lock_wait_sec"] < 1.0
    assert record["exec_sec"] >= 1.0


def test_lock_timeout_fails_closed_and_the_wait_is_still_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder, timer = _hold_lock(30.0)
    monkeypatch.setenv("DARKFAC_SUITE_LOCK_TIMEOUT_SEC", "1")
    try:
        with pytest.raises(suite_lock.SuiteLockTimeout) as excinfo:
            _run(_config(tmp_path, sleep_sec=0.1, timeout_sec=30))
    finally:
        timer.cancel()
        holder.release()

    assert excinfo.value.waited_sec >= 0.9
    (record,) = _timing_records(tmp_path)
    assert record["lock_timed_out"] is True
    assert record["success"] is None
    assert record["lock_wait_sec"] >= 0.9
    assert record["exec_sec"] == 0


def test_a_broken_timing_log_never_changes_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(record: dict) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(runner, "_append_run_timing", _boom)

    assert _run(_config(tmp_path, sleep_sec=0.1, timeout_sec=30)) is True
    assert "[STEP_PASS] probe" in capsys.readouterr().out


def test_timing_log_is_truncated_when_it_grows_too_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "RUN_TIMINGS_LOG_MAX_BYTES", 400)
    monkeypatch.setattr(runner, "RUN_TIMINGS_LOG_KEEP_LINES", 3)

    for index in range(10):
        runner._append_run_timing({"schema": 1, "n": index, "pad": "x" * 60})

    lines = _timing_records(tmp_path)
    assert [item["n"] for item in lines][-1] == 9
    assert len(lines) <= 4


def test_suite_lock_reports_how_long_acquire_waited() -> None:
    free = suite_lock.SuiteLock(timeout_sec=5)
    assert free.waited_sec is None
    with free:
        assert free.waited_sec is not None and free.waited_sec < 1.0

    holder, timer = _hold_lock(1.0)
    waiter = suite_lock.SuiteLock(timeout_sec=10)
    try:
        waiter.acquire()
        assert waiter.waited_sec is not None and waiter.waited_sec >= 0.8
    finally:
        timer.cancel()
        holder.release()
        waiter.release()


def test_remote_attempts_lost_to_a_fallback_are_accounted_apart_from_lock_and_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote run that was lost and fell back to local keeps its seconds in the record."""

    def _fake_dispatch(**_kwargs: object) -> None:
        runner.remote_dispatch.DISPATCH_ATTEMPTS.append(
            {"worker_url": "http://w", "job_id": "j1", "outcome": "lost", "error_code": "worker_unreachable", "elapsed_sec": 75.0}
        )
        return None  # fall back to local

    monkeypatch.setattr(runner.remote_dispatch, "maybe_dispatch_remote", _fake_dispatch)

    assert runner.run_with_cache(
        _config(tmp_path, sleep_sec=0.1, timeout_sec=30),
        config_hash="a" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
        no_cache=True,
    )

    (record,) = _timing_records(tmp_path)
    assert record["path"] == "local_after_remote"
    assert record["remote_lost_sec"] == 75.0
    assert record["exec_sec"] < 30  # the 75 s lost remotely are NOT in the local execution time
    assert record["remote_attempts"][0]["error_code"] == "worker_unreachable"
