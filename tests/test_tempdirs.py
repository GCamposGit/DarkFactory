"""Explicit ownership and cleanup for probe sandboxes (USR-457)."""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path

import pytest

from core.common import tempdirs


def test_probe_sandbox_removes_its_directory_on_normal_exit() -> None:
    sandbox = tempdirs.probe_sandbox(prefix="usr457-normal-")
    with sandbox as directory:
        assert directory.is_dir()
        (directory / "scratch.txt").write_text("x", encoding="utf-8")
        captured = directory

    assert not captured.exists()
    assert sandbox.cleanup_result == tempdirs.SandboxCleanup(path=captured, ok=True)


def test_probe_sandbox_removes_directory_even_when_the_body_raises() -> None:
    sandbox = tempdirs.probe_sandbox(prefix="usr457-raise-")
    with pytest.raises(RuntimeError, match="boom"):
        with sandbox as directory:
            captured = directory
            raise RuntimeError("boom")
    assert not captured.exists()
    assert sandbox.cleanup_result is not None and sandbox.cleanup_result.ok


def test_registered_process_stops_before_one_explicit_cleanup_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Process:
        returncode = 0

        def poll(self) -> int:
            return self.returncode

    def remove(path: Path) -> None:
        events.append("remove")
        tempdirs.shutil.rmtree(path)

    monkeypatch.setattr(tempdirs, "robust_rmtree", remove)
    sandbox = tempdirs.probe_sandbox(prefix="usr457-order-")
    with sandbox as directory:
        sandbox.register_process(Process(), lambda process: events.append("stop"))
        first = sandbox.close()
        second = sandbox.close()

    assert first is second
    assert first.ok is True
    assert events == ["stop", "remove"]
    assert not directory.exists()


def test_failed_process_stop_preserves_directory_without_attempting_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LiveProcess:
        def poll(self) -> None:
            return None

    attempts: list[Path] = []
    monkeypatch.setattr(tempdirs, "robust_rmtree", lambda path: attempts.append(path))
    sandbox = tempdirs.probe_sandbox(prefix="usr457-live-child-")
    with sandbox as directory:
        marker = directory / "keep.txt"
        marker.write_text("child may still own this", encoding="utf-8")

        def stop(_process: LiveProcess) -> None:
            raise PermissionError("cannot stop child")

        sandbox.register_process(LiveProcess(), stop)
        result = sandbox.close()

    assert result.ok is False
    assert result.error_kind == "process_cleanup_failed"
    assert attempts == []
    assert marker.read_text(encoding="utf-8") == "child may still own this"
    monkeypatch.undo()
    tempdirs.robust_rmtree(directory)


def test_cleanup_failure_is_structured_logged_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def denied(path: Path) -> None:
        exc = PermissionError("[WinError 5] Access is denied")
        exc.winerror = 5  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(tempdirs, "robust_rmtree", denied)
    sandbox = tempdirs.probe_sandbox(prefix="usr457-denied-")
    with caplog.at_level(logging.WARNING, logger="darkfac.common.tempdirs"):
        with sandbox as directory:
            pass

    result = sandbox.cleanup_result
    assert result is not None
    assert result.ok is False
    assert result.error_kind == "PermissionError"
    assert result.winerror == 5
    assert str(directory) in result.detail
    assert "winerror=5" in caplog.text
    # This class has no __del__/weakref finalizer, and close() does not retry.
    assert sandbox.close() is result

    monkeypatch.undo()
    if directory.exists():
        tempdirs.robust_rmtree(directory)


def test_unowned_directory_is_not_removed_through_normalized_or_equivalent_paths() -> None:
    shared = Path(tempfile.mkdtemp(prefix="usr457-shared-"))
    marker = shared / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    sandbox = tempdirs.probe_sandbox(prefix="usr457-owned-")
    try:
        with sandbox as owned:
            assert tempdirs.remove_sandbox_dir(shared / ".", owner=sandbox).ok is False
            assert tempdirs.remove_sandbox_dir(owned / ".." / owned.name).ok is False
            assert marker.read_text(encoding="utf-8") == "keep"
            assert owned.is_dir()
    finally:
        if shared.exists():
            tempdirs.robust_rmtree(shared)


def test_replaced_owned_path_is_preserved() -> None:
    sandbox = tempdirs.probe_sandbox(prefix="usr457-replaced-")
    with sandbox as owned:
        original = owned.with_name(owned.name + "-original")
        owned.rename(original)
        owned.mkdir()
        marker = owned / "foreign.txt"
        marker.write_text("preserve", encoding="utf-8")

    result = sandbox.cleanup_result
    assert result is not None
    assert result.ok is False
    assert result.error_kind == "ownership_changed"
    assert marker.read_text(encoding="utf-8") == "preserve"
    tempdirs.robust_rmtree(owned)
    tempdirs.robust_rmtree(original)


def test_cleanup_refuses_registered_path_outside_the_current_temp_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = tempdirs.probe_sandbox(prefix="usr457-outside-")
    with sandbox as directory:
        fake_root = tmp_path / "fake-system-temp"
        fake_root.mkdir()
        monkeypatch.setattr(tempdirs.tempfile, "gettempdir", lambda: str(fake_root))
        result = sandbox.close()
    assert result.ok is False
    assert result.error_kind == "outside_tempdir"
    assert directory.is_dir()
    monkeypatch.undo()
    tempdirs.robust_rmtree(directory)


def test_remove_sandbox_dir_refuses_the_system_temp_root_itself() -> None:
    root = Path(tempfile.gettempdir()).resolve()
    marker = root / "usr457-root-keep.txt"
    marker.write_text("keep", encoding="utf-8")
    try:
        result = tempdirs.remove_sandbox_dir(root)
        assert result.ok is False
        assert result.error_kind == "unowned_directory"
        assert marker.read_text(encoding="utf-8") == "keep"
    finally:
        marker.unlink(missing_ok=True)


def test_remove_sandbox_dir_clears_a_read_only_file_like_robust_rmtree() -> None:
    sandbox = tempdirs.probe_sandbox(prefix="usr457-readonly-")
    with sandbox as directory:
        locked = directory / "locked.txt"
        locked.write_text("r", encoding="utf-8")
        os.chmod(locked, stat.S_IREAD)

    assert sandbox.cleanup_result is not None
    assert sandbox.cleanup_result.ok is True
    assert not directory.exists()
