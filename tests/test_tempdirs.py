"""Sandbox temp dir cleanup never raises from a finalizer (USR-457).

Production evidence: preflight returned codex with 42% quota headroom, but at
shutdown ``weakref.finalize`` inside ``tempfile.TemporaryDirectory._cleanup`` raised
``PermissionError [WinError 5]`` trying to ``os.chmod`` a ``darkfac-quota-probe``
directory left behind by a restricted-ACL sandbox subprocess. These tests cover the
replacement (:mod:`core.common.tempdirs`): no finalizer, a removal failure is logged
and returned structured instead of propagating, and cleanup never deletes outside
``tempfile.gettempdir()``.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path

import pytest

from core.common import tempdirs


def test_probe_sandbox_creates_via_mkdtemp_and_removes_on_normal_exit() -> None:
    with tempdirs.probe_sandbox(prefix="usr457-normal-") as directory:
        assert directory.is_dir()
        (directory / "scratch.txt").write_text("x", encoding="utf-8")
        captured = directory
    assert not captured.exists()


def test_probe_sandbox_removes_directory_even_when_the_body_raises() -> None:
    captured: Path
    with pytest.raises(RuntimeError, match="boom"):
        with tempdirs.probe_sandbox(prefix="usr457-raise-") as directory:
            captured = directory
            raise RuntimeError("boom")
    assert not captured.exists()


def test_probe_sandbox_cleanup_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A WinError 5 PermissionError on cleanup must not propagate out of the `with` block."""

    def _denied(path: Path) -> None:
        exc = PermissionError("[WinError 5] Access is denied")
        exc.winerror = 5  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(tempdirs, "robust_rmtree", _denied)

    with caplog.at_level(logging.WARNING, logger="darkfac.common.tempdirs"):
        with tempdirs.probe_sandbox(prefix="usr457-denied-") as directory:
            captured = directory

    assert str(captured) in caplog.text
    assert "winerror=5" in caplog.text


def test_remove_sandbox_dir_reports_structured_failure_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def _denied(path: Path) -> None:
        exc = PermissionError("[WinError 5] Access is denied")
        exc.winerror = 5  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(tempdirs, "robust_rmtree", _denied)
    directory = Path(tempfile.mkdtemp(prefix="usr457-struct-"))
    try:
        result = tempdirs.remove_sandbox_dir(directory)
    finally:
        monkeypatch.undo()
        if directory.exists():
            tempdirs.robust_rmtree(directory)

    assert result.ok is False
    assert result.error_kind == "PermissionError"
    assert result.winerror == 5
    assert str(directory) in result.detail


def test_remove_sandbox_dir_refuses_a_path_outside_the_system_tempdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pytest's own tmp_path lives under the real system tempdir, so the system tempdir
    # itself is faked here to get a path that is genuinely outside it.
    fake_system_temp = tmp_path / "fake-system-temp"
    fake_system_temp.mkdir()
    monkeypatch.setattr(tempdirs.tempfile, "gettempdir", lambda: str(fake_system_temp))

    outside = tmp_path / "not-a-sandbox"
    outside.mkdir()

    result = tempdirs.remove_sandbox_dir(outside)

    assert result.ok is False
    assert result.error_kind == "outside_tempdir"
    assert outside.is_dir()  # left untouched


def test_remove_sandbox_dir_refuses_the_system_temp_root_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(tempfile.mkdtemp(prefix="usr457-root-"))
    marker = root / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(tempdirs.tempfile, "gettempdir", lambda: str(root))

    result = tempdirs.remove_sandbox_dir(root)

    assert result.ok is False
    assert result.error_kind == "outside_tempdir"
    assert marker.read_text(encoding="utf-8") == "keep"
    # Restore the real temp root for the test's own cleanup.
    monkeypatch.undo()
    tempdirs.robust_rmtree(root)


def test_remove_sandbox_dir_on_an_already_gone_directory_is_ok() -> None:
    directory = Path(tempfile.mkdtemp(prefix="usr457-gone-"))
    directory.rmdir()

    result = tempdirs.remove_sandbox_dir(directory)

    assert result.ok is True


def test_remove_sandbox_dir_clears_a_read_only_file_like_robust_rmtree() -> None:
    directory = Path(tempfile.mkdtemp(prefix="usr457-readonly-"))
    locked = directory / "locked.txt"
    locked.write_text("r", encoding="utf-8")
    os.chmod(locked, stat.S_IREAD)

    result = tempdirs.remove_sandbox_dir(directory)

    assert result.ok is True
    assert not directory.exists()
