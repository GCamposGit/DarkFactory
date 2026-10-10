"""Cross-worktree ID reservations and merge identity checks (USR-75)."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import subprocess

import pytest

import core.demands.id_allocator as id_allocator
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


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")


def test_allocator_fetches_origin_before_reserving_and_skips_taken_ids(tmp_path: Path) -> None:
    """A stale origin/main ref must not mint an ID the remote already registered (USR-193)."""
    remote = tmp_path / "remote"
    _init_repo(remote)
    ledger(remote, ("USR-186", "stale max"))
    git(remote, "add", LEDGER)
    git(remote, "commit", "-m", "seed")

    local = tmp_path / "local"
    git(tmp_path, "clone", str(remote), str(local))
    stale_main = git(local, "rev-parse", "origin/main")

    ledger(
        remote,
        ("USR-186", "stale max"),
        ("USR-187", "registered on main"),
        ("USR-188", "registered on main"),
    )
    git(remote, "add", LEDGER)
    git(remote, "commit", "-m", "register USR-187 and USR-188")
    git(remote, "checkout", "-b", "ticket/usr-190")
    ledger(
        remote,
        ("USR-186", "stale max"),
        ("USR-187", "registered on main"),
        ("USR-188", "registered on main"),
        ("USR-190", "reserved on ticket branch"),
    )
    git(remote, "add", LEDGER)
    git(remote, "commit", "-m", "ticket branch reserves USR-190")
    git(remote, "checkout", "main")

    reserved = reserve_ticket_id(local / LEDGER, "USR")

    assert reserved == "USR-191"
    assert reserved not in {"USR-186", "USR-187", "USR-188", "USR-190"}
    assert git(local, "rev-parse", "origin/main") != stale_main
    assert "USR-187" in git(local, "show", f"origin/main:{LEDGER}")
    assert "USR-188" in git(local, "show", f"origin/main:{LEDGER}")
    ticket_ledger = git(local, "show", f"refs/remotes/origin/ticket/usr-190:{LEDGER}")
    assert "USR-190" in ticket_ledger


def test_allocator_recalculates_when_candidate_is_already_on_origin_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the candidate is on origin/main after the snapshot, reserve the next free ID."""
    root = tmp_path / "repo"
    _init_repo(root)
    path = ledger(root, ("USR-186", "seed"))
    git(root, "add", LEDGER)
    git(root, "commit", "-m", "seed")
    git(root, "update-ref", "refs/remotes/origin/main", git(root, "rev-parse", "HEAD"))

    real_show = id_allocator._show_ledger
    origin_reads = {"count": 0}

    def show(root_path: Path, ref: str) -> str | None:
        raw = real_show(root_path, ref)
        if ref != "origin/main" or raw is None:
            return raw
        origin_reads["count"] += 1
        if origin_reads["count"] == 1:
            return raw
        rows = json.loads(raw)
        if not any(row.get("id") == "USR-187" for row in rows):
            rows.append({"id": "USR-187", "title": "landed on origin/main"})
        return json.dumps(rows)

    monkeypatch.setattr(id_allocator, "_show_ledger", show)

    assert reserve_ticket_id(path, "USR") == "USR-188"
    assert origin_reads["count"] >= 2
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    saved = json.loads((common / "darkfac-ids.json").read_text(encoding="utf-8"))
    assert saved["USR"] == 188


def test_allocator_fails_closed_when_origin_fetch_fails(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    path = ledger(root, ("USR-186", "seed"))
    git(root, "add", LEDGER)
    git(root, "commit", "-m", "seed")
    git(root, "remote", "add", "origin", str(tmp_path / "missing.git"))

    with pytest.raises(RuntimeError, match="Cannot fetch origin/main"):
        reserve_ticket_id(path, "USR")

    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    assert not (common / "darkfac-ids.json").exists()
