from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.orchestrator import guard


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def git_repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.name", "DarkFac Tests")
    _git(tmp_path, "config", "user.email", "darkfac-tests@example.invalid")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "--quiet", "-m", "seed")
    return tmp_path


def test_git_unavailable_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(guard.subprocess, "run", unavailable)

    with pytest.raises(guard.GitQueryError, match="unavailable"):
        guard.get_modified_files()


def test_cli_blocks_when_repository_state_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = guard.GitQueryError("git is unavailable", command=("git",))

    def fail_closed(base_ref: str = "HEAD") -> list[str]:
        raise error

    monkeypatch.setattr(guard, "get_modified_files", fail_closed)

    assert guard.main([]) == 1
    assert "[GUARD ERROR]" in capsys.readouterr().err


def test_invalid_base_ref_blocks(git_repository: Path) -> None:
    with pytest.raises(guard.GitQueryError, match="invalid base ref"):
        guard.get_modified_files("missing-ref", repository=git_repository)


def test_untracked_files_are_reported(git_repository: Path) -> None:
    untracked = git_repository / "core" / "harness" / "rogue.py"
    untracked.parent.mkdir(parents=True)
    untracked.write_text("print('untracked')\n", encoding="utf-8")

    modified = guard.get_modified_files(repository=git_repository)

    assert "core/harness/rogue.py" in modified
    assert guard.audit_paths(modified) == ["core/harness/rogue.py"]


def test_rename_reports_source_and_destination(git_repository: Path) -> None:
    protected = git_repository / "core" / "harness" / "gate.py"
    protected.parent.mkdir(parents=True)
    protected.write_text("GATE = True\n", encoding="utf-8")
    _git(git_repository, "add", "core/harness/gate.py")
    _git(git_repository, "commit", "--quiet", "-m", "add gate")
    (git_repository / "docs").mkdir()
    _git(git_repository, "mv", "core/harness/gate.py", "docs/gate.py")

    modified = guard.get_modified_files(repository=git_repository)

    assert "core/harness/gate.py" in modified
    assert "docs/gate.py" in modified
    assert guard.audit_paths(modified) == ["core/harness/gate.py"]


def test_diff_is_calculated_against_requested_base(git_repository: Path) -> None:
    base_sha = _git(git_repository, "rev-parse", "HEAD")
    changed = git_repository / "src" / "feature.py"
    changed.parent.mkdir()
    changed.write_text("VALUE = 1\n", encoding="utf-8")
    _git(git_repository, "add", "src/feature.py")
    _git(git_repository, "commit", "--quiet", "-m", "feature")

    assert guard.get_modified_files(repository=git_repository) == []
    assert guard.get_modified_files(base_sha, repository=git_repository) == [
        "src/feature.py"
    ]
