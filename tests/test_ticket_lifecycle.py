"""Ticket terminal transitions clean their linked worktrees safely (USR-500 T2)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.git import ticket_workspace
from core.git.autonomy import GitAutonomyManager
from core.roadmap.models import DeliveryStatus


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return result.stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "--quiet", "-b", "main")
    local = tmp_path / "checkout"
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
    return local


def _ticket(ticket_id: str, status: DeliveryStatus = DeliveryStatus.PLANNED) -> UserTicket:
    return UserTicket(id=ticket_id, title="Ticket lifecycle", status=status)


def test_terminal_status_cleans_its_clean_worktree(checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = ticket_workspace.create_workspace("USR-500-T2", cwd=checkout)
    monkeypatch.setattr(ticket_workspace, "_find_holders", lambda _path: [])
    store = DemandsStore(checkout / ".factory" / "demands" / "demands.json")
    store.save_ticket(_ticket("USR-500-T2"))

    closed = store.update_status("USR-500-T2", DeliveryStatus.CANCELLED)

    assert closed.status == DeliveryStatus.CANCELLED
    assert not workspace.path.exists()


def test_blocked_cleanup_preserves_worktree_and_records_reason(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    workspace = ticket_workspace.create_workspace("USR-500-T2", cwd=checkout)
    monkeypatch.setattr(ticket_workspace, "_find_holders", lambda _path: ["python (pid 42)"])
    store = DemandsStore(checkout / ".factory" / "demands" / "demands.json")
    store.save_ticket(_ticket("USR-500-T2"))

    closed = store.update_status("USR-500-T2", DeliveryStatus.CANCELLED)

    assert closed.status == DeliveryStatus.CANCELLED
    assert store.get_ticket("USR-500-T2").status == DeliveryStatus.CANCELLED
    assert workspace.path.is_dir()
    assert "live_process" in caplog.text
    assert "python (pid 42)" in caplog.text


def test_repeated_terminal_event_is_idempotent(checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = ticket_workspace.create_workspace("USR-500-T2", cwd=checkout)
    monkeypatch.setattr(ticket_workspace, "_find_holders", lambda _path: [])
    store = DemandsStore(checkout / ".factory" / "demands" / "demands.json")
    store.save_ticket(_ticket("USR-500-T2"))

    first = store.update_status("USR-500-T2", DeliveryStatus.CANCELLED)
    second = store.update_status("USR-500-T2", DeliveryStatus.CANCELLED)

    assert first.status == second.status == DeliveryStatus.CANCELLED
    assert not workspace.path.exists()
    assert store.get_ticket("USR-500-T2").status == DeliveryStatus.CANCELLED


def test_complete_ticket_cleans_worktree_when_delivery_is_not_requested(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = ticket_workspace.create_workspace("USR-500-T2", cwd=checkout)
    monkeypatch.setattr(ticket_workspace, "_find_holders", lambda _path: [])
    store = DemandsStore(workspace.path / ".factory" / "demands" / "demands.json")
    store.save_ticket(_ticket("USR-500-T2"))
    (workspace.path / "implementation.py").write_text("value = 1\n", encoding="utf-8")
    manager = GitAutonomyManager(checkout, cleanup_sleep_fn=lambda _seconds: None)

    report = manager.complete_ticket("USR-500-T2", cwd=workspace.path, auto_push=False)

    assert report.ok, report.message
    assert not workspace.path.exists()
    committed_ledger = json.loads(
        _git(checkout, "show", f"{workspace.branch}:.factory/demands/demands.json")
    )
    assert committed_ledger[0]["status"] == DeliveryStatus.COMPLETED.value
