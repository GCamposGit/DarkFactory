"""Focal tests for worktree hygiene (USR-159).

Every repository is a temporary one under ``tmp_path`` with real ``git worktree`` checkouts;
no network, no real ``gh`` (the PR lookup is injected) and nothing touches the repository
that runs the suite.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

import pytest

from core.git import ticket_workspace, worktree_gc
from core.git.worktree_gc import (
    ACTIVE,
    LOCKED,
    MERGED,
    OUT_OF_SCOPE,
    UNMERGED,
    PrInfo,
    run_gc,
)


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    )
    return res.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name + "\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "--quiet", "-m", f"feat: {name}")
    return _git(repo, "rev-parse", "HEAD")


def _later() -> datetime:
    """A clock one day ahead, so freshly created worktrees are not 'recently modified'."""
    return datetime.now(timezone.utc) + timedelta(days=1)


@pytest.fixture
def repo(tmp_path: Path) -> SimpleNamespace:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "--quiet", "-b", "main")
    local = tmp_path / "local"
    local.mkdir()
    _git(local, "init", "--quiet", "-b", "main")
    _git(local, "config", "user.name", "DarkFac Test")
    _git(local, "config", "user.email", "test@darkfac.internal")
    _git(local, "config", "commit.gpgsign", "false")
    (local / "README.md").write_text("base\n", encoding="utf-8")
    _git(local, "add", "README.md")
    _git(local, "commit", "--quiet", "-m", "chore: initial")
    _git(local, "remote", "add", "origin", str(bare))
    _git(local, "push", "--quiet", "-u", "origin", "main")
    return SimpleNamespace(local=local, tmp=tmp_path)


def _add_worktree(repo: SimpleNamespace, branch: str, *, root: str = ".worktrees", commit: bool = True) -> Path:
    path = repo.local / root / branch.replace("/", "-")
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo.local, "worktree", "add", "--quiet", "-b", branch, str(path), "origin/main")
    _git(path, "config", "user.name", "DarkFac Test")
    _git(path, "config", "user.email", "test@darkfac.internal")
    _git(path, "config", "commit.gpgsign", "false")
    if commit:
        _commit(path, f"{branch.replace('/', '-')}.txt")
    return path


def _merge_into_main(repo: SimpleNamespace, branch: str) -> None:
    """Integrate ``branch`` into main and publish it, like a merged PR."""
    _git(repo.local, "merge", "--quiet", "--no-ff", "-m", f"merge {branch}", branch)
    _git(repo.local, "push", "--quiet", "origin", "main")


def _registered(repo: SimpleNamespace) -> set[str]:
    return {
        os.path.normcase(os.path.realpath(e["worktree"])) for e in ticket_workspace.list_worktrees(repo.local)
    }


def _is_registered(repo: SimpleNamespace, path: Path) -> bool:
    return os.path.normcase(os.path.realpath(str(path))) in _registered(repo)


def _branches(repo: SimpleNamespace) -> set[str]:
    return set(_git(repo.local, "branch", "--format=%(refname:short)").splitlines())


def _state(report: worktree_gc.GcReport, path: Path) -> worktree_gc.WorktreeEntry:
    target = os.path.normcase(os.path.realpath(str(path)))
    for entry in report.entries:
        if os.path.normcase(os.path.realpath(entry.path)) == target:
            return entry
    raise AssertionError(f"{path} not in report: {[e.path for e in report.entries]}")


class FlakyGit:
    """git runner whose first ``worktree remove`` calls fail like a Windows file lock."""

    def __init__(self, fail_first: int) -> None:
        self.fail_first = fail_first
        self.remove_calls = 0

    def __call__(self, args: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
        if list(args[:2]) == ["worktree", "remove"]:
            self.remove_calls += 1
            if self.remove_calls <= self.fail_first:
                return subprocess.CompletedProcess(
                    ["git", *args], 1, "", "error: failed to delete 'x': Directory not empty"
                )
        return ticket_workspace._git(args, cwd)


# ---------------------------------------------------------------------------
# Classification and removal
# ---------------------------------------------------------------------------


def test_merged_clean_worktree_is_removed_with_its_branch(repo: SimpleNamespace) -> None:
    path = _add_worktree(repo, "feat/done")
    _merge_into_main(repo, "feat/done")

    report = run_gc(repo.local, apply=True, now_fn=_later)

    entry = _state(report, path)
    assert entry.state == MERGED and entry.removed and entry.branch_deleted
    assert not path.exists()
    assert not _is_registered(repo, path)
    assert "feat/done" not in _branches(repo)
    assert report.pruned and not report.failed


def test_squash_merged_branch_is_recognised_through_the_pr(repo: SimpleNamespace) -> None:
    path = _add_worktree(repo, "feat/squashed")
    head = _git(path, "rev-parse", "HEAD")
    lookup = lambda branch, cwd: [PrInfo(7, "MERGED", head)]  # noqa: E731

    report = run_gc(repo.local, apply=True, pr_lookup=lookup, now_fn=_later)

    assert _state(report, path).removed
    assert "feat/squashed" not in _branches(repo)


def test_worktree_with_open_pr_is_active_and_kept(repo: SimpleNamespace) -> None:
    path = _add_worktree(repo, "feat/open")
    lookup = lambda branch, cwd: [PrInfo(9, "OPEN", "")]  # noqa: E731

    report = run_gc(repo.local, apply=True, pr_lookup=lookup, now_fn=_later)

    entry = _state(report, path)
    assert entry.state == ACTIVE and "open PR #9" in entry.reason
    assert path.exists() and "feat/open" in _branches(repo)


def test_open_pr_wins_even_when_head_is_already_in_main(repo: SimpleNamespace) -> None:
    path = _add_worktree(repo, "feat/reopened")
    _merge_into_main(repo, "feat/reopened")
    lookup = lambda branch, cwd: [PrInfo(3, "OPEN", "")]  # noqa: E731

    report = run_gc(repo.local, apply=True, pr_lookup=lookup, now_fn=_later)

    assert _state(report, path).state == ACTIVE and path.exists()


def test_locked_worktree_is_kept_and_reported(repo: SimpleNamespace) -> None:
    path = _add_worktree(repo, "feat/locked")
    _merge_into_main(repo, "feat/locked")
    _git(repo.local, "worktree", "lock", "--reason", "agent session 42", str(path))

    report = run_gc(repo.local, apply=True, now_fn=_later)

    entry = _state(report, path)
    assert entry.state == LOCKED and "agent session 42" in entry.reason
    assert path.exists() and _is_registered(repo, path) and "feat/locked" in _branches(repo)


@pytest.mark.parametrize("kind", ["untracked", "modified"])
def test_worktree_with_local_changes_is_kept(repo: SimpleNamespace, kind: str) -> None:
    path = _add_worktree(repo, "feat/dirty")
    _merge_into_main(repo, "feat/dirty")
    if kind == "untracked":
        (path / "scratch.txt").write_text("wip\n", encoding="utf-8")
    else:
        (path / "README.md").write_text("changed\n", encoding="utf-8")

    report = run_gc(repo.local, apply=True, now_fn=_later)

    entry = _state(report, path)
    assert entry.state == ACTIVE and "uncommitted" in entry.reason
    assert path.exists() and "feat/dirty" in _branches(repo)


def test_recently_modified_worktree_is_kept(repo: SimpleNamespace) -> None:
    path = _add_worktree(repo, "feat/fresh")
    _merge_into_main(repo, "feat/fresh")

    report = run_gc(repo.local, apply=True)  # real clock: it was just created

    assert _state(report, path).state == ACTIVE and path.exists()


def test_run_in_progress_probe_keeps_worktree(repo: SimpleNamespace) -> None:
    path = _add_worktree(repo, "feat/running")
    _merge_into_main(repo, "feat/running")

    report = run_gc(repo.local, apply=True, run_probe=lambda p: True, now_fn=_later)

    assert _state(report, path).reason == "run in progress" and path.exists()


@pytest.mark.parametrize(
    "lookup",
    [
        lambda branch, cwd: None,  # gh unavailable
        lambda branch, cwd: [],  # no PR at all
        lambda branch, cwd: [PrInfo(5, "MERGED", "0" * 40)],  # merged, but local HEAD differs
        lambda branch, cwd: (_ for _ in ()).throw(RuntimeError("boom")),
    ],
    ids=["unknown", "no-pr", "sha-mismatch", "raises"],
)
def test_unproven_integration_fails_closed(repo: SimpleNamespace, lookup) -> None:
    path = _add_worktree(repo, "feat/unmerged")

    report = run_gc(repo.local, apply=True, pr_lookup=lookup, now_fn=_later)

    assert _state(report, path).state == UNMERGED
    assert path.exists() and "feat/unmerged" in _branches(repo)


def test_missing_base_ref_fails_closed(repo: SimpleNamespace) -> None:
    path = _add_worktree(repo, "feat/nobase")
    _merge_into_main(repo, "feat/nobase")

    report = run_gc(repo.local, apply=True, base_ref="origin/does-not-exist", now_fn=_later)

    assert _state(report, path).state == UNMERGED and path.exists()


# ---------------------------------------------------------------------------
# Retry, dry-run and scope
# ---------------------------------------------------------------------------


def test_removal_is_retried_with_backoff_when_first_attempts_fail(repo: SimpleNamespace) -> None:
    path = _add_worktree(repo, "feat/flaky")
    _merge_into_main(repo, "feat/flaky")
    flaky, sleeps = FlakyGit(fail_first=2), []

    report = run_gc(repo.local, apply=True, run_git=flaky, sleep_fn=sleeps.append, now_fn=_later)

    entry = _state(report, path)
    assert entry.removed and entry.method == "git" and entry.attempts == 3
    assert flaky.remove_calls == 3 and sleeps == [0.5, 1.0]
    assert not path.exists() and "feat/flaky" not in _branches(repo)


def test_persistent_removal_failure_is_reported_without_stopping_the_sweep(repo: SimpleNamespace) -> None:
    stuck = _add_worktree(repo, "feat/stuck")
    other = _add_worktree(repo, "feat/other")
    _merge_into_main(repo, "feat/stuck")
    _merge_into_main(repo, "feat/other")

    class Stuck:
        def __call__(self, args: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
            if list(args[:2]) == ["worktree", "remove"] and "feat-stuck" in args[-1]:
                return subprocess.CompletedProcess(["git", *args], 1, "", "Directory not empty")
            return ticket_workspace._git(args, cwd)

    def locked_rmtree(_path: Path) -> None:
        raise PermissionError("in use")

    def no_trash(*_a: object, **_k: object) -> Path:
        raise OSError("cannot move")

    original = ticket_workspace.trash_directory
    ticket_workspace.trash_directory = no_trash  # type: ignore[assignment]
    try:
        report = run_gc(
            repo.local, apply=True, run_git=Stuck(), sleep_fn=lambda s: None, rmtree_fn=locked_rmtree,
            now_fn=_later,
        )
    finally:
        ticket_workspace.trash_directory = original  # type: ignore[assignment]

    assert not _state(report, stuck).removed and stuck.exists()
    assert "feat/stuck" in _branches(repo)  # branch kept while its worktree could not be removed
    assert _state(report, other).removed
    assert [Path(p).name for p in report.to_dict()["failed"]] == [stuck.name]


def test_dry_run_changes_nothing(repo: SimpleNamespace) -> None:
    path = _add_worktree(repo, "feat/dry")
    _merge_into_main(repo, "feat/dry")

    report = run_gc(repo.local, now_fn=_later)  # apply defaults to False

    entry = _state(report, path)
    assert entry.state == MERGED and entry.action == "remove" and not entry.removed
    assert path.exists() and _is_registered(repo, path) and "feat/dry" in _branches(repo)
    assert not report.pruned and not report.failed


def test_worktrees_outside_roots_and_the_main_checkout_are_never_touched(repo: SimpleNamespace) -> None:
    inside = _add_worktree(repo, "feat/inside")
    outside = repo.tmp / "elsewhere" / "feat-outside"
    outside.parent.mkdir()
    _git(repo.local, "worktree", "add", "--quiet", "-b", "feat/outside", str(outside), "origin/main")
    _merge_into_main(repo, "feat/inside")
    _merge_into_main(repo, "feat/outside")

    report = run_gc(repo.local, apply=True, now_fn=_later)

    assert _state(report, outside).state == OUT_OF_SCOPE
    assert outside.exists() and _is_registered(repo, outside) and "feat/outside" in _branches(repo)
    assert _state(report, inside).removed
    assert all(os.path.realpath(e.path) != os.path.realpath(str(repo.local)) for e in report.entries)
    assert repo.local.exists() and "main" in _branches(repo)


def test_custom_root_brings_a_foreign_directory_into_scope(repo: SimpleNamespace) -> None:
    foreign = repo.tmp / "elsewhere" / "feat-foreign"
    foreign.parent.mkdir()
    _git(repo.local, "worktree", "add", "--quiet", "-b", "feat/foreign", str(foreign), "origin/main")

    report = run_gc(repo.local, apply=True, roots=[foreign.parent], now_fn=_later)

    assert _state(report, foreign).removed  # HEAD == origin/main: already integrated


def test_worktree_that_is_the_current_directory_is_kept(repo: SimpleNamespace, monkeypatch) -> None:
    path = _add_worktree(repo, "feat/cwd")
    _merge_into_main(repo, "feat/cwd")
    monkeypatch.chdir(path)

    report = run_gc(repo.local, apply=True, now_fn=_later)

    assert _state(report, path).state == ACTIVE and path.exists()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_is_dry_run_by_default_and_emits_json(repo: SimpleNamespace, capsys, monkeypatch) -> None:
    path = _add_worktree(repo, "feat/cli")
    _merge_into_main(repo, "feat/cli")
    monkeypatch.setattr(ticket_workspace, "utc_now", _later)

    code = worktree_gc.main(["--repo", str(repo.local), "--no-gh", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["apply"] is False
    assert payload["counts"] == {MERGED: 1} and payload["removed"] == []
    assert path.exists()

    code = worktree_gc.main(["--repo", str(repo.local), "--no-gh", "--apply", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["apply"] is True and len(payload["removed"]) == 1
    assert not path.exists()


def test_cli_rejects_apply_with_dry_run() -> None:
    with pytest.raises(SystemExit):
        worktree_gc.main(["--apply", "--dry-run"])
