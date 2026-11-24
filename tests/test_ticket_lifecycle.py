"""Ticket terminal transitions clean their linked worktrees safely (USR-500 T2)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.git import ticket_workspace
from core.git.autonomy import DeliveryReport, GitAutonomyManager
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
    calls: list[tuple[Path, Path]] = []
    real_cleanup = ticket_workspace.cleanup

    def tracked_cleanup(path: Path, main_root: Path, **kwargs: object) -> ticket_workspace.CleanupResult:
        calls.append((path.resolve(), main_root.resolve()))
        return real_cleanup(path, main_root, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ticket_workspace, "cleanup", tracked_cleanup)
    store = DemandsStore(checkout / ".factory" / "demands" / "demands.json")
    store.save_ticket(_ticket("USR-500-T2"))

    closed = store.update_status("USR-500-T2", DeliveryStatus.CANCELLED)

    assert closed.status == DeliveryStatus.CANCELLED
    assert not workspace.path.exists()
    assert calls == [(workspace.path.resolve(), checkout.resolve())]


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
    pending = ticket_workspace.list_cleanup_pending(checkout)
    assert len(pending) == 1
    assert Path(pending[0]["path"]) == workspace.path.resolve()
    assert pending[0]["reason"] == "live_process"


def test_dirty_worktree_is_preserved_when_ticket_closes(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    workspace = ticket_workspace.create_workspace("USR-500-T2", cwd=checkout)
    monkeypatch.setattr(ticket_workspace, "_find_holders", lambda _path: [])
    (workspace.path / "README.md").write_text("uncommitted work\n", encoding="utf-8")
    store = DemandsStore(checkout / ".factory" / "demands" / "demands.json")
    store.save_ticket(_ticket("USR-500-T2"))

    closed = store.update_status("USR-500-T2", DeliveryStatus.CANCELLED)

    assert closed.status == DeliveryStatus.CANCELLED
    assert store.get_ticket("USR-500-T2").status == DeliveryStatus.CANCELLED
    assert workspace.path.is_dir()
    assert (workspace.path / "README.md").read_text(encoding="utf-8") == "uncommitted work\n"
    assert "worktree_dirty" in caplog.text


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


def test_terminal_status_without_worktree_is_ok(checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_cleanup(*_args: object, **_kwargs: object) -> ticket_workspace.CleanupResult:
        raise AssertionError("a ticket without a worktree must not attempt removal")

    monkeypatch.setattr(ticket_workspace, "cleanup", forbidden_cleanup)
    store = DemandsStore(checkout / ".factory" / "demands" / "demands.json")
    store.save_ticket(_ticket("USR-500-T2"))

    closed = store.update_status("USR-500-T2", DeliveryStatus.CANCELLED)

    assert closed.status == DeliveryStatus.CANCELLED
    assert store.get_ticket("USR-500-T2").status == DeliveryStatus.CANCELLED


def test_unexpected_cleanup_error_does_not_prevent_terminal_status(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    workspace = ticket_workspace.create_workspace("USR-500-T2", cwd=checkout)
    store = DemandsStore(checkout / ".factory" / "demands" / "demands.json")
    store.save_ticket(_ticket("USR-500-T2"))

    def fail_cleanup(*_args: object, **_kwargs: object) -> ticket_workspace.CleanupResult:
        raise RuntimeError("cleanup failure")

    monkeypatch.setattr(ticket_workspace, "_cleanup_outside", fail_cleanup)

    closed = store.update_status("USR-500-T2", DeliveryStatus.CANCELLED)

    assert closed.status == DeliveryStatus.CANCELLED
    assert store.get_ticket("USR-500-T2").status == DeliveryStatus.CANCELLED
    assert workspace.path.is_dir()
    assert "cleanup failed for" in caplog.text
    assert "cleanup failure" in caplog.text
    pending = ticket_workspace.list_cleanup_pending(checkout)
    assert len(pending) == 1
    assert Path(pending[0]["path"]) == workspace.path.resolve()
    assert pending[0]["outcome"] == "error"
    assert pending[0]["reason"] == "cleanup_exception"


def test_complete_ticket_preserves_worktree_when_delivery_is_not_requested(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = ticket_workspace.create_workspace("USR-500-T2", cwd=checkout)
    monkeypatch.setattr(ticket_workspace, "_find_holders", lambda _path: [])
    calls: list[tuple[Path, Path]] = []
    real_cleanup = ticket_workspace.cleanup

    def tracked_cleanup(path: Path, main_root: Path, **kwargs: object) -> ticket_workspace.CleanupResult:
        calls.append((path.resolve(), main_root.resolve()))
        return real_cleanup(path, main_root, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ticket_workspace, "cleanup", tracked_cleanup)
    store = DemandsStore(workspace.path / ".factory" / "demands" / "demands.json")
    store.save_ticket(_ticket("USR-500-T2"))
    (workspace.path / "implementation.py").write_text("value = 1\n", encoding="utf-8")
    manager = GitAutonomyManager(checkout, cleanup_sleep_fn=lambda _seconds: None)

    report = manager.complete_ticket("USR-500-T2", cwd=workspace.path, auto_push=False)

    assert not report.ok and report.cause == "merge_evidence_missing"
    assert workspace.path.is_dir()
    assert calls == []
    committed_ledger = json.loads(
        _git(checkout, "show", f"{workspace.branch}:.factory/demands/demands.json")
    )
    assert committed_ledger[0]["status"] == DeliveryStatus.PLANNED.value


def test_complete_ticket_does_not_repeat_a_blocked_delivery_cleanup(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = ticket_workspace.create_workspace("USR-500-T2", cwd=checkout)
    store = DemandsStore(workspace.path / ".factory" / "demands" / "demands.json")
    store.save_ticket(_ticket("USR-500-T2"))
    _git(workspace.path, "add", ".factory/demands/demands.json")
    _git(workspace.path, "commit", "--quiet", "-m", "chore: record terminal ticket")
    manager = GitAutonomyManager(checkout)
    calls: list[tuple[Path, Path]] = []
    real_cleanup = ticket_workspace.cleanup

    def tracked_cleanup(path: Path, main_root: Path, **kwargs: object) -> ticket_workspace.CleanupResult:
        calls.append((path.resolve(), main_root.resolve()))
        kwargs["holder_probe"] = lambda _path: ("python (pid 42)",)
        return real_cleanup(path, main_root, **kwargs)  # type: ignore[arg-type]

    def deliver_with_blocked_cleanup(
        _ticket_id: str,
        _title: str,
        _project_id: str,
        target_dir: Path,
        _gh_runner: object,
        _commit_sha: str | None,
    ) -> tuple[None, DeliveryReport, None]:
        ticket_workspace.cleanup(target_dir, checkout)
        return None, DeliveryReport(
            ok=True,
            action="merged",
            merge_sha=_git(target_dir, "rev-parse", "HEAD"),
            cleaned=False,
            worktree_cleanup_attempted=True,
            branch=workspace.branch,
        ), None

    monkeypatch.setattr(ticket_workspace, "cleanup", tracked_cleanup)
    monkeypatch.setattr(manager, "_inspect_completion_changes", lambda *_args: None)
    monkeypatch.setattr(manager, "_commit_completion", lambda *_args: ("abc123", None))
    monkeypatch.setattr(manager, "_deliver_completion", deliver_with_blocked_cleanup)
    monkeypatch.setattr(manager, "_verify_live_completion", lambda *_args: None)

    report = manager.complete_ticket("USR-500-T2", cwd=workspace.path, auto_push=True)

    assert report.ok, report.message
    assert calls == [(workspace.path.resolve(), checkout.resolve())]
    assert workspace.path.is_dir()
    assert ticket_workspace.list_cleanup_pending(checkout)[0]["reason"] == "live_process"
