"""USR-312: owner_actions.json conflicts are auto-merged by id during the git sync."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from core.git.autonomy import GitAutonomyManager
from core.owner_actions.merge import merge_owner_actions_3way

OA_REL = ".factory/owner_actions/owner_actions.json"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, encoding="utf-8", check=True
    )


def _row(action_id: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"id": action_id, "title": f"titulo {action_id}", "status": "open"}
    row.update(extra)
    return row


def _dump(rows: list[dict[str, Any]]) -> str:
    doc = {"schema_version": 1, "description": "acoes (teste)", "actions": rows}
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Callable[[list[dict[str, Any]]], None]]:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "--quiet", "-b", "main")
    local = tmp_path / "local"
    local.mkdir()
    _git(local, "init", "--quiet", "-b", "main")
    _git(local, "config", "user.name", "DarkFac Test")
    _git(local, "config", "user.email", "test@darkfac.internal")
    _git(local, "config", "commit.gpgsign", "false")
    _git(local, "remote", "add", "origin", str(bare))
    target = local / OA_REL
    target.parent.mkdir(parents=True)
    target.write_text(_dump([_row("OA-001"), _row("OA-002")]), encoding="utf-8")
    _git(local, "add", "-A")
    _git(local, "commit", "--quiet", "-m", "base")
    _git(local, "push", "--quiet", "-u", "origin", "main")
    _git(local, "checkout", "--quiet", "-b", "ticket/usr-x")

    def main_commit(rows: list[dict[str, Any]]) -> None:
        _git(local, "checkout", "--quiet", "main")
        target.write_text(_dump(rows), encoding="utf-8")
        _git(local, "commit", "--quiet", "-am", "main change")
        _git(local, "push", "--quiet", "origin", "main")
        _git(local, "checkout", "--quiet", "ticket/usr-x")

    return local, main_commit


def _branch_commit(local: Path, rows: list[dict[str, Any]]) -> None:
    (local / OA_REL).write_text(_dump(rows), encoding="utf-8")
    _git(local, "commit", "--quiet", "-am", "branch change")


def _actions(local: Path) -> list[dict[str, Any]]:
    return json.loads((local / OA_REL).read_text(encoding="utf-8"))["actions"]


@pytest.mark.parametrize("preserve", [True, False])
def test_new_actions_on_both_sides_are_kept(repo, preserve: bool) -> None:
    local, main_commit = repo
    _branch_commit(local, [_row("OA-001"), _row("OA-002"), _row("OA-003", title="da branch")])
    main_commit([_row("OA-001"), _row("OA-002"), _row("OA-004", title="do watchdog")])
    ok, files, _ = GitAutonomyManager(local)._sync_with_base("main", local, preserve_commits=preserve)
    assert ok and files == []
    actions = _actions(local)
    assert [a["id"] for a in actions] == ["OA-001", "OA-002", "OA-004", "OA-003"]
    assert (local / OA_REL).read_text(encoding="utf-8").endswith("}\n")
    assert _git_clean(local)


def test_status_change_on_one_side_survives_new_item_on_other(repo) -> None:
    local, main_commit = repo
    done = _row("OA-002", status="done", resolved_at="2026-10-18T10:00:00Z")
    _branch_commit(local, [_row("OA-001"), done])
    main_commit([_row("OA-001"), _row("OA-002"), _row("OA-003")])
    ok, files, _ = GitAutonomyManager(local)._sync_with_base("main", local, preserve_commits=True)
    assert ok and files == []
    actions = _actions(local)
    assert [a["id"] for a in actions] == ["OA-001", "OA-002", "OA-003"]
    assert actions[1] == done


def test_same_item_changed_differently_is_a_real_conflict(repo) -> None:
    local, main_commit = repo
    _branch_commit(local, [_row("OA-001", status="done"), _row("OA-002")])
    main_commit([_row("OA-001", status="blocked"), _row("OA-002")])
    ok, files, msg = GitAutonomyManager(local)._sync_with_base("main", local, preserve_commits=True)
    assert not ok
    assert files == [OA_REL]
    assert "OA-001" in msg
    assert _git_clean(local)  # merge aborted


def test_id_collision_renumbers_ours(repo) -> None:
    local, main_commit = repo
    _branch_commit(local, [_row("OA-001"), _row("OA-002"), _row("OA-003", title="minha")])
    main_commit([_row("OA-001"), _row("OA-002"), _row("OA-003", title="do main"), _row("OA-004")])
    ok, files, _ = GitAutonomyManager(local)._sync_with_base("main", local, preserve_commits=True)
    assert ok and files == []
    actions = _actions(local)
    ids = [a["id"] for a in actions]
    assert len(ids) == len(set(ids)) == 5
    by_id = {a["id"]: a for a in actions}
    assert by_id["OA-003"]["title"] == "do main"
    assert by_id["OA-004"]["title"] == "titulo OA-004"
    assert by_id["OA-005"]["title"] == "minha"


def test_merge_function_rejects_invalid_json() -> None:
    ok, text, err = merge_owner_actions_3way("{}", "nao json", "{}")
    assert not ok and text == "" and "ours" in err


def _git_clean(local: Path) -> bool:
    out = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=str(local), capture_output=True, text=True
    ).stdout
    return out.strip() == ""
