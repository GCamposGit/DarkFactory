"""Tests for the exclusive line lock (`core/line/line_lock.py`, USR-250).

Root cause (2026-10-12): nothing stopped a second `run_ticket.py` launch -- even of the SAME ticket --
from starting while another was still running, so both ended up fighting over pytest, the Desktop test
worker and RAM until both timed out. These tests cover the four USR-250 acceptance scenarios directly
against the lock module: a second launch is refused on the spot (never queued), a dead owner's lock is
recovered automatically with a warning, a child process of the owning run inherits the lock instead of
refusing itself, and the lock is released after a failure (not just a clean exit).

Every test passes an explicit ``root=tmp_path`` so these never touch the real machine's line lock file.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest

from core.line import line_lock
from core.line.line_lock import LineLock, LineLockHeld, LineLockInfo, guard


@pytest.fixture(autouse=True)
def _no_inherited_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(line_lock.ENV_INHERITED, raising=False)


def _write(path: Path, info: LineLockInfo) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info.to_dict()), encoding="utf-8")


# A PID far above anything a real OS hands out on this box, so `_pid_alive` reliably reports it dead
# without the flakiness of spawning and racing a real subprocess's exit.
_IMPROBABLE_DEAD_PID = 999_999_999


# --------------------------------------------------------------------------
# Basic acquire/release
# --------------------------------------------------------------------------


def test_acquire_writes_the_sidecar_and_release_removes_it(tmp_path: Path) -> None:
    lock = LineLock("USR-01", root=tmp_path)
    lock.acquire()
    try:
        path = line_lock.lock_path(tmp_path)
        assert path.is_file()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["pid"] == os.getpid()
        assert data["ticket_id"] == "USR-01"
        assert lock.acquired is True
    finally:
        lock.release()
    assert not line_lock.lock_path(tmp_path).exists()
    assert lock.acquired is False


# --------------------------------------------------------------------------
# Scenario 1: a second launch is refused on the spot
# --------------------------------------------------------------------------


def test_second_launch_is_refused_immediately_with_holder_since_and_cancel_hint(tmp_path: Path) -> None:
    holder = LineLockInfo(
        pid=os.getpid(),  # this test process: genuinely alive
        ticket_id="USR-239", hostname="NOTEBOOK-GUI",
        started_at=1791796130.0,  # 2026-10-12 09:08:50 UTC
        heartbeat_at=time.time(),
    )
    _write(line_lock.lock_path(tmp_path), holder)

    second = LineLock("USR-240", root=tmp_path)
    started = time.monotonic()
    with pytest.raises(LineLockHeld) as excinfo:
        second.acquire()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "a second launch must be refused on the spot, never queued/waited on"
    assert second.acquired is False
    message = str(excinfo.value)
    assert "USR-239" in message  # who: the holder's ticket
    assert str(os.getpid()) in message and "NOTEBOOK-GUI" in message  # who: pid + host
    assert "2026-10-12" in message  # since when
    assert "--cancel USR-239" in message  # how to cancel
    # the held lock was never touched by the refused attempt
    assert json.loads(line_lock.lock_path(tmp_path).read_text(encoding="utf-8"))["ticket_id"] == "USR-239"


def test_second_launch_of_the_very_same_ticket_is_also_refused(tmp_path: Path) -> None:
    """The USR-239 incident: two launches of the SAME ticket raced each other just as badly."""
    holder = LineLockInfo(
        pid=os.getpid(), ticket_id="USR-239", hostname="NOTEBOOK-GUI",
        started_at=time.time(), heartbeat_at=time.time(),
    )
    _write(line_lock.lock_path(tmp_path), holder)

    with pytest.raises(LineLockHeld):
        LineLock("USR-239", root=tmp_path).acquire()


# --------------------------------------------------------------------------
# Scenario 2: a dead owner is recovered automatically, with a warning
# --------------------------------------------------------------------------


def test_a_lock_with_a_nonexistent_pid_is_recovered_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dead = LineLockInfo(
        pid=_IMPROBABLE_DEAD_PID, ticket_id="USR-239", hostname="NOTEBOOK-GUI",
        started_at=time.time() - 3600.0, heartbeat_at=time.time() - 3600.0,
    )
    _write(line_lock.lock_path(tmp_path), dead)

    with caplog.at_level(logging.WARNING, logger="core.line.line_lock"):
        lock = LineLock("USR-241", root=tmp_path)
        lock.acquire()
    try:
        assert lock.acquired is True
        assert "USR-239" in caplog.text  # names the recovered ticket
        assert "LINE_LOCK" in caplog.text
        data = json.loads(line_lock.lock_path(tmp_path).read_text(encoding="utf-8"))
        assert data["ticket_id"] == "USR-241" and data["pid"] == os.getpid()
    finally:
        lock.release()


def test_a_lock_with_an_expired_heartbeat_is_recovered_even_if_the_pid_is_alive_once_past_the_reuse_window(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A live-but-stuck holder (e.g. a hung process) is still recovered: PID liveness alone is not enough."""
    stuck = LineLockInfo(
        pid=os.getpid(),  # alive (it's this test process)
        ticket_id="USR-239", hostname="NOTEBOOK-GUI",
        started_at=time.time() - 7200.0,
        heartbeat_at=time.time() - (line_lock.HEARTBEAT_STALE_PID_ALIVE_SEC + 30.0),  # past the PID-reuse window
    )
    _write(line_lock.lock_path(tmp_path), stuck)

    with caplog.at_level(logging.WARNING, logger="core.line.line_lock"):
        lock = LineLock("USR-241", root=tmp_path)
        lock.acquire()
    try:
        assert lock.acquired is True
        assert "USR-239" in caplog.text
    finally:
        lock.release()


def test_a_fresh_heartbeat_with_an_alive_pid_is_not_recovered(tmp_path: Path) -> None:
    fresh = LineLockInfo(
        pid=os.getpid(), ticket_id="USR-239", hostname="NOTEBOOK-GUI",
        started_at=time.time() - 10.0, heartbeat_at=time.time() - 1.0,
    )
    _write(line_lock.lock_path(tmp_path), fresh)

    with pytest.raises(LineLockHeld):
        LineLock("USR-241", root=tmp_path).acquire()


def test_heartbeat_thread_refreshes_the_sidecar_while_held(tmp_path: Path) -> None:
    lock = LineLock("USR-01", root=tmp_path)
    original_interval = line_lock.HEARTBEAT_INTERVAL_SEC
    line_lock.HEARTBEAT_INTERVAL_SEC = 0.05
    try:
        lock.acquire()
        first = json.loads(line_lock.lock_path(tmp_path).read_text(encoding="utf-8"))["heartbeat_at"]
        deadline = time.monotonic() + 2.0
        refreshed = False
        while time.monotonic() < deadline:
            time.sleep(0.05)
            current = json.loads(line_lock.lock_path(tmp_path).read_text(encoding="utf-8"))["heartbeat_at"]
            if current > first:
                refreshed = True
                break
        assert refreshed, "the heartbeat on disk never advanced while the lock was held"
    finally:
        lock.release()
        line_lock.HEARTBEAT_INTERVAL_SEC = original_interval


# --------------------------------------------------------------------------
# Scenario 3: a child of the owning run inherits the lock instead of refusing
# --------------------------------------------------------------------------


def test_a_child_marked_as_inherited_never_refuses_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = LineLockInfo(
        pid=os.getpid(), ticket_id="USR-250", hostname="NOTEBOOK-GUI",
        started_at=time.time(), heartbeat_at=time.time(),
    )
    _write(line_lock.lock_path(tmp_path), owner)
    monkeypatch.setenv(line_lock.ENV_INHERITED, "1")

    child = LineLock("USR-250", root=tmp_path)
    child.acquire()  # must not raise, even though the lock is already held

    assert child.acquired is False
    assert child.inherited is True
    # the owner's lock is untouched: the child never wrote or deleted anything
    on_disk = json.loads(line_lock.lock_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk == owner.to_dict()

    child.release()
    # releasing an inherited lock is a no-op: the owner's lock survives the child's exit
    assert json.loads(line_lock.lock_path(tmp_path).read_text(encoding="utf-8")) == owner.to_dict()


def test_mark_inherited_sets_the_env_var_without_mutating_the_process_environ() -> None:
    env = line_lock.mark_inherited({"FOO": "bar"})
    assert env[line_lock.ENV_INHERITED] == "1"
    assert env["FOO"] == "bar"
    assert line_lock.ENV_INHERITED not in os.environ


def test_is_inherited_reflects_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    assert line_lock.is_inherited() is False
    monkeypatch.setenv(line_lock.ENV_INHERITED, "1")
    assert line_lock.is_inherited() is True


# --------------------------------------------------------------------------
# Scenario 4: the lock is released after a failure, not only on a clean exit
# --------------------------------------------------------------------------


def test_guard_releases_the_lock_even_when_the_body_raises(tmp_path: Path) -> None:
    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with guard("USR-01", root=tmp_path) as lock:
            assert lock.acquired is True
            raise _Boom("simulated gate/delivery failure")

    assert not line_lock.lock_path(tmp_path).exists()
    # a subsequent launch now succeeds immediately: nothing was left behind
    with guard("USR-02", root=tmp_path) as lock2:
        assert lock2.acquired is True


def test_release_is_idempotent_and_safe_without_a_prior_acquire(tmp_path: Path) -> None:
    lock = LineLock("USR-01", root=tmp_path)
    lock.release()  # never acquired: must not raise
    lock.acquire()
    lock.release()
    lock.release()  # already released: must not raise
    assert not line_lock.lock_path(tmp_path).exists()


# --------------------------------------------------------------------------
# Heartbeat vs PID liveness (suspended notebook must not lose its lock)
# --------------------------------------------------------------------------


def test_live_pid_with_a_moderately_old_heartbeat_is_refused(tmp_path: Path) -> None:
    held = LineLockInfo(
        pid=os.getpid(), ticket_id="USR-239", hostname="NOTEBOOK-GUI",
        started_at=time.time() - 600.0, heartbeat_at=time.time() - 120.0,  # > 90 s, but PID alive
    )
    _write(line_lock.lock_path(tmp_path), held)
    with pytest.raises(LineLockHeld):
        LineLock("USR-241", root=tmp_path).acquire()
    assert json.loads(line_lock.lock_path(tmp_path).read_text(encoding="utf-8"))["ticket_id"] == "USR-239"


def test_live_pid_with_a_heartbeat_older_than_the_reuse_window_is_recovered(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    reused = LineLockInfo(
        pid=os.getpid(), ticket_id="USR-239", hostname="NOTEBOOK-GUI",
        started_at=time.time() - 5000.0, heartbeat_at=time.time() - 2000.0,
    )
    _write(line_lock.lock_path(tmp_path), reused)
    with caplog.at_level(logging.WARNING, logger="core.line.line_lock"):
        lock = LineLock("USR-241", root=tmp_path)
        lock.acquire()
    try:
        assert lock.acquired is True
        assert "LINE_LOCK" in caplog.text and "USR-239" in caplog.text
    finally:
        lock.release()


# --------------------------------------------------------------------------
# Race on stale recovery: never delete a lock another process just created
# --------------------------------------------------------------------------


def _info(pid: int, ticket: str, started_at: float, heartbeat_at: float) -> LineLockInfo:
    return LineLockInfo(pid=pid, ticket_id=ticket, hostname="H", started_at=started_at, heartbeat_at=heartbeat_at)


def test_recover_stale_removes_the_lock_when_it_is_still_the_same_stale_owner(tmp_path: Path) -> None:
    path = line_lock.lock_path(tmp_path)
    stale = _info(_IMPROBABLE_DEAD_PID, "USR-1", 100.0, 100.0)
    _write(path, stale)
    assert line_lock._recover_stale(path, stale) is None
    assert not path.exists()
    assert not list(path.parent.glob("line.lock.stale-*"))


def test_recover_stale_restores_a_lock_another_process_created_in_between(tmp_path: Path) -> None:
    """Interleaving: B judged A's lock stale, but C replaced it with a live lock before B renamed it."""
    path = line_lock.lock_path(tmp_path)
    stale = _info(_IMPROBABLE_DEAD_PID, "USR-1", 100.0, 100.0)
    fresh = _info(os.getpid(), "USR-2", time.time(), time.time())
    _write(path, fresh)  # C already took the lock
    holder = line_lock._recover_stale(path, stale)  # B still believes `stale` is on disk
    assert holder is not None and holder.ticket_id == "USR-2"
    assert json.loads(path.read_text(encoding="utf-8"))["ticket_id"] == "USR-2"  # C's lock survived
    assert not list(path.parent.glob("line.lock.stale-*"))


def test_acquire_raises_held_when_recovery_finds_a_new_live_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = line_lock.lock_path(tmp_path)
    stale = _info(_IMPROBABLE_DEAD_PID, "USR-1", 100.0, 100.0)
    fresh = _info(os.getpid(), "USR-2", time.time(), time.time())
    _write(path, stale)
    real_read = line_lock._read_lock
    calls = {"n": 0}

    def read_then_swap(p: Path) -> LineLockInfo | None:
        result = real_read(p)
        calls["n"] += 1
        if calls["n"] == 1:  # right after B observed the stale lock, C swaps in a live one
            _write(path, fresh)
        return result

    monkeypatch.setattr(line_lock, "_read_lock", read_then_swap)
    with pytest.raises(LineLockHeld) as excinfo:
        LineLock("USR-3", root=tmp_path).acquire()
    assert excinfo.value.info.ticket_id == "USR-2"
    assert json.loads(path.read_text(encoding="utf-8"))["ticket_id"] == "USR-2"


def test_dead_pid_with_a_fresh_heartbeat_is_recovered_immediately(tmp_path: Path) -> None:
    dead = _info(_IMPROBABLE_DEAD_PID, "USR-239", time.time() - 60.0, time.time() - 5.0)
    _write(line_lock.lock_path(tmp_path), dead)
    lock = LineLock("USR-241", root=tmp_path)
    lock.acquire()
    try:
        assert lock.acquired is True
    finally:
        lock.release()
