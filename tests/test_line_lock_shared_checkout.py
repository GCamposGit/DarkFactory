"""Regressao USR-250: a trava de linha nao pode sujar o checkout compartilhado."""

from __future__ import annotations

import subprocess
from pathlib import Path

import run_ticket
from core.git.ticket_workspace import shared_checkout_dirty_paths
from core.line import line_lock

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_gitignore_lists_line_lock_and_local_cancel() -> None:
    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".factory/line_lock/" in lines
    assert ".factory/local_cancel/" in lines
    assert ".factory/line_lock/" in run_ticket.VOLATILE_STATE_PREFIXES


def test_held_line_lock_does_not_dirty_shared_checkout(tmp_path: Path, monkeypatch) -> None:
    # The gate itself may run as a delivery subprocess with the inheritance env var already set
    # (USR-250); without clearing it here, guard() would treat the lock as already held by an
    # ancestor and skip creating it, failing the assertions below for a reason unrelated to the code.
    monkeypatch.delenv(line_lock.ENV_INHERITED, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text((REPO_ROOT / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "init")
    state = repo / ".factory"
    state.mkdir()
    with line_lock.guard("USR-TEST", root=state):
        assert (state / "line_lock" / "line.lock").exists()
        (state / "local_cancel").mkdir()
        (state / "local_cancel" / "USR-TEST.cancel").write_text("x", encoding="utf-8")
        assert shared_checkout_dirty_paths(repo) == []
        assert shared_checkout_dirty_paths(repo, ignore_prefixes=run_ticket.VOLATILE_STATE_PREFIXES) == []
