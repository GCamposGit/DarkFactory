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


def test_acquire_and_release_handles_race_creating_lock_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "state" / "suite-0.lock"
    real_open = Path.open
    raced = False

    def _race_exclusive_create(self: Path, mode: str = "r", *args: object, **kwargs: object):
        nonlocal raced
        if self == lock_path and mode == "xb" and not raced:
            raced = True
            real_open(self, mode, *args, **kwargs).close()
            raise FileExistsError(self)
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _race_exclusive_create)
    lock = suite_lock.SuiteLock(timeout_sec=5)
    lock.acquire()
    try:
        directory = tmp_path / "state"
        assert raced is True
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


def test_timeout_raises_when_no_slot_frees_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    holder = suite_lock.SuiteLock(timeout_sec=5)
    holder.acquire()
    try:
        # Simulated time (USR-377): the waiter's backoff advances a fake monotonic clock instead of sleeping.
        import time as _real_time
        from types import SimpleNamespace

        fake = [0.0]
        proxy = SimpleNamespace(**{n: getattr(_real_time, n) for n in dir(_real_time) if not n.startswith("_")})
        proxy.monotonic = lambda: fake[0]
        monkeypatch.setattr(suite_lock, "time", proxy)
        monkeypatch.setattr(suite_lock, "_sleep", lambda seconds: fake.__setitem__(0, fake[0] + seconds))
        waiter = suite_lock.SuiteLock(timeout_sec=0.5)
        with pytest.raises(suite_lock.SuiteLockTimeout):
            waiter.acquire()
    finally:
        holder.release()


def test_probe_free_slot_true_when_no_holder(tmp_path: Path) -> None:
    assert suite_lock.probe_free_slot() is True


def test_probe_free_slot_false_when_slot_held(tmp_path: Path) -> None:
    holder = suite_lock.SuiteLock(timeout_sec=5)
    holder.acquire()
    try:
        assert suite_lock.probe_free_slot() is False
    finally:
        holder.release()


def test_probe_free_slot_respects_disabled_and_held_by_ancestor_flags() -> None:
    os.environ["DARKFAC_SUITE_LOCK"] = "off"
    assert suite_lock.probe_free_slot() is True
    del os.environ["DARKFAC_SUITE_LOCK"]
    os.environ["DARKFAC_SUITE_LOCK_HELD"] = "1"
    assert suite_lock.probe_free_slot() is False


def test_try_lock_file_raises_suite_lock_unwritable_on_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """USR-463: a restricted ACL (e.g. CodexSandboxUsers) denying write access
    to the lock file must surface as ``SuiteLockUnwritable``, never as a raw
    ``PermissionError`` traceback that kills the whole gate run."""

    lock_path = tmp_path / "suite-0.lock"
    lock_path.touch()
    real_open = Path.open
    attempts = 0

    def _denied_read(self: Path, mode: str = "r", *args: object, **kwargs: object):
        nonlocal attempts
        if self == lock_path and mode == "rb":
            attempts += 1
            raise PermissionError(13, "Permission denied")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _denied_read)
    with pytest.raises(suite_lock.SuiteLockUnwritable) as excinfo:
        suite_lock._try_lock_file(lock_path)
    assert excinfo.value.path == lock_path
    assert attempts == suite_lock._LOCK_PERMISSION_ATTEMPTS


def test_try_lock_file_still_returns_none_when_slot_is_busy(tmp_path: Path) -> None:
    """A slot genuinely held by another process stays a plain ``None`` --
    permission failures must not be conflated with lock contention."""

    lock_path = tmp_path / "suite-0.lock"
    holder_handle = suite_lock._try_lock_file(lock_path)
    assert holder_handle is not None
    try:
        assert suite_lock._try_lock_file(lock_path) is None
    finally:
        suite_lock._unlock_file(holder_handle)


def test_state_dir_raises_suite_lock_unwritable_on_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unwritable_dir = tmp_path / "state" / "nested"
    monkeypatch.setenv("DARKFAC_HARNESS_STATE_DIR", str(unwritable_dir))

    def _denied_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "mkdir", _denied_mkdir)
    with pytest.raises(suite_lock.SuiteLockUnwritable) as excinfo:
        suite_lock.state_dir()
    assert excinfo.value.path == unwritable_dir


def test_probe_free_slot_returns_false_instead_of_raising_when_state_dir_unwritable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unwritable(*args: object, **kwargs: object):
        raise suite_lock.SuiteLockUnwritable(Path("unused"), "denied")

    monkeypatch.setattr(suite_lock, "state_dir", _unwritable)
    assert suite_lock.probe_free_slot() is False


def test_resolve_state_dir_returns_canonical_when_writable(tmp_path: Path) -> None:
    resolution = suite_lock.resolve_state_dir()
    assert resolution.degraded is False
    assert resolution.reason is None
    assert resolution.path == tmp_path / "state"
    assert resolution.path.is_dir()


def test_resolve_state_dir_falls_back_per_identity_when_canonical_lock_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """USR-463: probing read access to the exact lock must detect a restricted
    identity even though the directory itself already exists and
    ``mkdir(exist_ok=True)`` is a silent no-op that proves nothing."""

    canonical = tmp_path / "state"
    canonical.mkdir(parents=True)
    (canonical / "suite-0.lock").touch()

    real_open = Path.open

    def _denied_read(self: Path, mode: str = "r", *args: object, **kwargs: object):
        if self == canonical / "suite-0.lock" and mode == "rb":
            raise PermissionError(13, "Permission denied")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _denied_read)
    monkeypatch.setenv("USERNAME", "sandboxed-user")
    resolution = suite_lock.resolve_state_dir()
    assert resolution.degraded is True
    assert resolution.reason is not None
    assert "suite-0.lock" in resolution.reason
    assert resolution.path != canonical
    assert "sandboxed-user" in resolution.path.name
    assert resolution.path.is_dir()


def test_acquire_existing_lock_read_only_without_changing_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing lock remains usable without writing or changing its mtime."""

    canonical = tmp_path / "state"
    canonical.mkdir(parents=True)
    lock_path = canonical / "suite-0.lock"
    lock_path.write_bytes(b"")
    before = lock_path.stat().st_mtime_ns
    real_open = Path.open
    opened_modes: list[str] = []

    def _observe_open(self: Path, mode: str = "r", *args: object, **kwargs: object):
        if self == lock_path:
            opened_modes.append(mode)
            assert mode == "rb"
        return real_open(self, mode, *args, **kwargs)

    def _deny_touch(self: Path, *args: object, **kwargs: object) -> None:
        if self == lock_path:
            raise AssertionError("an existing lock must not be touched")
        raise AssertionError("the state directory already exists")

    monkeypatch.setattr(Path, "open", _observe_open)
    monkeypatch.setattr(Path, "touch", _deny_touch)
    lock = suite_lock.SuiteLock(timeout_sec=5)
    lock.acquire()
    try:
        assert lock.degraded is False
        assert lock.state_dir_used == canonical
    finally:
        lock.release()

    assert opened_modes == ["rb", "rb"]
    assert lock_path.stat().st_mtime_ns == before


def test_acquire_degrades_if_canonical_lock_becomes_unwritable_after_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A permission change after resolution must still switch acquisition to fallback."""

    canonical = tmp_path / "state"
    real_try_lock_file = suite_lock._try_lock_file
    denied = False

    def _deny_acquisition_open(path: Path):
        nonlocal denied
        if path == canonical / "suite-0.lock" and not denied:
            denied = True
            raise suite_lock.SuiteLockUnwritable(path, "permission changed after probe")
        return real_try_lock_file(path)

    monkeypatch.setattr(suite_lock, "_try_lock_file", _deny_acquisition_open)
    monkeypatch.setenv("USERNAME", "sandboxed-user")

    lock = suite_lock.SuiteLock(timeout_sec=5)
    lock.acquire()
    try:
        fallback = canonical / "darkfac-harness-sandboxed-user"
        assert lock.held is True
        assert lock.degraded is True
        assert lock.state_dir_used == fallback
        assert lock._handle is not None and lock._handle.path.parent == fallback
    finally:
        lock.release()

    assert "estado degradado" in capsys.readouterr().out


def test_resolve_state_dir_fallback_rooted_at_configured_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``DARKFAC_HARNESS_STATE_DIR`` is configured, the per-identity
    fallback stays under that same configured base instead of jumping to the
    system temp dir, so it remains on the same volume as the canonical dir."""

    canonical = tmp_path / "state"
    canonical.mkdir(parents=True)

    real_open = Path.open

    def _denied_read(self: Path, mode: str = "r", *args: object, **kwargs: object):
        if self == canonical / "suite-0.lock" and mode == "rb":
            raise PermissionError(13, "Permission denied")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _denied_read)
    monkeypatch.setenv("USERNAME", "sandboxed-user")
    resolution = suite_lock.resolve_state_dir()
    assert resolution.degraded is True
    assert resolution.path.parent == canonical


def test_resolve_state_dir_uses_temp_fallback_when_configured_state_tree_is_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured state root may deny writes to the fallback subdirectory too.

    Resolution keeps the same-volume fallback as its first choice, then uses
    the process temp directory if that whole tree is restricted.
    """

    canonical = tmp_path / "state"
    fallback = canonical / "darkfac-harness-sandboxed-user"
    temp_root = tmp_path / "system-temp"
    canonical.mkdir(parents=True)
    fallback.mkdir(parents=True)
    (canonical / "suite-0.lock").touch()
    (fallback / "suite-0.lock").touch()
    monkeypatch.setenv("DARKFAC_HARNESS_STATE_DIR", str(canonical))
    monkeypatch.setenv("USERNAME", "sandboxed-user")
    monkeypatch.setattr(suite_lock.tempfile, "gettempdir", lambda: str(temp_root))

    real_open = Path.open

    def _deny_state_lock_open(self: Path, mode: str = "r", *args: object, **kwargs: object):
        if self in (canonical / "suite-0.lock", fallback / "suite-0.lock") and mode in ("rb", "xb"):
            raise PermissionError(13, "Permission denied")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _deny_state_lock_open)

    resolution = suite_lock.resolve_state_dir()

    assert resolution.degraded is True
    assert resolution.path == temp_root / "darkfac-harness-sandboxed-user"
    assert (resolution.path / "suite-0.lock").exists()

    def _deny_all_state_lock_open(self: Path, mode: str = "r", *args: object, **kwargs: object):
        if self.name == "suite-0.lock" and mode in ("rb", "xb"):
            raise PermissionError(13, "Permission denied")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _deny_all_state_lock_open)
    with pytest.raises(suite_lock.SuiteLockUnwritable) as excinfo:
        suite_lock.resolve_state_dir()
    assert excinfo.value.path == resolution.path / "suite-0.lock"


def test_acquire_stays_on_canonical_dir_with_no_warning_when_writable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """USR-463: the writable case must behave identically to before degraded
    mode existed -- no warning printed, no fallback directory created."""

    lock = suite_lock.SuiteLock(timeout_sec=5)
    lock.acquire()
    try:
        assert lock.degraded is False
        assert lock.degradation_reason is None
        assert lock.state_dir_used == tmp_path / "state"
    finally:
        lock.release()
    captured = capsys.readouterr()
    assert "[SUITE_LOCK]" not in captured.out
    assert not list(tmp_path.glob("darkfac-harness-*"))


def test_acquire_uses_degraded_fallback_when_canonical_sidecar_is_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """USR-463: a restricted identity must still be able to acquire the lock,
    in a writable fallback directory, with exactly one explanatory warning."""

    canonical = tmp_path / "state"
    canonical.mkdir(parents=True)
    canonical_lock = canonical / "suite-0.lock"
    canonical_lock.touch()
    canonical_sidecar_tmp = canonical / "suite-0.tmp"

    real_open = Path.open

    def _denied_sidecar_write(self: Path, mode: str = "r", *args: object, **kwargs: object):
        if self == canonical_sidecar_tmp and mode == "w":
            raise PermissionError(13, "Permission denied")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _denied_sidecar_write)
    monkeypatch.setenv("USERNAME", "sandboxed-user")

    lock = suite_lock.SuiteLock(timeout_sec=5)
    lock.acquire()
    try:
        assert lock.degraded is True
        assert lock.state_dir_used is not None
        assert lock.state_dir_used != canonical
        assert lock.held is True
    finally:
        lock.release()
    captured = capsys.readouterr()
    assert captured.out.count("[SUITE_LOCK]") == 1
    assert "best-effort" in captured.out or "best effort" in captured.out


def test_acquire_degraded_does_not_wait_for_dead_canonical_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canonical sidecar naming a pid that is not alive must never block the
    degraded acquisition (USR-463)."""

    canonical = tmp_path / "state"
    canonical.mkdir(parents=True)
    canonical_lock = canonical / "suite-0.lock"
    canonical_lock.touch()
    import json
    import socket

    (canonical / "suite-0.json").write_text(
        json.dumps(
            {
                "pid": 999999999,
                "hostname": socket.gethostname(),
                "started_at": time.time(),
            }
        ),
        encoding="utf-8",
    )

    real_open = Path.open

    def _denied_read(self: Path, mode: str = "r", *args: object, **kwargs: object):
        if self == canonical_lock and mode == "rb":
            raise PermissionError(13, "Permission denied")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _denied_read)
    monkeypatch.setenv("USERNAME", "sandboxed-user")

    lock = suite_lock.SuiteLock(timeout_sec=5)
    lock.acquire()
    try:
        assert lock.degraded is True
        assert lock.held is True
    finally:
        lock.release()


def test_acquire_degraded_waits_for_live_canonical_owner_then_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canonical sidecar naming *this* (confirmably alive) process must block
    the degraded acquisition until the configured deadline (USR-463)."""

    canonical = tmp_path / "state"
    canonical.mkdir(parents=True)
    canonical_lock = canonical / "suite-0.lock"
    canonical_lock.touch()
    import json
    import socket

    (canonical / "suite-0.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "started_at": time.time(),
            }
        ),
        encoding="utf-8",
    )

    real_open = Path.open

    def _denied_read(self: Path, mode: str = "r", *args: object, **kwargs: object):
        if self == canonical_lock and mode == "rb":
            raise PermissionError(13, "Permission denied")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _denied_read)
    monkeypatch.setenv("USERNAME", "sandboxed-user")
    monkeypatch.setattr(suite_lock, "_sleep", lambda _s: None)

    try:
        import psutil  # noqa: F401
    except ImportError:
        pytest.skip("psutil not installed")

    lock = suite_lock.SuiteLock(timeout_sec=0.2)
    with pytest.raises(suite_lock.SuiteLockTimeout) as excinfo:
        lock.acquire()
    assert excinfo.value.waited_sec >= 0.0
    assert lock.held is False


def test_probe_free_slot_skips_unwritable_slot_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single unwritable slot must not crash the probe; it is simply not
    counted as free (USR-463)."""

    real_try_lock_file = suite_lock._try_lock_file

    def _flaky(path: Path):
        raise suite_lock.SuiteLockUnwritable(path, "denied")

    monkeypatch.setattr(suite_lock, "_try_lock_file", _flaky)
    assert suite_lock.probe_free_slot() is False
    monkeypatch.setattr(suite_lock, "_try_lock_file", real_try_lock_file)


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
