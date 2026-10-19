"""Decisao pura da tarefa DarkFac-Usage-Cloud-Sync (USR-236); independe de Windows."""

from __future__ import annotations

import sys
from pathlib import Path

from core.usage.sync_task import (
    SyncTaskAction,
    SyncTaskState,
    decide_sync_task,
    is_worktree_path,
)

MAIN = "C:/dev/DarkFac"
GOOD = SyncTaskState(r"C:\dev\DarkFac\scripts\sync_usage_to_cloud.py", r"C:\dev\DarkFac")


def test_absent_creates() -> None:
    assert decide_sync_task(None, MAIN, MAIN).action is SyncTaskAction.CREATE


def test_equal_keeps_even_with_case_and_slashes() -> None:
    cur = SyncTaskState("c:/dev/darkfac/scripts/sync_usage_to_cloud.py", "C:/dev/DarkFac/")
    assert decide_sync_task(cur, MAIN, MAIN).action is SyncTaskAction.KEEP
    assert decide_sync_task(GOOD, MAIN, MAIN).action is SyncTaskAction.KEEP


def test_worktree_target_updates() -> None:
    cur = SyncTaskState(r"C:\dev\DarkFac\.codex\worktrees\abc\scripts\sync_usage_to_cloud.py", "")
    d = decide_sync_task(cur, MAIN, MAIN)
    assert d.action is SyncTaskAction.UPDATE and "worktree" in d.reason


def test_other_path_updates() -> None:
    cur = SyncTaskState(r"D:\other\scripts\sync_usage_to_cloud.py", r"D:\other")
    assert decide_sync_task(cur, MAIN, MAIN).action is SyncTaskAction.UPDATE


def test_wrong_workdir_updates() -> None:
    cur = SyncTaskState(GOOD.script_path, r"D:\other")
    assert decide_sync_task(cur, MAIN, MAIN).action is SyncTaskAction.UPDATE


def test_refuses_when_execution_root_is_worktree() -> None:
    for wt in (
        r"C:\dev\DarkFac\.worktrees\usr-1",
        "C:/dev/DarkFac/.claude/worktrees/x",
        "C:/dev/DarkFac/.codex/worktrees/y",
    ):
        assert decide_sync_task(GOOD, MAIN, wt).action is SyncTaskAction.REFUSE
        assert decide_sync_task(None, MAIN, wt).action is SyncTaskAction.REFUSE


def test_refuses_when_desired_root_is_worktree() -> None:
    d = decide_sync_task(None, "C:/dev/DarkFac/.worktrees/a", MAIN)
    assert d.action is SyncTaskAction.REFUSE


def test_is_worktree_path_not_fooled_by_substring() -> None:
    assert not is_worktree_path("C:/dev/my.worktrees2/repo")
    assert is_worktree_path("C:/dev/DarkFac/.worktrees")


def test_installer_xml_roundtrip() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import install_usage_sync_task as inst

    xml = inst.build_task_xml(Path("C:/dev/DarkFac"), "https://x", "me", "C:/py/pythonw.exe")
    state = inst.parse_task_xml(xml)
    assert state is not None
    assert decide_sync_task(state, MAIN, MAIN).action is SyncTaskAction.KEEP
