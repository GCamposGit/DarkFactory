from pathlib import Path

from core.harness.suite_lock import _try_lock_file


def test_try_lock_file_handles_permission_error_when_touching_lock(
    tmp_path, monkeypatch
):
    lock_path = tmp_path / "suite-0.lock"

    def deny_touch(self, *, exist_ok=False):
        raise PermissionError("write access denied")

    monkeypatch.setattr(Path, "touch", deny_touch)

    assert _try_lock_file(lock_path) is False
