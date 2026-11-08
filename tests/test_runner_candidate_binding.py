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
    (clean_git_repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
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
    assert "[DIRTY_PATHS] tracked.txt, untracked.py" in output
    assert "[HARNESS_FAIL]" in output
    assert "[HARNESS_RESULT]" not in output


def test_dirty_worktree_path_report_caps_at_twenty(clean_git_repository: Path) -> None:
    for index in range(22):
        (clean_git_repository / f"untracked-{index:02d}.txt").write_text("x", encoding="utf-8")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(runner, "PROJECT_ROOT", clean_git_repository)
    try:
        with pytest.raises(RuntimeError) as error:
            runner._ensure_clean_worktree()
    finally:
        monkeypatch.undo()

    path_line = next(line for line in str(error.value).splitlines() if line.startswith("[DIRTY_PATHS] "))
    assert path_line.count("untracked-") == 20
    assert path_line.endswith("+2")


def test_dirty_worktree_git_status_failure_fails_closed(
    clean_git_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "PROJECT_ROOT", clean_git_repository)

    def failed_git_status(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 128, "", "git status failed")

    monkeypatch.setattr(runner.subprocess, "run", failed_git_status)
    with pytest.raises(RuntimeError, match="Unable to verify candidate worktree cleanliness: git status failed"):
        runner._ensure_clean_worktree()


def test_execute_rejects_head_change_during_steps(
    clean_git_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(runner, "PROJECT_ROOT", clean_git_repository)
    candidate_shas = iter(("a" * 40, "b" * 40))
    monkeypatch.setattr(runner, "_candidate_sha", lambda: next(candidate_shas))
    monkeypatch.setattr(
        runner,
        "run_step",
        lambda step: runner.StepExecution(step.name, 0, 1, 1, 0),
    )
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
    assert "HEAD changed during harness execution" in output
    assert "[HARNESS_FAIL]" in output
    assert "[HARNESS_RESULT]" not in output
    assert "[HARNESS_PASS]" not in output
