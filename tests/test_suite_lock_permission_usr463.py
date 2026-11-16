from pathlib import Path

from core.harness import suite_lock


def test_try_lock_file_handles_permission_error_when_touching_lock(
    tmp_path, monkeypatch
):
    def deny_touch(self, *args, **kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr(Path, "touch", deny_touch)

    assert suite_lock._try_lock_file(tmp_path / "suite-0.lock") is False
