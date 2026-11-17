"""USR-178 (criteria 1 and 3): suite-lock queue time is not step time, and every run
records the lock wait separately from the suite execution time.

Real ``SuiteLock`` on a per-test state directory (never the machine-global one),
real ``run_with_cache``/``execute``/``run_step`` with tiny ``python <script>`` steps:
no network, no git (clean-tree and candidate-SHA checks are stubbed), no real suite.
"""

from __future__ import annotations

import contextlib
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


# --- USR-463: runner reports suite-lock degradation instead of crashing -------


def _deny_canonical_sidecar_write(canonical: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a restricted identity that can lock the canonical file but
    cannot write the sidecar metadata in the canonical state directory.

    The degraded path must cover this case too, so the gate and its timing log
    use one writable per-identity directory.
    """

    canonical.mkdir(parents=True, exist_ok=True)
    lock_path = canonical / "suite-0.lock"
    lock_path.touch(exist_ok=True)
    sidecar_tmp = canonical / "suite-0.tmp"
    real_open = Path.open

    def _denied_sidecar_write(self: Path, mode: str = "r", *args: object, **kwargs: object):
        if self == sidecar_tmp and mode == "w":
            raise PermissionError(13, "Permission denied")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _denied_sidecar_write)
    monkeypatch.setenv("USERNAME", "sandboxed-user")


def test_gate_passes_and_reports_degraded_lock_when_canonical_sidecar_is_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """USR-463: a gate running under a restricted identity still reaches
    [HARNESS_PASS], and the per-run record shows which (degraded) directory
    the lock -- and the timing log itself -- actually used, instead of either
    crashing with a traceback or silently losing the telemetry."""

    canonical = tmp_path / "state"
    _deny_canonical_sidecar_write(canonical, monkeypatch)

    passed = _run(_config(tmp_path, sleep_sec=0.1, timeout_sec=30))

    out = capsys.readouterr().out
    assert passed is True
    assert "[STEP_PASS] probe" in out
    assert runner.MARKER_HARNESS_PASS in out
    assert "sandboxed-user" in out  # the one degraded [SUITE_LOCK] warning names the fallback dir

    fallback = canonical / "darkfac-harness-sandboxed-user"
    assert not (canonical / runner.RUN_TIMINGS_LOG_NAME).exists()
    (record,) = _timing_records_at(fallback)
    assert record["success"] is True
    assert record["lock_degraded"] is True
    assert record["lock_state_dir"] == str(fallback)


def _timing_records_at(directory: Path) -> list[dict]:
    log = directory / runner.RUN_TIMINGS_LOG_NAME
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_append_run_timing_uses_the_directory_recorded_for_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """USR-463: telemetry follows the fallback selected by lock acquisition,
    even when a fresh probe can still read the canonical lock."""

    canonical = tmp_path / "state"
    _deny_canonical_sidecar_write(canonical, monkeypatch)
    fallback = canonical / "darkfac-harness-sandboxed-user"
    fallback.mkdir(parents=True)
    record = {"schema": 1, "probe": True, "lock_state_dir": str(fallback)}

    runner._append_run_timing(record)

    assert not (canonical / runner.RUN_TIMINGS_LOG_NAME).exists()
    (record,) = _timing_records_at(fallback)
    assert record == {"schema": 1, "probe": True, "lock_state_dir": str(fallback)}


def test_timed_suite_lock_reports_suite_lock_unwritable_as_a_clean_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """USR-463: the residual case where even the degraded fallback directory
    turns out unwritable must surface as the structured ``[SUITE_LOCK]``
    message ``SuiteLockUnwritable`` already carries, not a bare traceback --
    in addition to the existing ``SuiteLockTimeout`` handling."""

    @contextlib.contextmanager
    def _boom(**_kwargs: object):
        raise suite_lock.SuiteLockUnwritable(Path("unused") / "suite-0.lock", "denied")
        yield  # pragma: no cover - unreachable, required to make this a generator

    monkeypatch.setattr(runner.harness_suite_lock, "suite_lock", _boom)

    timings = runner.RunTimings()
    with pytest.raises(suite_lock.SuiteLockUnwritable):
        with runner._timed_suite_lock(timings):
            pass  # pragma: no cover - the context manager raises on __enter__

    out = capsys.readouterr().out
    assert "[SUITE_LOCK] cannot access lock state" in out
    assert "Traceback" not in out


def test_run_with_cache_propagates_suite_lock_unwritable_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same residual case as above, exercised through the real ``run_with_cache``
    path: the run still fails closed (no false [HARNESS_PASS]), the per-run
    timing record is still written, and nothing resembling a raw traceback
    reaches the gate's stdout."""

    @contextlib.contextmanager
    def _boom(**_kwargs: object):
        raise suite_lock.SuiteLockUnwritable(Path("unused") / "suite-0.lock", "denied")
        yield  # pragma: no cover

    monkeypatch.setattr(runner.harness_suite_lock, "suite_lock", _boom)

    with pytest.raises(suite_lock.SuiteLockUnwritable):
        _run(_config(tmp_path, sleep_sec=0.1, timeout_sec=30))

    out = capsys.readouterr().out
    assert "[SUITE_LOCK] cannot access lock state" in out
    assert "Traceback" not in out
    assert runner.MARKER_HARNESS_PASS not in out
    (record,) = _timing_records(tmp_path)
    assert record["success"] is None
