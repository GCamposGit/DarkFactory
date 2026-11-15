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
    disabled = xml.replace("<Hidden>true</Hidden><Enabled>true</Enabled>", "<Hidden>true</Hidden><Enabled>false</Enabled>")
    assert decide_sync_task(inst.parse_task_xml(disabled), MAIN, MAIN).action is SyncTaskAction.UPDATE


def test_publish_once_reads_files_without_probes_and_returns_receipt(tmp_path, monkeypatch) -> None:
    import json
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from core.usage import sync_task

    directory = tmp_path / ".factory" / "usage" / "providers"
    directory.mkdir(parents=True)
    checked = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    (directory / "anthropic.json").write_text(json.dumps({"provider_id": "anthropic", "checked_at": checked}), encoding="utf-8")
    monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", "test-key")
    def forbidden(*a, **kw):
        raise AssertionError("publication must never inspect or probe")
    monkeypatch.setattr("core.usage.monitor.AccountUsageMonitor.inspect", forbidden)
    monkeypatch.setattr("core.usage.api_credits.ApiCreditsMonitor.generate_report", forbidden)
    calls = []
    def post(*a, **kw):
        calls.append(kw)
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr("httpx.post", post)
    result = sync_task.publish_once(tmp_path, target_url="https://test.invalid")
    assert result.ok and result.http_status == 200
    assert result.oldest_snapshot_age_seconds >= 1800
    assert calls[0]["timeout"] <= 6
    assert calls[0]["json"]["accounts"][0]["checked_at"] == checked


def test_publish_once_never_raises_or_leaks_credentials(tmp_path, monkeypatch) -> None:
    from core.usage import sync_task
    monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", "secret-should-not-leak")
    def fail(*a, **kw):
        raise RuntimeError("secret-should-not-leak")
    monkeypatch.setattr(sync_task, "collect_local_usage_payload", fail)
    result = sync_task.publish_once(tmp_path)
    assert not result.ok and result.error == "RuntimeError"
    assert "secret-should-not-leak" not in result.model_dump_json()
