"""Tests for per-ticket worktrees and reliable cleanup (USR-69).

Root cause: ``run_ticket`` ran the agent in the SHARED checkout and ``commit_ticket`` did
``git add -A`` there, so two simultaneous sessions swept each other's files into one commit
(USR-60/USR-64); on Windows ``git worktree remove`` also left dead directories behind.

Every repository here is a temporary one under ``tmp_path``: nothing touches the checkout that runs
the suite, and nothing here ever sweeps the real ``.worktrees`` directories.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

import pytest

import run_ticket
from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.git import autonomy, ticket_workspace
from core.git.autonomy import GitAutonomyManager
from core.git.ticket_workspace import (
    TicketWorkspace,
    WorkspaceError,
    cleanup,
    create_workspace,
    ensure_local_excludes,
    find_orphan_dirs,
    purge_trash,
    shared_checkout_dirty_paths,
)
from tests.test_git_delivery import FakeGh

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
STAMP = "20260930T120000Z"


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    )
    return res.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> SimpleNamespace:
    """A bare origin plus a clone on main (no .gitignore: the workspace must protect itself)."""
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
    return SimpleNamespace(local=local, bare=bare, tmp=tmp_path)


def _registered(local: Path) -> list[str]:
    return [
        os.path.normcase(os.path.realpath(entry["worktree"]))
        for entry in ticket_workspace.list_worktrees(local)
    ]


def _locked_rmtree(_path: Path) -> None:
    raise PermissionError("[WinError 32] the process cannot access the file because it is in use")


class FlakyGit:
    """`git` runner whose ``worktree remove`` fails (a locked file) before delegating to real git."""

    def __init__(self, fail_first: int = 0, always: bool = False) -> None:
        self.fail_first = fail_first
        self.always = always
        self.remove_calls = 0

    def __call__(self, args: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
        if list(args[:2]) == ["worktree", "remove"]:
            self.remove_calls += 1
            if self.always or self.remove_calls <= self.fail_first:
                return subprocess.CompletedProcess(
                    ["git", *args], 1, "", "error: failed to delete: Permission denied (file in use)"
                )
        return ticket_workspace._git(args, cwd)


# ---------------------------------------------------------------------------
# Creation and isolation
# ---------------------------------------------------------------------------


def test_two_simultaneous_tickets_get_isolated_worktrees_and_commits(repo: SimpleNamespace) -> None:
    local = repo.local
    ws_a = create_workspace("USR-201", cwd=local, now_fn=lambda: NOW)
    ws_b = create_workspace("USR-202", cwd=local, now_fn=lambda: NOW)

    assert ws_a.path != ws_b.path
    assert ws_a.path.parent == ws_b.path.parent == (local / ".worktrees")
    assert ws_a.path.name == f"usr-201-{STAMP}" and ws_b.path.name == f"usr-202-{STAMP}"
    assert (ws_a.branch, ws_b.branch) == ("ticket/usr-201", "ticket/usr-202")
    assert ws_a.base_ref == "origin/main" and ws_a.base_sha == _git(local, "rev-parse", "origin/main")

    # Three sessions edit at the same time: A, B and somebody in the shared checkout
    (ws_a.path / "a.txt").write_text("a\n", encoding="utf-8")
    (ws_b.path / "b.txt").write_text("b\n", encoding="utf-8")
    (local / "shared.txt").write_text("someone else's work\n", encoding="utf-8")

    mgr = GitAutonomyManager(local)
    mgr.commit_ticket("USR-201", "Feature A", cwd=ws_a.path)
    mgr.commit_ticket("USR-202", "Feature B", cwd=ws_b.path)

    assert _git(ws_a.path, "show", "--name-only", "--format=", "HEAD").splitlines() == ["a.txt"]
    assert _git(ws_b.path, "show", "--name-only", "--format=", "HEAD").splitlines() == ["b.txt"]
    assert _git(ws_a.path, "log", "-1", "--format=%s") == "feat(core): Feature A [USR-201]"
    # The shared checkout was never swept into either commit and still holds its own file
    assert _git(local, "status", "--porcelain") == "?? shared.txt"
    assert not (ws_a.path / "shared.txt").exists() and not (ws_b.path / "shared.txt").exists()
    assert not (ws_a.path / "b.txt").exists() and not (ws_b.path / "a.txt").exists()


def test_a_worktree_name_is_never_reused_and_the_branch_stays_unique(repo: SimpleNamespace) -> None:
    first = create_workspace("USR-203", cwd=repo.local, now_fn=lambda: NOW)
    second = create_workspace("USR-203", cwd=repo.local, now_fn=lambda: NOW)  # same ticket, same second
    assert second.path != first.path and second.path.name == f"usr-203-{STAMP}-2"
    assert first.branch == "ticket/usr-203"
    assert second.branch != first.branch and second.branch.startswith("ticket/usr-203-")
    assert first.path.exists() and second.path.exists()


def test_queue_labels_get_a_timestamped_branch(repo: SimpleNamespace) -> None:
    ws = create_workspace("queue", cwd=repo.local, unique_branch=True, now_fn=lambda: NOW)
    assert ws.branch == f"ticket/queue-{STAMP}"


def test_creation_keeps_worktrees_out_of_git_even_without_a_gitignore(repo: SimpleNamespace) -> None:
    create_workspace("USR-204", cwd=repo.local)
    assert _git(repo.local, "status", "--porcelain") == ""  # `.worktrees/` is excluded locally
    ensure_local_excludes(repo.local)  # idempotent
    exclude = (repo.local / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert exclude.count("/.worktrees/") == 1


def test_creation_works_from_inside_another_worktree(repo: SimpleNamespace) -> None:
    ws_a = create_workspace("USR-205", cwd=repo.local)
    ws_b = create_workspace("USR-206", cwd=ws_a.path)
    assert ws_b.main_root == ws_a.main_root
    assert ws_b.path.parent == (repo.local / ".worktrees")  # always under the MAIN checkout


def test_creation_fails_clearly_without_a_remote(tmp_path: Path) -> None:
    lone = tmp_path / "lone"
    lone.mkdir()
    _git(lone, "init", "--quiet", "-b", "main")
    with pytest.raises(WorkspaceError, match="fetch origin main failed"):
        create_workspace("USR-207", cwd=lone)


def test_commit_in_a_worktree_stages_explicit_paths_never_add_all(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = create_workspace("USR-208", cwd=repo.local)
    (ws.path / "one.txt").write_text("1\n", encoding="utf-8")
    (ws.path / "dir").mkdir()
    (ws.path / "dir" / "two[1].txt").write_text("2\n", encoding="utf-8")  # glob characters stay literal
    (ws.path / "README.md").unlink()  # deletions are staged too

    calls: list[list[str]] = []
    real = autonomy._run_git

    def spy(args: Sequence[str], *a: object, **kw: object) -> "subprocess.CompletedProcess[str]":
        calls.append(list(args))
        return real(args, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(autonomy, "_run_git", spy)
    GitAutonomyManager(repo.local).commit_ticket("USR-208", "Estagiar so o necessario", cwd=ws.path)

    adds = [c for c in calls if "add" in c[:2]]
    assert adds, "nothing was staged"
    for call in adds:
        assert "--" in call and call[call.index("--") + 1 :], f"bare add without explicit paths: {call}"
    changed = sorted(_git(ws.path, "show", "--name-only", "--format=", "HEAD").splitlines())
    assert changed == ["README.md", "dir/two[1].txt", "one.txt"]


# ---------------------------------------------------------------------------
# Cleanup: retry, robust rmtree, recoverable trash
# ---------------------------------------------------------------------------


def test_cleanup_retries_a_transient_failure_with_backoff(repo: SimpleNamespace) -> None:
    ws = create_workspace("USR-210", cwd=repo.local)
    (ws.path / "work.txt").write_text("w\n", encoding="utf-8")
    flaky, sleeps = FlakyGit(fail_first=2), []

    result = cleanup(ws.path, ws.main_root, sleep_fn=sleeps.append, run_git=flaky)

    assert result.removed and result.method == "git" and result.attempts == 3
    assert sleeps == [0.5, 1.0]  # backoff between the failed attempts, never after the last one
    assert not ws.path.exists()
    assert os.path.normcase(os.path.realpath(ws.path)) not in _registered(repo.local)


def test_cleanup_falls_back_to_rmtree_and_clears_read_only_files(repo: SimpleNamespace) -> None:
    ws = create_workspace("USR-211", cwd=repo.local)
    locked = ws.path / "readonly.txt"
    locked.write_text("r\n", encoding="utf-8")
    os.chmod(locked, 0o444)

    result = cleanup(ws.path, ws.main_root, sleep_fn=lambda _s: None, run_git=FlakyGit(always=True), retries=2)

    assert result.removed and result.method == "rmtree"
    assert not ws.path.exists()
    assert os.path.normcase(os.path.realpath(ws.path)) not in _registered(repo.local)  # pruned


def test_cleanup_moves_an_undeletable_worktree_to_the_recoverable_trash(repo: SimpleNamespace) -> None:
    ws = create_workspace("USR-212", cwd=repo.local)
    (ws.path / "precious.txt").write_text("keep me\n", encoding="utf-8")

    result = cleanup(
        ws.path, ws.main_root, sleep_fn=lambda _s: None, run_git=FlakyGit(always=True),
        rmtree_fn=_locked_rmtree, now_fn=lambda: NOW,
    )

    expected = repo.local / ".factory" / "backups" / "trash" / f"{ws.path.name}-{STAMP}"
    assert result.removed and result.method == "trash" and result.trash_path == expected
    assert not ws.path.exists()
    assert (expected / "precious.txt").read_text(encoding="utf-8") == "keep me\n"  # recoverable
    assert os.path.normcase(os.path.realpath(ws.path)) not in _registered(repo.local)
    assert _git(repo.local, "status", "--porcelain") == ""  # the trash is excluded too


def test_cleanup_reports_failure_when_even_the_rename_fails(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = create_workspace("USR-213", cwd=repo.local)

    def refuse(path: Path, main_root: Path, **_kw: object) -> Path:
        raise PermissionError("rename blocked")

    monkeypatch.setattr(ticket_workspace, "trash_directory", refuse)
    result = cleanup(
        ws.path, ws.main_root, sleep_fn=lambda _s: None, run_git=FlakyGit(always=True), rmtree_fn=_locked_rmtree
    )
    assert not result.removed and result.method == "failed"
    assert ws.path.exists() and "rename blocked" in "; ".join(result.errors)  # nothing was lost


def test_cleanup_never_touches_a_locked_worktree(repo: SimpleNamespace) -> None:
    ws = create_workspace("USR-214", cwd=repo.local)
    _git(repo.local, "worktree", "lock", "--reason", "agent running", str(ws.path))
    sleeps: list[float] = []

    result = cleanup(ws.path, ws.main_root, sleep_fn=sleeps.append)

    assert not result.removed and "locked" in result.detail
    assert ws.path.exists() and sleeps == []


def test_cleanup_of_an_absent_directory_just_prunes(repo: SimpleNamespace) -> None:
    ws = create_workspace("USR-215", cwd=repo.local)
    ticket_workspace.robust_rmtree(ws.path)  # the directory vanished behind git's back
    result = cleanup(ws.path, ws.main_root)
    assert result.removed and result.method == "absent"
    assert os.path.normcase(os.path.realpath(ws.path)) not in _registered(repo.local)


def test_delivery_cleanup_uses_the_robust_removal(repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """`deliver_branch` removes the ticket worktree through cleanup(): a transient lock is retried."""
    gh = FakeGh(repo.bare)
    ws = create_workspace("USR-216", cwd=repo.local)
    (ws.path / "w.txt").write_text("w\n", encoding="utf-8")
    sleeps: list[float] = []
    mgr = GitAutonomyManager(repo.local, cleanup_sleep_fn=sleeps.append, now_fn=lambda: NOW)

    real_cleanup = ticket_workspace.cleanup

    def stubborn(path: Path, main_root: Path, **kwargs: object) -> object:
        kwargs["run_git"] = FlakyGit(fail_first=1)  # one transient lock, then git succeeds
        return real_cleanup(path, main_root, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ticket_workspace, "cleanup", stubborn)
    rep = mgr.deliver_branch("USR-216", "Entrega com worktree", cwd=ws.path, gh_runner=gh)

    assert rep.ok and rep.action == "merged" and rep.cleaned, rep.message
    assert not ws.path.exists() and sleeps == [0.5]
    assert _git(repo.local, "branch", "--list", ws.branch) == ""


# ---------------------------------------------------------------------------
# sweep_stale: orphans, trash retention, and what must NOT be touched
# ---------------------------------------------------------------------------


def _age(path: Path, days: float) -> None:
    """Back-date a directory tree to ``days`` before the frozen NOW (orphan detection looks at mtimes)."""
    past = NOW.timestamp() - days * 86400
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        for name in (*filenames, *dirnames):
            os.utime(os.path.join(dirpath, name), (past, past))
    os.utime(path, (past, past))


def _no_gh(args: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(list(args), 1, "", "gh is not used here")


def test_sweep_moves_orphan_directories_to_the_trash_never_deleting_them(repo: SimpleNamespace) -> None:
    local = repo.local
    dead_a = local / ".worktrees" / "dead-a"
    dead_b = local / ".claude" / "worktrees" / "dead-b"
    for dead in (dead_a, dead_b):
        (dead / "src").mkdir(parents=True)
        (dead / "src" / "module.py").write_text("print('left behind')\n", encoding="utf-8")
        _age(dead, days=10)
    reserved = local / ".worktrees" / "harness_state"  # machine state next to worktrees
    reserved.mkdir(parents=True)
    (reserved / "verdicts.json").write_text("{}", encoding="utf-8")
    _age(reserved, days=10)
    broken = local / ".worktrees" / "broken-with-git"  # has .git but git does not know it
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
    _age(broken, days=10)
    live = create_workspace("USR-220", cwd=local, now_fn=lambda: NOW)
    (live.path / "wip.txt").write_text("not committed\n", encoding="utf-8")

    report = GitAutonomyManager(local, now_fn=lambda: NOW).sweep_stale(gh_runner=_no_gh)

    trash = local / ".factory" / "backups" / "trash"
    assert sorted(p.name for p in trash.iterdir()) == [f"dead-a-{STAMP}", f"dead-b-{STAMP}"]
    assert (trash / f"dead-a-{STAMP}" / "src" / "module.py").read_text(encoding="utf-8") == "print('left behind')\n"
    assert not dead_a.exists() and not dead_b.exists()
    assert len(report.orphans_trashed) == 2
    # Untouched: reserved state dir, the unregistered-but-.git directory, the live worktree
    assert reserved.exists() and broken.exists() and live.path.exists()
    assert any("broken-with-git" in item and "left alone" in item for item in report.skipped)
    assert str(live.path) not in report.removed_worktrees


def test_sweep_leaves_recently_touched_orphan_candidates_alone(repo: SimpleNamespace) -> None:
    fresh = repo.local / ".worktrees" / "being-created"
    fresh.mkdir(parents=True)
    (fresh / "file.txt").write_text("x\n", encoding="utf-8")

    report = GitAutonomyManager(repo.local).sweep_stale(gh_runner=_no_gh)  # default one-hour grace

    assert fresh.exists() and report.orphans_trashed == []
    assert any("being-created" in item and "recently" in item for item in report.skipped)


def test_sweep_purges_only_trash_entries_past_the_retention(repo: SimpleNamespace) -> None:
    trash = repo.local / ".factory" / "backups" / "trash"
    old_stamp = (NOW - timedelta(days=8)).strftime("%Y%m%dT%H%M%SZ")
    new_stamp = (NOW - timedelta(days=6)).strftime("%Y%m%dT%H%M%SZ")
    for name in (f"old-{old_stamp}", f"fresh-{new_stamp}"):
        (trash / name).mkdir(parents=True)
        (trash / name / "f.txt").write_text("f\n", encoding="utf-8")

    report = GitAutonomyManager(repo.local, now_fn=lambda: NOW).sweep_stale(gh_runner=_no_gh)

    assert report.trash_purged == [f"old-{old_stamp}"]
    assert not (trash / f"old-{old_stamp}").exists()
    assert (trash / f"fresh-{new_stamp}" / "f.txt").exists()  # still recoverable


def test_purge_trash_falls_back_to_mtime_for_unstamped_entries(repo: SimpleNamespace) -> None:
    trash = repo.local / ".factory" / "backups" / "trash"
    odd = trash / "no-timestamp-here"
    odd.mkdir(parents=True)
    _age(odd, days=30)
    report = purge_trash(repo.local, now_fn=lambda: NOW)
    assert report.purged == ["no-timestamp-here"] and not odd.exists()


def test_sweep_does_not_touch_dirty_worktrees_or_unmerged_branches(repo: SimpleNamespace) -> None:
    local = repo.local
    # merged branch + dirty worktree: must stay
    dirty = create_workspace("USR-221", cwd=local, now_fn=lambda: NOW)
    (dirty.path / "unsaved.txt").write_text("precious uncommitted work\n", encoding="utf-8")
    # unmerged commits: must stay (worktree AND branch)
    unmerged = create_workspace("USR-222", cwd=local, now_fn=lambda: NOW)
    (unmerged.path / "feature.txt").write_text("f\n", encoding="utf-8")
    GitAutonomyManager(local).commit_ticket("USR-222", "Ainda nao mergeado", cwd=unmerged.path)
    # locked (agent running): must stay
    locked = create_workspace("USR-223", cwd=local, now_fn=lambda: NOW)
    _git(local, "worktree", "lock", str(locked.path))
    # merged, clean and idle: the only one that goes
    idle = create_workspace("USR-224", cwd=local, now_fn=lambda: NOW)
    # a loose, unmerged branch with no worktree
    _git(local, "checkout", "-q", "-b", "ticket/loose-unmerged")
    (local / "loose.txt").write_text("l\n", encoding="utf-8")
    _git(local, "add", "loose.txt")
    _git(local, "commit", "--quiet", "-m", "wip")
    _git(local, "checkout", "-q", "main")

    report = GitAutonomyManager(local, sweep_grace_s=0).sweep_stale(gh_runner=_no_gh)

    assert dirty.path.exists() and (dirty.path / "unsaved.txt").exists()
    assert unmerged.path.exists() and locked.path.exists()
    assert not idle.path.exists() and str(idle.path.resolve()) in report.removed_worktrees
    assert _git(local, "branch", "--list", dirty.branch, unmerged.branch, "ticket/loose-unmerged").count("\n") == 2
    assert _git(local, "branch", "--list", idle.branch) == ""  # its merged branch is removed with it
    assert any("usr-221" in s and s.endswith("(dirty)") for s in report.skipped)
    assert any("not merged" in s and "usr-222" in s for s in report.skipped)
    assert any("locked" in s and "usr-223" in s for s in report.skipped)
    assert any("branch:ticket/loose-unmerged (not merged)" == s for s in report.skipped)


def test_sweep_spares_a_fresh_worktree_without_commits_and_the_one_it_runs_in(repo: SimpleNamespace) -> None:
    fresh = create_workspace("USR-225", cwd=repo.local)
    current = create_workspace("USR-226", cwd=repo.local)

    default_grace = GitAutonomyManager(repo.local).sweep_stale(gh_runner=_no_gh)
    assert fresh.path.exists() and any("fresh, no commits yet" in s for s in default_grace.skipped)

    # sweeping FROM inside a worktree never removes that worktree
    report = GitAutonomyManager(repo.local, sweep_grace_s=0).sweep_stale(gh_runner=_no_gh, cwd=current.path)
    assert current.path.exists() and any("current working directory" in s for s in report.skipped)
    assert not fresh.path.exists()  # grace 0: the idle fresh one is swept


def test_orphan_finder_ignores_registered_worktrees_and_files(repo: SimpleNamespace) -> None:
    live = create_workspace("USR-227", cwd=repo.local)
    (repo.local / ".worktrees" / "stray.json").write_text("{}", encoding="utf-8")
    orphans, notes = find_orphan_dirs(repo.local, min_age_s=0)
    assert orphans == [] and notes == []
    assert live.path.exists()


# ---------------------------------------------------------------------------
# run_ticket: refuses a dirty shared checkout, prepares the worktree
# ---------------------------------------------------------------------------


def test_shared_checkout_dirty_paths_only_reports_the_primary_checkout(repo: SimpleNamespace) -> None:
    (repo.local / "other_ticket.py").write_text("x = 1\n", encoding="utf-8")
    (repo.local / ".factory" / "telegram").mkdir(parents=True)
    (repo.local / ".factory" / "telegram" / "state.json").write_text("{}", encoding="utf-8")

    assert shared_checkout_dirty_paths(repo.local) == [".factory/telegram/state.json", "other_ticket.py"]
    assert shared_checkout_dirty_paths(repo.local, ignore_prefixes=run_ticket.VOLATILE_STATE_PREFIXES) == [
        "other_ticket.py"
    ]
    ws = create_workspace("USR-230", cwd=repo.local)
    assert shared_checkout_dirty_paths(ws.path) == []  # a ticket worktree is never "shared"


def test_run_ticket_refuses_to_start_from_a_dirty_shared_checkout(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_ticket, "PROJECT_ROOT", repo.local)
    (repo.local / "other_ticket.py").write_text("x = 1\n", encoding="utf-8")
    args = run_ticket.build_parser().parse_args(["USR-231"])
    ticket = UserTicket(id="USR-231", project_id="darkfac", title="Ticket de teste")

    with pytest.raises(run_ticket.SharedCheckoutDirty) as excinfo:
        run_ticket._prepare_workspace(args, ticket)

    message = str(excinfo.value)
    assert "other_ticket.py" in message and "checkout compartilhado" in message
    assert not (repo.local / ".worktrees").exists()  # nothing was created

    allowed = run_ticket.build_parser().parse_args(["USR-231", "--allow-dirty-shared-checkout"])
    workspace = run_ticket._prepare_workspace(allowed, ticket)
    assert workspace.path.exists() and not (workspace.path / "other_ticket.py").exists()  # never carried over


def test_prepare_workspace_adds_a_new_ticket_to_the_worktree_ledger_not_the_shared_one(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_ticket, "PROJECT_ROOT", repo.local)
    args = run_ticket.build_parser().parse_args(["--create", "--title", "Novo"])
    ticket = UserTicket(id="USR-232", project_id="darkfac", title="Ticket novo")

    workspace = run_ticket._prepare_workspace(args, ticket)

    in_worktree = DemandsStore(workspace.path / ".factory" / "demands" / "demands.json").get_ticket("USR-232")
    assert in_worktree is not None and in_worktree.title == "Ticket novo"
    assert not (repo.local / ".factory").exists()  # the shared checkout stayed untouched


def test_discarding_an_untouched_workspace_removes_worktree_and_branch(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_ticket, "PROJECT_ROOT", repo.local)
    untouched = create_workspace("USR-233", cwd=repo.local)
    touched = create_workspace("USR-234", cwd=repo.local)
    (touched.path / "agent_output.py").write_text("x = 1\n", encoding="utf-8")

    assert run_ticket._discard_untouched_workspace(untouched) is True
    assert not untouched.path.exists() and _git(repo.local, "branch", "--list", untouched.branch) == ""
    assert run_ticket._discard_untouched_workspace(touched) is False  # the agent's work is never dropped
    assert touched.path.exists()

    bogus = TicketWorkspace(
        ticket_id="USR-9", path=repo.tmp, branch="ticket/usr-9", main_root=repo.tmp, base_ref="origin/main", base_sha="0" * 40
    )
    assert run_ticket._discard_untouched_workspace(bogus) is False  # not a worktree: never deleted
    assert repo.tmp.exists()


def _github_like_origin(repo: SimpleNamespace) -> None:
    """Make `origin` look like a GitHub remote (so the PR path is taken) while it stays a local bare repo."""
    _git(repo.local, "remote", "set-url", "origin", "https://github.com/x/y.git")
    _git(repo.local, "config", f"url.{repo.bare.as_posix()}.insteadOf", "https://github.com/x/y.git")


def _seed_ledger_ticket(repo: SimpleNamespace, ticket_id: str) -> str:
    ledger = repo.local / ".factory" / "demands" / "demands.json"
    DemandsStore(ledger).save_ticket(
        UserTicket(
            id=ticket_id, project_id="darkfac", title="Ticket de ponta a ponta", problem_statement="p",
            acceptance_criteria=["c"],
        )
    )
    _git(repo.local, "add", ".factory/demands/demands.json")
    _git(repo.local, "commit", "--quiet", "-m", "chore(backlog): seed")
    _git(repo.local, "push", "--quiet", "origin", "main")
    return _git(repo.local, "rev-parse", "HEAD")


def _launcher_env(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, agent: object, gh: FakeGh
) -> None:
    from core.line import routing

    healthy = {
        name: {"provider": provider, "headroom": 80.0, "is_critical": False, "status": "SAUDAVEL"}
        for name, provider in routing._HARNESS_TO_PROVIDER.items()
    }
    monkeypatch.setattr(run_ticket, "PROJECT_ROOT", repo.local)
    monkeypatch.setattr(run_ticket, "inspect_quotas", lambda: healthy)
    monkeypatch.setattr(run_ticket, "run_agent", agent)
    monkeypatch.setattr(autonomy, "_run_gh", gh)
    monkeypatch.setattr(routing, "default_cooldown_path", lambda: repo.tmp / "cooldowns.json")


def test_run_ticket_main_exits_3_and_creates_nothing_when_the_shared_checkout_is_dirty(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from unittest.mock import MagicMock

    _github_like_origin(repo)
    _seed_ledger_ticket(repo, "USR-98")
    (repo.local / "other_ticket.py").write_text("x = 1\n", encoding="utf-8")
    agent = MagicMock(name="run_agent")
    _launcher_env(repo, monkeypatch, agent, FakeGh(repo.bare))

    assert run_ticket.main(["USR-98", "--harness", "codex", "--skip-validation"]) == 3

    assert "other_ticket.py" in capsys.readouterr().err
    agent.assert_not_called()
    assert not (repo.local / ".worktrees").exists()


def test_run_ticket_removes_the_worktree_when_the_agent_fails_without_touching_anything(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.line.agent_cli import AgentResult

    _github_like_origin(repo)
    _seed_ledger_ticket(repo, "USR-97")
    seen: dict[str, Path] = {}

    def agent(request: object) -> AgentResult:
        seen["cwd"] = request.cwd  # type: ignore[attr-defined]
        return AgentResult(ok=False, text="", harness="codex", error_kind="not_installed", exit_code=None, duration_s=1.0)

    _launcher_env(repo, monkeypatch, agent, FakeGh(repo.bare))

    assert run_ticket.main(["USR-97", "--harness", "codex", "--skip-validation", "--json"]) == 1

    assert not seen["cwd"].exists()  # nothing to keep: the worktree and its branch are gone
    assert _git(repo.local, "branch", "--list", "ticket/*") == ""


def test_run_ticket_keeps_the_worktree_when_a_failed_agent_left_work_behind(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.line.agent_cli import AgentResult

    _github_like_origin(repo)
    _seed_ledger_ticket(repo, "USR-96")
    seen: dict[str, Path] = {}

    def agent(request: object) -> AgentResult:
        seen["cwd"] = request.cwd  # type: ignore[attr-defined]
        (request.cwd / "half_done.py").write_text("x = ", encoding="utf-8")  # type: ignore[attr-defined]
        return AgentResult(ok=False, text="", harness="codex", error_kind="timeout", exit_code=None, duration_s=1.0)

    _launcher_env(repo, monkeypatch, agent, FakeGh(repo.bare))
    monkeypatch.setattr(run_ticket.time, "sleep", lambda _s: None)  # the retry backoff must not really wait

    assert run_ticket.main(["USR-96", "--harness", "codex", "--skip-validation", "--json"]) == 1

    assert (seen["cwd"] / "half_done.py").exists()  # the partial work is never destroyed
