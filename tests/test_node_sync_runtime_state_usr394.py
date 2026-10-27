"""USR-394: local runtime-state edits (owner_actions.json) must not abort the node update."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.infra.node_sync import restore_runtime_state, run_git, stash_runtime_state

REL = ".factory/owner_actions/owner_actions.json"


def _g(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _commit(repo: Path, content: str, other: str = "x") -> None:
    (repo / REL).parent.mkdir(parents=True, exist_ok=True)
    (repo / REL).write_text(content, encoding="utf-8")
    (repo / "other.txt").write_text(other, encoding="utf-8")
    _g(repo, "add", "-A")
    _g(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")


@pytest.fixture()
def pair(tmp_path: Path) -> tuple[Path, Path]:
    up = tmp_path / "up"
    up.mkdir()
    _g(up, "init", "-q", "-b", "main")
    _commit(up, "base")
    node = tmp_path / "node"
    subprocess.run(["git", "clone", "-q", str(up), str(node)], check=True, capture_output=True)
    return up, node


def _pull(node: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "pull", "-q"], cwd=node, capture_output=True, text=True)


def test_unchanged_upstream_keeps_local_state(pair):
    up, node = pair
    _commit(up, "base", other="y")
    (node / REL).write_text("local", encoding="utf-8")
    saved = stash_runtime_state(node)
    assert _pull(node).returncode == 0
    restore_runtime_state(node, saved)
    assert (node / REL).read_text(encoding="utf-8") == "local"
    assert (node / "other.txt").read_text(encoding="utf-8") == "y"


def test_changed_upstream_wins_and_local_copy_saved(pair):
    up, node = pair
    _commit(up, "upstream-new")
    (node / REL).write_text("local", encoding="utf-8")
    assert _pull(node).returncode != 0  # the collision of USR-394
    saved = stash_runtime_state(node)
    assert _pull(node).returncode == 0
    notes = restore_runtime_state(node, saved)
    assert (node / REL).read_text(encoding="utf-8") == "upstream-new"
    copies = list((node / ".factory/runs/node_sync_conflicts").glob("*-owner_actions.json"))
    assert len(copies) == 1 and copies[0].read_text(encoding="utf-8") == "local"
    assert notes


def test_unknown_dirty_file_still_aborts(pair):
    up, node = pair
    _commit(up, "base", other="y")
    (node / "other.txt").write_text("mine", encoding="utf-8")
    stash_runtime_state(node)
    assert _pull(node).returncode != 0
    assert run_git(node, ["status", "--porcelain", "--", "other.txt"])
