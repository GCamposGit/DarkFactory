from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.harness import runner
from core.harness.models import HarnessConfig, HarnessStepConfig


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def clean_git_repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.name", "DarkFac Tests")
    _git(tmp_path, "config", "user.email", "darkfac-tests@example.invalid")
    (tmp_path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "--quiet", "-m", "baseline")
    return tmp_path


def test_candidate_sha_rejects_untracked_worktree(
    clean_git_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (clean_git_repository / "untracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(runner, "PROJECT_ROOT", clean_git_repository)

    with pytest.raises(RuntimeError, match="worktree is dirty"):
        runner._candidate_sha()


def test_execute_rejects_dirty_worktree_before_running_steps(
    clean_git_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (clean_git_repository / "untracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(runner, "PROJECT_ROOT", clean_git_repository)

    def unexpected_step(step: HarnessStepConfig) -> runner.StepExecution:
        raise AssertionError(f"dirty worktree executed step: {step.name}")

    monkeypatch.setattr(runner, "run_step", unexpected_step)
    config = HarnessConfig(
        steps=[
            HarnessStepConfig(
                name="probe",
                cmd="python -c pass",
                quick=True,
                kind="check",
            )
        ]
    )

    assert (
        runner.execute(
            config,
            config_hash="a" * 64,
            quick=True,
            include_holdout=False,
            config_path=clean_git_repository / "harness.config.json",
        )
        is False
    )

    output = capsys.readouterr().out
    assert "Candidate worktree is dirty" in output
    assert "[HARNESS_FAIL]" in output
    assert "[HARNESS_RESULT]" not in output
