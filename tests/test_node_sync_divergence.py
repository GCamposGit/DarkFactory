"""Tests for node_sync divergence classification, safe repair, dirty remediation, and live proof (USR-78, USR-119, USR-127)."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from core.infra import node_sync
from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.roadmap.models import DeliveryStatus


def _init_repo(path: Path) -> None:
    """Initialize a git repository with an initial commit on main."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=path, check=True, capture_output=True)


def test_classify_checkout_ahead_behind_and_diverged(tmp_path: Path) -> None:
    origin_repo = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin_repo)], check=True, capture_output=True)

    clone_repo = tmp_path / "clone"
    subprocess.run(["git", "clone", str(origin_repo), str(clone_repo)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=clone_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=clone_repo, check=True, capture_output=True)
    (clone_repo / "file1.txt").write_text("1", encoding="utf-8")
    subprocess.run(["git", "add", "file1.txt"], cwd=clone_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base commit"], cwd=clone_repo, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=clone_repo, check=True, capture_output=True)

    base_sha = node_sync.run_git(clone_repo, ["rev-parse", "HEAD"])

    # 1. Ahead checkout: make 2 local commits without pushing
    (clone_repo / "file2.txt").write_text("2", encoding="utf-8")
    subprocess.run(["git", "add", "file2.txt"], cwd=clone_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "local commit 1"], cwd=clone_repo, check=True, capture_output=True)
    (clone_repo / "file3.txt").write_text("3", encoding="utf-8")
    subprocess.run(["git", "add", "file3.txt"], cwd=clone_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "local commit 2"], cwd=clone_repo, check=True, capture_output=True)

    classification, local_commits, dirty_files, reason = node_sync.classify_checkout(
        clone_repo, node_sync.run_git, base_sha
    )
    assert classification == "ahead"
    assert len(local_commits) == 2
    assert "local unpushed commits" in (reason or "")

    # 2. Behind checkout: in second clone, push commits so clone 1 is behind
    clone2 = tmp_path / "clone2"
    subprocess.run(["git", "clone", str(origin_repo), str(clone2)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test2"], cwd=clone2, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t2@e.com"], cwd=clone2, check=True, capture_output=True)
    (clone2 / "file_remote.txt").write_text("remote", encoding="utf-8")
    subprocess.run(["git", "add", "file_remote.txt"], cwd=clone2, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remote commit"], cwd=clone2, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=clone2, check=True, capture_output=True)

    # In clone 1: fetch origin
    subprocess.run(["git", "fetch", "origin", "main"], cwd=clone_repo, check=True, capture_output=True)
    expected_remote_sha = node_sync.run_git(clone_repo, ["rev-parse", "origin/main"])

    # clone 1 has local commits (ahead 2) AND is behind origin/main (behind 1) -> diverged!
    classification, local_commits, dirty_files, reason = node_sync.classify_checkout(
        clone_repo, node_sync.run_git, expected_remote_sha
    )
    assert classification == "diverged"
    assert len(local_commits) == 2
    assert "diverged: ahead 2 commits" in (reason or "")
    assert "behind 1 commits" in (reason or "")


def test_classify_checkout_dirty_classes_no_content_leak(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    # Add .gitignore for ignored class
    (repo / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add gitignore"], cwd=repo, check=True, capture_output=True)

    # 1. Modify tracked file with secret content
    (repo / "README.md").write_text("SECRET_CONTENT_DO_NOT_LEAK", encoding="utf-8")

    # 2. Add untracked file with secret content
    (repo / "untracked.py").write_text("SECRET_KEY = 12345", encoding="utf-8")

    # 3. Add ignored file with secret content
    (repo / "temp.ignored").write_text("SECRET_TOKEN = 'xyz'", encoding="utf-8")

    expected_sha = node_sync.run_git(repo, ["rev-parse", "HEAD"])
    classification, local_commits, dirty_files, reason = node_sync.classify_checkout(
        repo, node_sync.run_git, expected_sha
    )

    assert classification == "dirty"
    assert "README.md" in dirty_files["tracked_modified"]
    assert "untracked.py" in dirty_files["untracked"]
    assert "temp.ignored" in dirty_files["ignored"]

    # Invariant: names only, never content!
    assert "README.md" in (reason or "")
    assert "untracked.py" in (reason or "")
    assert "SECRET" not in (reason or "")
    assert "12345" not in (reason or "")


def test_remediate_dirty_checkout_ephemeral_vs_unknown(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    # Ephemeral files
    pycache = repo / "__pycache__"
    pycache.mkdir()
    pyc_file = pycache / "test.cpython-312.pyc"
    pyc_file.write_text("bytecode", encoding="utf-8")

    dirty_files = {"tracked_modified": [], "untracked": ["__pycache__/test.cpython-312.pyc"], "ignored": []}
    remediated = node_sync.remediate_dirty_checkout(repo, node_sync.run_git, dirty_files)
    assert remediated is True
    assert not pyc_file.exists()

    # Non-ephemeral file (e.g. real user code)
    user_file = repo / "core" / "new_feature.py"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("code", encoding="utf-8")

    store_file = repo / ".factory" / "demands" / "demands.json"
    store = DemandsStore(store_file)

    dirty_files2 = {"tracked_modified": ["core/new_feature.py"], "untracked": [], "ignored": []}
    alerts = []
    remediated2 = node_sync.remediate_dirty_checkout(
        repo, node_sync.run_git, dirty_files2, store=store, notifier=alerts.append
    )
    assert remediated2 is False
    assert user_file.exists()  # Never deleted!
    assert len(alerts) == 1
    assert "core/new_feature.py" in alerts[0]

    # Ticket created in demands store
    tickets = store.list_tickets()
    assert any("Investigar arquivos modificados nao-efemeros" in t.title for t in tickets)


def test_safe_repair_divergent_checkout_remote_backup(tmp_path: Path) -> None:
    origin_repo = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin_repo)], check=True, capture_output=True)

    clone_repo = tmp_path / "clone"
    subprocess.run(["git", "clone", str(origin_repo), str(clone_repo)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=clone_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=clone_repo, check=True, capture_output=True)
    (clone_repo / "f.txt").write_text("1", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=clone_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=clone_repo, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=clone_repo, check=True, capture_output=True)

    base_sha = node_sync.run_git(clone_repo, ["rev-parse", "HEAD"])

    # Make local unpushed commit
    (clone_repo / "divergent.txt").write_text("local work", encoding="utf-8")
    subprocess.run(["git", "add", "divergent.txt"], cwd=clone_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "work to keep in backup"], cwd=clone_repo, check=True, capture_output=True)

    store_file = clone_repo / ".factory" / "demands" / "demands.json"
    store = DemandsStore(store_file)

    notifs = []
    backup_branch = node_sync.safe_repair_divergent_checkout(
        clone_repo, "Desktop", node_sync.run_git, base_sha, notifier=notifs.append, store=store
    )

    assert backup_branch is not None
    assert backup_branch.startswith("backup/desktop-")
    # Verify backup branch exists on origin
    branches_remote = node_sync.run_git(clone_repo, ["branch", "-r"])
    assert f"origin/{backup_branch}" in branches_remote

    # Verify local checkout is now reset to origin/main
    current_sha = node_sync.run_git(clone_repo, ["rev-parse", "HEAD"])
    assert current_sha == base_sha
    assert not (clone_repo / "divergent.txt").exists()

    # Ticket created for cherry-pick
    tickets = store.list_tickets()
    assert any("Cherry-pick commits divergentes" in t.title and backup_branch in t.title for t in tickets)
    assert len(notifs) == 1


def test_safe_repair_fails_closed_if_backup_push_fails(tmp_path: Path) -> None:
    repo = tmp_path / "isolated"
    _init_repo(repo)

    # Local commit without remote configured (push will fail)
    (repo / "f2.txt").write_text("data", encoding="utf-8")
    subprocess.run(["git", "add", "f2.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "local"], cwd=repo, check=True, capture_output=True)
    unpushed_sha = node_sync.run_git(repo, ["rev-parse", "HEAD"])

    res = node_sync.safe_repair_divergent_checkout(repo, "Desktop", node_sync.run_git, "a" * 40)
    assert res is None  # Push failed, reset aborted!
    # Commit was not reset
    assert node_sync.run_git(repo, ["rev-parse", "HEAD"]) == unpushed_sha
    assert (repo / "f2.txt").exists()


def test_record_divergence_cycle_notification(tmp_path: Path) -> None:
    state_file = tmp_path / "divergence.json"
    alerts = []

    # Cycle 1: divergent -> count = 1, no alert yet
    c1 = node_sync.record_divergence_cycle("Desktop", "divergent", state_path=state_file, notifier=alerts.append)
    assert c1 == 1
    assert len(alerts) == 0

    # Cycle 2: still divergent -> count = 2, alert triggered!
    c2 = node_sync.record_divergence_cycle("Desktop", "divergent", state_path=state_file, notifier=alerts.append)
    assert c2 == 2
    assert len(alerts) == 1
    assert "Desktop permanece divergente por 2 ciclos" in alerts[0]

    # Cycle 3: converged -> count resets to 0
    c3 = node_sync.record_divergence_cycle("Desktop", "converged", state_path=state_file, notifier=alerts.append)
    assert c3 == 0


def test_pending_convergence_persistence(tmp_path: Path) -> None:
    pending_file = tmp_path / "pending.json"
    assert node_sync.load_pending_convergence(path=pending_file) is None

    node_sync.record_pending_convergence("Desktop", "sha12345", "Desktop is busy", path=pending_file)
    loaded = node_sync.load_pending_convergence(path=pending_file)
    assert loaded is not None
    assert loaded["node"] == "Desktop"
    assert loaded["expected_sha"] == "sha12345"

    node_sync.clear_pending_convergence(path=pending_file)
    assert node_sync.load_pending_convergence(path=pending_file) is None


def test_find_canonical_main_checkout(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    _init_repo(primary)

    # In primary repo on main
    assert node_sync.find_canonical_main_checkout(root=primary, git=node_sync.run_git) == primary

    # Add worktree on feature branch
    wt = tmp_path / "worktree-feat"
    subprocess.run(["git", "worktree", "add", "-b", "feat-1", str(wt)], cwd=primary, check=True, capture_output=True)

    # Called from worktree -> discovers primary root on main
    found = node_sync.find_canonical_main_checkout(root=wt, git=node_sync.run_git)
    assert found == primary
