"""Tests for the machine-wide suite lock (`core/harness/suite_lock.py`).

Every test points `DARKFAC_HARNESS_STATE_DIR` at a fresh `tmp_path` so these
tests never touch the real machine-global lock -- doing so could deadlock
against an unrelated harness run for up to `DARKFAC_SUITE_LOCK_TIMEOUT_SEC`
on a shared development box where other agents/harnesses may be validating
concurrently.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from core.harness import suite_lock


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("DARKFAC_HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.delenv("DARKFAC_SUITE_LOCK_HELD", raising=False)
    monkeypatch.delenv("DARKFAC_SUITE_LOCK", raising=False)
    monkeypatch.delenv("DARKFAC_SUITE_SLOTS", raising=False)
    monkeypatch.delenv("DARKFAC_SUITE_LOCK_TIMEOUT_SEC", raising=False)
    return state_dir


def test_state_dir_is_created_and_stable(tmp_path: Path) -> None:
    directory = suite_lock.state_dir()
    assert directory.is_dir()
    assert directory == suite_lock.state_dir()


def test_disabled_flag() -> None:
    assert suite_lock.is_disabled() is False
    os.environ["DARKFAC_SUITE_LOCK"] = "off"
    assert suite_lock.is_disabled() is True
    os.environ["DARKFAC_SUITE_LOCK"] = "OFF"
    assert suite_lock.is_disabled() is True


def test_held_by_ancestor_flag() -> None:
    assert suite_lock.is_held_by_ancestor() is False
    os.environ["DARKFAC_SUITE_LOCK_HELD"] = "1"
    assert suite_lock.is_held_by_ancestor() is True


def test_suite_lock_context_manager_disabled_is_a_noop(tmp_path: Path) -> None:
    os.environ["DARKFAC_SUITE_LOCK"] = "off"
    with suite_lock.suite_lock() as lock:
        assert lock is None
        assert not list((tmp_path / "state").glob("suite-*.lock"))


def test_suite_lock_context_manager_held_by_ancestor_is_a_noop() -> None:
    os.environ["DARKFAC_SUITE_LOCK_HELD"] = "1"
    with suite_lock.suite_lock() as lock:
        assert lock is None


def test_acquire_and_release_creates_and_removes_sidecar(tmp_path: Path) -> None:
    lock = suite_lock.SuiteLock(timeout_sec=5)
    lock.acquire()
    try:
        directory = tmp_path / "state"
        assert (directory / "suite-0.lock").exists()
        assert (directory / "suite-0.json").exists()
    finally:
        lock.release()
    assert not (tmp_path / "state" / "suite-0.json").exists()


def test_context_manager_sets_and_restores_env_handshake() -> None:
    assert "DARKFAC_SUITE_LOCK_HELD" not in os.environ
    with suite_lock.suite_lock() as lock:
        assert lock is not None
        assert os.environ.get("DARKFAC_SUITE_LOCK_HELD") == "1"
    assert "DARKFAC_SUITE_LOCK_HELD" not in os.environ


def test_child_env_sets_flag_without_mutating_process_environ() -> None:
    env = suite_lock.child_env({"FOO": "bar"})
    assert env["DARKFAC_SUITE_LOCK_HELD"] == "1"
    assert env["FOO"] == "bar"
    assert "DARKFAC_SUITE_LOCK_HELD" not in os.environ


def test_second_slot_is_acquired_when_first_is_held(tmp_path: Path) -> None:
    os.environ["DARKFAC_SUITE_SLOTS"] = "2"
    first = suite_lock.SuiteLock(timeout_sec=5)
    first.acquire()
    try:
        second = suite_lock.SuiteLock(timeout_sec=5)
        second.acquire()
        try:
            assert first._handle.slot != second._handle.slot  # type: ignore[union-attr]
        finally:
            second.release()
    finally:
        first.release()


def test_timeout_raises_when_no_slot_frees_up(tmp_path: Path) -> None:
    holder = suite_lock.SuiteLock(timeout_sec=5)
    holder.acquire()
    try:
        waiter = suite_lock.SuiteLock(timeout_sec=0.5)
        with pytest.raises(suite_lock.SuiteLockTimeout):
            waiter.acquire()
    finally:
        holder.release()


def _contender_script(state_dir: Path, marker_path: Path, hold_seconds: float) -> str:
    return textwrap.dedent(
        f"""
        import os
        import sys
        import time

        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        os.environ["DARKFAC_HARNESS_STATE_DIR"] = {str(state_dir)!r}
        os.environ.pop("DARKFAC_SUITE_LOCK_HELD", None)

        from core.harness import suite_lock

        lock = suite_lock.SuiteLock(timeout_sec=30)
        lock.acquire()
        with open({str(marker_path)!r}, "w", encoding="utf-8") as handle:
            handle.write("acquired")
        time.sleep({hold_seconds})
        lock.release()
        """
    )


def test_two_real_processes_contend_for_the_single_slot(tmp_path: Path) -> None:
    """Cross-process contention: process B must wait until process A releases."""

    state_dir = tmp_path / "cross_process_state"
    marker_a = tmp_path / "a_acquired.txt"
    marker_b = tmp_path / "b_acquired.txt"

    proc_a = subprocess.Popen(
        [sys.executable, "-c", _contender_script(state_dir, marker_a, 3.0)],
    )
    try:
        deadline = time.monotonic() + 10
        while not marker_a.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker_a.exists(), "process A never acquired the lock"

        proc_b = subprocess.Popen(
            [sys.executable, "-c", _contender_script(state_dir, marker_b, 0.1)],
        )
        try:
            # B must not have acquired the lock immediately -- A still holds it.
            time.sleep(0.5)
            assert not marker_b.exists(), "process B acquired the lock while A still held it"
            proc_b.wait(timeout=15)
            assert marker_b.exists(), "process B never acquired the lock after A released"
        finally:
            if proc_b.poll() is None:
                proc_b.kill()
    finally:
        proc_a.wait(timeout=15)
        if proc_a.poll() is None:
            proc_a.kill()
