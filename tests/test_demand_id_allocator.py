"""Cross-worktree ID reservations and merge identity checks (USR-75)."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import subprocess

from core.demands.id_allocator import LEDGER, reserve_ticket_id, title_collisions
from core.demands.store import DemandsStore


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        encoding="utf-8", check=True,
    )
    return result.stdout.strip()


def ledger(root: Path, *items: tuple[str, str]) -> Path:
    path = root / LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{"id": key, "title": title} for key, title in items]), encoding="utf-8")
    return path


def allocate(path: Path) -> str:
    return DemandsStore(path).next_ticket_id()


def test_two_worktrees_reserve_distinct_ids_concurrently(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    first = ledger(root, ("USR-07", "old"))
    git(root, "add", LEDGER)
    git(root, "commit", "-m", "seed")
    git(root, "branch", "ticket/hidden")
    other = tmp_path / "other"
    git(root, "worktree", "add", "-b", "ticket/other", str(other))
    second = ledger(other, ("USR-08", "worktree-only"))

    with ProcessPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(allocate, (first, second)))

    assert set(ids) == {"USR-09", "USR-10"}
    assert reserve_ticket_id(first, "USR") == "USR-11"


def test_allocator_sees_ticket_branch_without_worktree(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    path = ledger(root, ("USR-03", "main"))
    git(root, "add", LEDGER)
    git(root, "commit", "-m", "seed")
    git(root, "checkout", "-b", "ticket/hidden")
    ledger(root, ("USR-90", "branch"))
    git(root, "add", LEDGER)
    git(root, "commit", "-m", "hidden")
    git(root, "checkout", "main")

    assert reserve_ticket_id(path, "USR") == "USR-91"


def test_allocator_sees_origin_main_ahead_of_local_checkout(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    path = ledger(root, ("USR-03", "old"))
    git(root, "add", LEDGER)
    git(root, "commit", "-m", "seed")
    old_head = git(root, "rev-parse", "HEAD")
    ledger(root, ("USR-99", "remote"))
    git(root, "add", LEDGER)
    git(root, "commit", "-m", "remote ticket")
    git(root, "update-ref", "refs/remotes/origin/main", git(root, "rev-parse", "HEAD"))
    git(root, "reset", "--hard", old_head)

    assert reserve_ticket_id(path, "USR") == "USR-100"


def test_title_collision_reports_both_titles() -> None:
    base = json.dumps([{"id": "USR-67", "title": "Original"}])
    branch = json.dumps([{"id": "USR-67", "title": "DarkHub"}])
    assert title_collisions(base, branch) == ["USR-67: origin/main='Original', branch='DarkHub'"]
    assert title_collisions(base, base) == []


def test_allocator_works_without_git_binary(tmp_path: Path, monkeypatch) -> None:
    """The slim DarkHub container ships no git; reservation must use the isolated ledger."""
    path = ledger(tmp_path, ("USR-07", "seed"))

    def no_git(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(subprocess, "run", no_git)

    assert reserve_ticket_id(path, "USR") == "USR-08"
    assert reserve_ticket_id(path, "USR") == "USR-09"
