"""Stalled-holder detection of the machine-wide suite lock (USR-147) and the
``--no-cache`` lock-before-execute behaviour of the runner (USR-135/147).

Every test points ``DARKFAC_HARNESS_STATE_DIR`` at a fresh ``tmp_path`` so the
real machine-global lock is never touched.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from core.harness import runner, suite_lock

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("DARKFAC_HARNESS_STATE_DIR", str(state_dir))
    for key in (
        "DARKFAC_SUITE_LOCK_HELD",
        "DARKFAC_SUITE_LOCK",
        "DARKFAC_SUITE_SLOTS",
        "DARKFAC_SUITE_LOCK_TIMEOUT_SEC",
        "DARKFAC_SUITE_LOCK_STALL_AFTER_SEC",
        "DARKFAC_SUITE_LOCK_STALL_WINDOW_SEC",
        "DARKFAC_SUITE_LOCK_REAP_STALLED",
    ):
        monkeypatch.delenv(key, raising=False)
    return state_dir


def _idle_holder_script(state_dir: Path, marker: Path) -> str:
    """A process that takes the lock and then sleeps (alive, ~0% CPU)."""

    return textwrap.dedent(
        f"""
        import os, sys, time
        sys.path.insert(0, {str(REPO_ROOT)!r})
        os.environ["DARKFAC_HARNESS_STATE_DIR"] = {str(state_dir)!r}
        os.environ.pop("DARKFAC_SUITE_LOCK_HELD", None)
        from core.harness import suite_lock
        lock = suite_lock.SuiteLock(timeout_sec=30)
        lock.acquire()
        open({str(marker)!r}, "w", encoding="utf-8").write("acquired")
        time.sleep(120)
        """
    )


@contextlib.contextmanager
def _idle_holder(tmp_path: Path):
    state_dir = tmp_path / "state"
    marker = tmp_path / "holder_acquired.txt"
    proc = subprocess.Popen([sys.executable, "-c", _idle_holder_script(state_dir, marker)])
    try:
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "holder never acquired the lock"
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=15)


# --- env parsing ------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "abc", "-5", "nan", "inf"])
def test_invalid_stall_envs_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("DARKFAC_SUITE_LOCK_STALL_AFTER_SEC", raw)
    monkeypatch.setenv("DARKFAC_SUITE_LOCK_STALL_WINDOW_SEC", raw)
    assert suite_lock._stall_after_sec() == 600.0
    assert suite_lock._stall_window_sec() == 10.0


def test_valid_stall_envs_are_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_SUITE_LOCK_STALL_AFTER_SEC", "0")
    monkeypatch.setenv("DARKFAC_SUITE_LOCK_STALL_WINDOW_SEC", "2.5")
    assert suite_lock._stall_after_sec() == 0.0
    assert suite_lock._stall_window_sec() == 2.5


def test_reap_is_off_unless_explicitly_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    assert suite_lock._reap_stalled_enabled() is False
    for raw in ("", "0", "true", "yes", "on"):
        monkeypatch.setenv("DARKFAC_SUITE_LOCK_REAP_STALLED", raw)
        assert suite_lock._reap_stalled_enabled() is False
    monkeypatch.setenv("DARKFAC_SUITE_LOCK_REAP_STALLED", "1")
    assert suite_lock._reap_stalled_enabled() is True


# --- real idle holder ----------------------------------------------------------------


def test_idle_live_holder_triggers_a_stalled_warning_but_is_not_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DARKFAC_SUITE_LOCK_STALL_AFTER_SEC", "0")
    monkeypatch.setenv("DARKFAC_SUITE_LOCK_STALL_WINDOW_SEC", "1")
    with _idle_holder(tmp_path) as holder:
        waiter = suite_lock.SuiteLock(timeout_sec=6)
        with pytest.raises(suite_lock.SuiteLockTimeout):
            waiter.acquire()
        out = capsys.readouterr().out
        assert "[SUITE_LOCK] holder appears stalled" in out
        assert f"pid {holder.pid}" in out
        assert socket.gethostname() in out
        assert "DARKFAC_SUITE_LOCK_REAP_STALLED=1" in out
        assert holder.poll() is None, "reaping is opt-in; the holder must still be alive"


def test_stall_check_waits_for_the_configured_wait_before_sampling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DARKFAC_SUITE_LOCK_STALL_AFTER_SEC", "3600")
    monkeypatch.setenv("DARKFAC_SUITE_LOCK_STALL_WINDOW_SEC", "0.5")

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("must not sample the holder before the stall wait elapsed")

    monkeypatch.setattr(suite_lock, "_sample_holder_tree", _boom)
    with _idle_holder(tmp_path):
        with pytest.raises(suite_lock.SuiteLockTimeout):
            suite_lock.SuiteLock(timeout_sec=2).acquire()
    assert "appears stalled" not in capsys.readouterr().out


def test_reap_enabled_terminates_the_stalled_holder_and_takes_the_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DARKFAC_SUITE_LOCK_STALL_AFTER_SEC", "0")
    monkeypatch.setenv("DARKFAC_SUITE_LOCK_STALL_WINDOW_SEC", "1")
    monkeypatch.setenv("DARKFAC_SUITE_LOCK_REAP_STALLED", "1")
    with _idle_holder(tmp_path) as holder:
        waiter = suite_lock.SuiteLock(timeout_sec=30)
        waiter.acquire()
        try:
            assert waiter.held
            assert holder.wait(timeout=15) is not None
        finally:
            waiter.release()
    out = capsys.readouterr().out
    assert "holder appears stalled" in out
    assert "terminated stalled holder" in out


# --- deterministic unit tests of the detector -------------------------------------------


def _write_sidecar(state: Path, **overrides: object) -> Path:
    state.mkdir(parents=True, exist_ok=True)
    lock_path = state / "suite-0.lock"
    lock_path.touch()
    payload = {
        "pid": 424242,
        "hostname": socket.gethostname(),
        "started_at": time.time() - 100,
        "cwd": "C:/x",
    }
    payload.update(overrides)
    lock_path.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")
    return lock_path


def _watch(monkeypatch: pytest.MonkeyPatch, samples: list[dict[int, float] | None]) -> suite_lock._StallWatch:
    iterator = iter(samples)
    monkeypatch.setattr(suite_lock, "_sample_holder_tree", lambda pid, started_at: next(iterator))
    return suite_lock._StallWatch(stall_after=0.0, window=0.0, reap=False)


def test_cpu_advancing_in_the_tree_is_not_stalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lock_path = _write_sidecar(tmp_path / "s")
    watch = _watch(monkeypatch, [{424242: 1.0, 7: 5.0}, {424242: 1.0, 7: 9.0}])  # a child works hard
    watch.check(lock_path.parent, 1)
    watch.check(lock_path.parent, 1)
    assert "appears stalled" not in capsys.readouterr().out


def test_unchanged_cpu_in_the_whole_tree_is_stalled_and_warned_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lock_path = _write_sidecar(tmp_path / "s")
    tree = {424242: 1.0, 7: 5.0}
    watch = _watch(monkeypatch, [dict(tree), dict(tree), dict(tree), dict(tree)])
    for _ in range(4):
        watch.check(lock_path.parent, 1)
    out = capsys.readouterr().out
    assert out.count("[SUITE_LOCK] holder appears stalled") == 1
    assert "pid 424242" in out


def test_processes_joining_the_tree_count_as_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lock_path = _write_sidecar(tmp_path / "s")
    watch = _watch(monkeypatch, [{424242: 1.0}, {424242: 1.0, 99: 0.0}])
    watch.check(lock_path.parent, 1)
    watch.check(lock_path.parent, 1)
    assert "appears stalled" not in capsys.readouterr().out


def test_sidecar_from_another_host_is_never_inspected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path = _write_sidecar(tmp_path / "s", hostname="SOME-OTHER-HOST-XYZ")

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("a remote holder's pid is meaningless on this host")

    monkeypatch.setattr(suite_lock, "_sample_holder_tree", _boom)
    suite_lock._StallWatch(stall_after=0.0, window=0.0, reap=True).check(lock_path.parent, 1)


def test_own_pid_is_never_inspected_or_reaped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path = _write_sidecar(tmp_path / "s", pid=os.getpid())

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("must not inspect itself")

    monkeypatch.setattr(suite_lock, "_sample_holder_tree", _boom)
    suite_lock._StallWatch(stall_after=0.0, window=0.0, reap=True).check(lock_path.parent, 1)
    assert suite_lock._reap_holder(os.getpid()) is False


def test_recycled_pid_is_not_mistaken_for_the_holder(tmp_path: Path) -> None:
    with _idle_holder(tmp_path) as holder:
        # Sidecar claims the lock was taken long before the process existed -> pid reuse.
        assert suite_lock._sample_holder_tree(holder.pid, started_at=1.0) is None
        sample = suite_lock._sample_holder_tree(holder.pid, started_at=time.time())
        assert sample is not None and holder.pid in sample


def test_detector_failure_never_breaks_lock_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DARKFAC_SUITE_LOCK_STALL_AFTER_SEC", "0")

    def _boom(self: object, directory: Path, slots: int) -> None:
        raise RuntimeError("psutil exploded")

    monkeypatch.setattr(suite_lock._StallWatch, "check", _boom)
    with _idle_holder(tmp_path):
        with pytest.raises(suite_lock.SuiteLockTimeout):
            suite_lock.SuiteLock(timeout_sec=1.5).acquire()
    assert "stalled-holder check failed" in capsys.readouterr().out


# --- runner: lock before execute on the --no-cache path ---------------------------------


def test_no_cache_run_takes_the_suite_lock_before_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wait for the machine-wide lock must happen OUTSIDE the step subprocess,
    otherwise it is charged against the step timeout (false timeout, count=0)."""

    from core.harness.models import HarnessConfig, HarnessStepConfig

    events: list[str] = []

    @contextlib.contextmanager
    def fake_suite_lock(**_kwargs: object):
        events.append("lock_enter")
        try:
            yield None
        finally:
            events.append("lock_exit")

    def fake_execute(_config: object, **_kwargs: object) -> bool:
        events.append("execute")
        return True

    monkeypatch.setattr(runner.harness_suite_lock, "suite_lock", fake_suite_lock)
    monkeypatch.setattr(runner, "execute", fake_execute)
    monkeypatch.delenv("CI", raising=False)
    config = HarnessConfig(steps=[HarnessStepConfig(name="probe", cmd="python -c pass", quick=True, kind="check")])

    ok = runner.run_with_cache(
        config,
        config_hash="a" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
        no_cache=True,
        local_only=True,
    )
    assert ok is True
    assert events == ["lock_enter", "execute", "lock_exit"]
