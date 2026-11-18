"""Ownerless worktree classification (USR-500 T1).

Every git repository here lives under ``tmp_path``. Classification only reads.
It must not call ``cleanup``, remove a directory, or delete a branch.
"""

from __future__ import annotations

import builtins
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.git import ticket_workspace
from core.git.ticket_workspace import (
    OWNERLESS_MAX_AGE_S,
    OwnerlessFacts,
    TicketStatusUnreadable,
    classify_ownerless,
    classify_ownerless_worktree,
    classify_ownerless_worktrees,
    resolve_ticket_status,
    substantive_worktree_changes,
    worktree_age_s,
    worktree_ticket_id,
)

BORN = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
STAMP = "20261101T000000Z"


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return res.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> SimpleNamespace:
    """A bare origin plus a clone on main."""
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


def _facts(**overrides: object) -> OwnerlessFacts:
    base: dict[str, object] = {
        "path": Path("usr-500-20261101T000000Z"),
        "is_main": False,
        "locked": False,
        "index_locked": False,
        "git_readable": True,
        "detached": False,
        "head_outside_base": False,
        "changed_paths": (),
        "status_readable": True,
        "ticket_status": "completed",
        "ticket_status_known": True,
        "holders": (),
        "age_s": OWNERLESS_MAX_AGE_S + 1,
    }
    base.update(overrides)
    return OwnerlessFacts(**base)  # type: ignore[arg-type]


def _idle(_path: Path) -> tuple[str, ...]:
    return ()


def _forbid_removal(args: list[str] | tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
    if list(args[:2]) == ["worktree", "remove"] or (args and args[0] == "branch" and any(flag in args for flag in ("-d", "-D"))):
        raise AssertionError(f"classification tried to change git state: {args}")
    return ticket_workspace._git(args, cwd)


def _old_worktree(local: Path, ticket_id: str = "USR-500") -> Path:
    workspace = ticket_workspace.create_workspace(ticket_id, cwd=local, now_fn=lambda: BORN)
    assert STAMP in workspace.path.name
    return workspace.path


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------


def test_ownerless_max_age_is_48_hours() -> None:
    assert OWNERLESS_MAX_AGE_S == 48 * 60 * 60


def test_age_uses_directory_stamp_and_ignores_mtime(tmp_path: Path) -> None:
    stamped = tmp_path / f"usr-1-{STAMP}"
    stamped.mkdir()
    future = datetime(2030, 1, 1, tzinfo=timezone.utc)
    os.utime(stamped, (future.timestamp(), future.timestamp()))
    missing = tmp_path / f"usr-2-{STAMP}-3"
    # The directory is never created: a stamp must be enough on its own.
    now = BORN + timedelta(hours=48, seconds=1)
    assert worktree_age_s(stamped, now=now) == OWNERLESS_MAX_AGE_S + 1
    assert worktree_age_s(missing, now=now) == OWNERLESS_MAX_AGE_S + 1
    assert worktree_age_s(stamped, now=BORN + timedelta(hours=48)) == OWNERLESS_MAX_AGE_S


def test_age_uses_directory_mtime_only_when_the_stamp_is_absent(tmp_path: Path) -> None:
    plain = tmp_path / "scratch-no-stamp"
    plain.mkdir()
    child = plain / "child"
    child.mkdir()
    now = datetime(2026, 11, 5, 0, 0, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=49)
    os.utime(child, (now.timestamp(), now.timestamp()))
    os.utime(plain, (old.timestamp(), old.timestamp()))
    age = worktree_age_s(plain, now=now)
    assert age is not None
    assert abs(age - 49 * 60 * 60) < 2

    invalid = tmp_path / "usr-3-20261399T000000Z"
    invalid.mkdir()
    os.utime(invalid, (old.timestamp(), old.timestamp()))
    invalid_age = worktree_age_s(invalid, now=now)
    assert invalid_age is not None and abs(invalid_age - 49 * 60 * 60) < 2

    assert worktree_age_s(tmp_path / "missing-no-stamp", now=now) is None


def test_scratch_and_demands_ledger_are_not_substantive_changes() -> None:
    assert substantive_worktree_changes(
        [
            ".tmp",
            ".tmp/lock",
            ".tmp\\harness.json",
            "./.factory/demands/demands.json",
            ".factory/demands/demands.json",
            ".factory\\demands\\demands.json",
        ]
    ) == []
    assert substantive_worktree_changes([".tmpfile", "src/.tmp/a", ".factory/demands/other.json", "keep.txt"]) == [
        ".tmpfile",
        "src/.tmp/a",
        ".factory/demands/other.json",
        "keep.txt",
    ]


def test_ticket_id_comes_from_the_branch_or_the_directory_stamp() -> None:
    assert worktree_ticket_id(Path(f"usr-500-{STAMP}"), "refs/heads/ticket/usr-500") == "usr-500"
    assert worktree_ticket_id(Path(f"usr-500-{STAMP}"), f"refs/heads/ticket/usr-500-{STAMP}") == "usr-500"
    assert worktree_ticket_id(Path(f"usr-500-{STAMP}-2"), "") == "usr-500"
    assert worktree_ticket_id(Path("scratch-no-stamp"), "") is None


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"is_main": True}, "main_checkout"),
        ({"cleanup_pending": True}, "cleanup_pending"),
        ({"holders": None}, "psutil_unavailable"),
        ({"holders": ("python (pid 7)",)}, "holder"),
        ({"locked": True}, "locked"),
        ({"index_locked": True}, "index_lock"),
        ({"git_readable": False}, "git_unreadable"),
        ({"status_readable": False}, "status_unreadable"),
        ({"changed_paths": ("keep.txt",)}, "dirty"),
        ({"detached": True, "head_outside_base": True}, "detached_outside_base"),
        ({"ticket_status_known": False}, "ticket_status_unknown"),
        ({"ticket_status": "planned"}, "open_ticket"),
        ({"ticket_status": "implementing"}, "open_ticket"),
        ({"ticket_status": "validating"}, "open_ticket"),
        ({"ticket_status": "waiting_human"}, "open_ticket"),
        ({"ticket_status": "discovered"}, "open_ticket"),
        ({"ticket_status": "accepted"}, "open_ticket"),
        ({"ticket_status": ""}, "open_ticket"),
        ({"age_s": None}, "age_unknown"),
        ({"age_s": 0}, "too_young"),
        ({"age_s": OWNERLESS_MAX_AGE_S}, "too_young"),
        ({"age_s": OWNERLESS_MAX_AGE_S - 1}, "too_young"),
        ({"ticket_status": None}, "ownerless"),
        ({"ticket_status": "cancelled"}, "ownerless"),
        ({"ticket_status": "canceled"}, "ownerless"),
        ({"ticket_status": "COMPLETED"}, "ownerless"),
        (
            {"changed_paths": (".tmp/lock", ".factory/demands/demands.json", ".tmp")},
            "ownerless",
        ),
        ({"detached": True, "head_outside_base": False}, "ownerless"),
        ({"detached": False, "head_outside_base": True}, "ownerless"),
    ],
)
def test_classify_ownerless_matrix(overrides: dict[str, object], reason: str) -> None:
    verdict = classify_ownerless(_facts(**overrides))
    assert verdict.reason == reason
    assert verdict.eligible is (reason == "ownerless")


def test_psutil_failure_wins_over_a_dirty_tree() -> None:
    verdict = classify_ownerless(_facts(holders=None, changed_paths=("keep.txt",)))
    assert verdict.reason == "psutil_unavailable" and not verdict.eligible


def test_active_ticket_is_preserved_before_reporting_retryable_dirty_state() -> None:
    verdict = classify_ownerless(
        _facts(ticket_status="implementing", changed_paths=("keep.txt",), holders=("python (pid 7)",))
    )
    assert verdict.reason == "open_ticket" and not verdict.eligible


def test_resolve_ticket_status_prefers_a_terminal_ledger_row() -> None:
    assert resolve_ticket_status("completed", "implementing", file_readable=True) == "completed"
    assert resolve_ticket_status("planned", "completed", file_readable=True) == "completed"
    assert resolve_ticket_status("cancelled", None, file_readable=True) == "cancelled"
    assert resolve_ticket_status(None, None, file_readable=True) is None
    assert resolve_ticket_status("implementing", None, file_readable=True) == "implementing"
    with pytest.raises(TicketStatusUnreadable):
        resolve_ticket_status(None, None, file_readable=False)


# ---------------------------------------------------------------------------
# Reader against a real temporary repository
# ---------------------------------------------------------------------------


def test_reader_age_boundary_follows_the_stamp_not_mtime(repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _old_worktree(repo.local)
    future = datetime(2030, 1, 1, tzinfo=timezone.utc)
    os.utime(path, (future.timestamp(), future.timestamp()))
    monkeypatch.setattr(ticket_workspace, "cleanup", _fail_cleanup)
    at_limit = classify_ownerless_worktree(
        path,
        now=BORN + timedelta(hours=48),
        statuses={"USR-500": "completed"},
        holder_probe=_idle,
        run_git=_forbid_removal,
    )
    over = classify_ownerless_worktree(
        path,
        now=BORN + timedelta(hours=48, seconds=1),
        statuses={"usr-500": "completed"},
        holder_probe=_idle,
        run_git=_forbid_removal,
    )
    assert at_limit.reason == "too_young" and not at_limit.eligible
    assert over.reason == "ownerless" and over.eligible
    assert path.exists()
    assert "ticket/usr-500" in _git(repo.local, "branch", "--list", "ticket/usr-500")


def test_reader_preserves_worktree_registered_as_cleanup_pending(repo: SimpleNamespace) -> None:
    path = _old_worktree(repo.local)
    pending_dir = repo.local / ticket_workspace.CLEANUP_PENDING_RELATIVE
    pending_dir.mkdir(parents=True)
    (pending_dir / "pending.json").write_text(
        json.dumps({"path": str(path.resolve())}), encoding="utf-8"
    )

    verdict = classify_ownerless_worktree(
        path,
        now=BORN + timedelta(hours=49),
        statuses={"USR-500": "completed"},
        holder_probe=_idle,
        run_git=_forbid_removal,
    )

    assert verdict.reason == "cleanup_pending" and not verdict.eligible
    assert path.exists()


def _fail_cleanup(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("cleanup must not run during classification")


def test_scratch_and_ledger_do_not_make_a_worktree_dirty(repo: SimpleNamespace) -> None:
    path = _old_worktree(repo.local)
    now = BORN + timedelta(hours=49)
    (path / ".tmp").mkdir()
    (path / ".tmp" / "suite.lock").write_text("1", encoding="utf-8")
    ledger = path / ".factory" / "demands"
    ledger.mkdir(parents=True)
    (ledger / "demands.json").write_text("[]\n", encoding="utf-8")
    clean = classify_ownerless_worktree(
        path, now=now, statuses={"USR-500": "completed"}, holder_probe=_idle, run_git=_forbid_removal
    )
    assert clean.eligible and clean.reason == "ownerless"
    (path / "keep.txt").write_text("real work\n", encoding="utf-8")
    dirty = classify_ownerless_worktree(
        path, now=now, statuses={"USR-500": "completed"}, holder_probe=_idle, run_git=_forbid_removal
    )
    assert dirty.reason == "dirty" and not dirty.eligible
    assert (path / "keep.txt").is_file()
    assert (path / ".tmp" / "suite.lock").is_file()
    assert (ledger / "demands.json").is_file()


@pytest.mark.parametrize("status", ["planned", "implementing", "validating", "waiting_human"])
def test_open_ticket_is_preserved(repo: SimpleNamespace, status: str) -> None:
    path = _old_worktree(repo.local)
    verdict = classify_ownerless_worktree(
        path,
        now=BORN + timedelta(hours=49),
        statuses={"USR-500": status},
        holder_probe=_idle,
    )
    assert verdict.reason == "open_ticket" and not verdict.eligible
    assert path.exists()


@pytest.mark.parametrize("statuses", [{}, {"USR-500": "completed"}, {"usr-500": "cancelled"}])
def test_terminal_or_absent_ticket_is_eligible(repo: SimpleNamespace, statuses: dict[str, str]) -> None:
    path = _old_worktree(repo.local)
    verdict = classify_ownerless_worktree(
        path,
        now=BORN + timedelta(hours=49),
        statuses=statuses,
        holder_probe=_idle,
        run_git=_forbid_removal,
    )
    assert verdict.eligible and verdict.reason == "ownerless"


def test_main_checkout_is_never_eligible(repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    old = BORN - timedelta(days=30)
    now = old + timedelta(hours=49)
    os.utime(repo.local, (old.timestamp(), old.timestamp()))
    monkeypatch.setattr(ticket_workspace, "cleanup", _fail_cleanup)
    monkeypatch.setattr(ticket_workspace, "robust_rmtree", _fail_cleanup)
    monkeypatch.setattr(ticket_workspace, "trash_directory", _fail_cleanup)
    verdict = classify_ownerless_worktree(repo.local, now=now, statuses={}, holder_probe=_idle)
    assert verdict.reason == "main_checkout" and not verdict.eligible
    assert (repo.local / "README.md").is_file()


def test_holder_locked_and_index_lock_preserve(repo: SimpleNamespace) -> None:
    path = _old_worktree(repo.local)
    now = BORN + timedelta(hours=49)
    held = classify_ownerless_worktree(
        path,
        now=now,
        statuses={"USR-500": "completed"},
        holder_probe=lambda _path: ("python (pid 42)",),
    )
    assert held.reason == "holder" and not held.eligible

    _git(repo.local, "worktree", "lock", str(path), "--reason", "agent")
    locked = classify_ownerless_worktree(path, now=now, statuses={"USR-500": "completed"}, holder_probe=_idle)
    assert locked.reason == "locked" and path.exists()
    _git(repo.local, "worktree", "unlock", str(path))

    git_dir = Path(_git(path, "rev-parse", "--absolute-git-dir"))
    index_lock = git_dir / "index.lock"
    index_lock.write_text("", encoding="utf-8")
    try:
        locked_index = classify_ownerless_worktree(
            path, now=now, statuses={"USR-500": "completed"}, holder_probe=_idle, run_git=_forbid_removal
        )
        assert locked_index.reason == "index_lock" and not locked_index.eligible
        assert index_lock.is_file()
    finally:
        index_lock.unlink(missing_ok=True)


def test_detached_head_outside_the_base_is_preserved_and_on_the_base_is_not(repo: SimpleNamespace) -> None:
    path = _old_worktree(repo.local)
    now = BORN + timedelta(hours=49)
    (path / "feature.txt").write_text("wip\n", encoding="utf-8")
    _git(path, "add", "feature.txt")
    _git(path, "commit", "--quiet", "-m", "wip")
    _git(path, "checkout", "--quiet", "--detach")
    outside = classify_ownerless_worktree(
        path, now=now, statuses={"USR-500": "completed"}, holder_probe=_idle, run_git=_forbid_removal
    )
    assert outside.reason == "detached_outside_base" and not outside.eligible
    assert (path / "feature.txt").is_file()

    _git(path, "checkout", "--quiet", "--detach", "origin/main")
    on_base = classify_ownerless_worktree(
        path, now=now, statuses={"USR-500": "cancelled"}, holder_probe=_idle, run_git=_forbid_removal
    )
    assert on_base.reason == "ownerless" and on_base.eligible
    assert "ticket/usr-500" in _git(repo.local, "branch", "--list", "ticket/usr-500")


def test_directory_mtime_is_used_when_the_name_has_no_stamp(repo: SimpleNamespace) -> None:
    path = repo.local / ".worktrees" / "scratch-no-stamp"
    path.parent.mkdir(parents=True)
    _git(repo.local, "worktree", "add", "--detach", "--quiet", str(path), "HEAD")
    now = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=49)
    readme = path / "README.md"
    os.utime(readme, (now.timestamp(), now.timestamp()))
    os.utime(path, (old.timestamp(), old.timestamp()))
    old_verdict = classify_ownerless_worktree(path, now=now, statuses={}, holder_probe=_idle, run_git=_forbid_removal)
    assert old_verdict.eligible and old_verdict.reason == "ownerless"
    os.utime(path, (now.timestamp(), now.timestamp()))
    young = classify_ownerless_worktree(path, now=now, statuses={}, holder_probe=_idle)
    assert young.reason == "too_young" and not young.eligible
    assert path.exists()


def test_psutil_importerror_preserves_the_worktree(repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _old_worktree(repo.local)
    real_import = builtins.__import__

    def blocked(name: str, globals: object = None, locals: object = None, fromlist: tuple[str, ...] = (), level: int = 0) -> object:
        if name == "psutil" or name.startswith("psutil."):
            raise ImportError("psutil blocked for the test")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked)
    verdict = classify_ownerless_worktree(
        path,
        now=BORN + timedelta(hours=49),
        statuses={"USR-500": "completed"},
        run_git=_forbid_removal,
    )
    assert verdict.reason == "psutil_unavailable" and not verdict.eligible
    assert path.exists()


def test_ledger_is_read_and_never_created(repo: SimpleNamespace) -> None:
    path = _old_worktree(repo.local)
    now = BORN + timedelta(hours=49)
    ledger = repo.local / ".factory" / "demands" / "demands.json"
    absent = classify_ownerless_worktree(path, now=now, queue_lookup=lambda _ticket: None, holder_probe=_idle)
    assert absent.eligible and absent.reason == "ownerless"
    assert not ledger.exists()

    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps([{"id": "USR-500", "status": "planned"}]), encoding="utf-8")
    open_ticket = classify_ownerless_worktree(path, now=now, queue_lookup=lambda _ticket: None, holder_probe=_idle)
    assert open_ticket.reason == "open_ticket"

    completed_in_queue = classify_ownerless_worktree(
        path, now=now, queue_lookup=lambda _ticket: "completed", holder_probe=_idle
    )
    assert completed_in_queue.eligible

    ledger.write_text(json.dumps([{"id": "usr-500", "status": "completed"}]), encoding="utf-8")
    terminal_file = classify_ownerless_worktree(
        path, now=now, queue_lookup=lambda _ticket: "implementing", holder_probe=_idle
    )
    assert terminal_file.eligible

    ledger.write_text("{", encoding="utf-8")
    unknown = classify_ownerless_worktree(path, now=now, queue_lookup=lambda _ticket: None, holder_probe=_idle)
    assert unknown.reason == "ticket_status_unknown" and not unknown.eligible
    assert ledger.read_text(encoding="utf-8") == "{"
    assert path.exists()


def test_batch_classification_removes_nothing(repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    old = _old_worktree(repo.local, "USR-500")
    moment = BORN + timedelta(hours=49)
    young = ticket_workspace.create_workspace("USR-501", cwd=repo.local, now_fn=lambda: moment).path
    monkeypatch.setattr(ticket_workspace, "cleanup", _fail_cleanup)
    monkeypatch.setattr(ticket_workspace, "robust_rmtree", _fail_cleanup)
    monkeypatch.setattr(ticket_workspace, "trash_directory", _fail_cleanup)
    verdicts = classify_ownerless_worktrees(
        repo.local,
        now=moment,
        statuses={"USR-500": "completed", "USR-501": "cancelled"},
        holder_probe=_idle,
        queue_lookup=lambda _ticket: None,
        run_git=_forbid_removal,
    )
    by_path = {os.path.normcase(os.path.realpath(str(item.path))): item for item in verdicts}
    main = by_path[os.path.normcase(os.path.realpath(str(repo.local)))]
    old_verdict = by_path[os.path.normcase(os.path.realpath(str(old)))]
    young_verdict = by_path[os.path.normcase(os.path.realpath(str(young)))]
    assert main.reason == "main_checkout" and not main.eligible
    assert old_verdict.eligible and old_verdict.reason == "ownerless"
    assert young_verdict.reason == "too_young" and not young_verdict.eligible
    assert old.exists() and young.exists() and (repo.local / "README.md").is_file()
    branches = _git(repo.local, "branch", "--list", "ticket/*")
    assert "ticket/usr-500" in branches and "ticket/usr-501" in branches
