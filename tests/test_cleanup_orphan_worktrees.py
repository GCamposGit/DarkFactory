"""Daily ownerless worktree sweep (USR-500 T3)."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from core.git import ticket_workspace
from core.git.ticket_workspace import CleanupResult
from scripts import cleanup_orphan_worktrees as sweep

BORN = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> SimpleNamespace:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "--quiet", "-b", "main")
    local = tmp_path / "local"
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
    return SimpleNamespace(local=local, bare=bare, tmp=tmp_path)


def _workspace(local: Path, ticket_id: str) -> Path:
    return ticket_workspace.create_workspace(ticket_id, cwd=local, now_fn=lambda: BORN).path


def _idle(_path: Path) -> tuple[str, ...]:
    return ()


def test_sweep_delegates_removal_and_repeated_sweeps_are_idempotent(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _workspace(repo.local, "USR-601")
    now = BORN + timedelta(hours=48, seconds=1)
    real_cleanup = ticket_workspace.cleanup
    calls: list[Path] = []

    def tracked_cleanup(candidate: Path, root: Path, **kwargs: object) -> CleanupResult:
        calls.append(candidate)
        return real_cleanup(candidate, root, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ticket_workspace, "cleanup", tracked_cleanup)
    first = sweep.sweep_orphan_worktrees(
        repo.local,
        now=now,
        holder_probe=_idle,
        statuses={"USR-601": "completed"},
    )
    second = sweep.sweep_orphan_worktrees(
        repo.local,
        now=now + timedelta(minutes=1),
        holder_probe=_idle,
        statuses={"USR-601": "completed"},
    )

    assert first.removed == 1 and first.errors == first.blocked == 0
    assert calls == [path]
    assert not path.exists()
    assert second.removed == 0


@pytest.mark.parametrize("age", [timedelta(hours=48), timedelta(hours=48) - timedelta(seconds=1)])
def test_sweep_does_not_send_worktrees_at_or_under_48_hours_to_cleanup(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, age: timedelta
) -> None:
    path = _workspace(repo.local, "USR-607")
    calls: list[Path] = []

    def tracked_cleanup(candidate: Path, _root: Path, **_kwargs: object) -> CleanupResult:
        calls.append(candidate)
        return CleanupResult(path=candidate, removed=True, method="git", outcome="removed")

    monkeypatch.setattr(ticket_workspace, "cleanup", tracked_cleanup)
    summary = sweep.sweep_orphan_worktrees(
        repo.local,
        now=BORN + age,
        holder_probe=_idle,
        statuses={"USR-607": "completed"},
    )

    assert calls == []
    assert summary.ignored == 2  # primary checkout plus the young ticket worktree
    assert any(item["path"] == str(path) and item["reason"] == "too_young" for item in summary.details)
    assert path.exists()


def test_dirty_and_held_worktrees_are_reported_pending_without_removal(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    dirty = _workspace(repo.local, "USR-602")
    held = _workspace(repo.local, "USR-603")
    (dirty / "keep.txt").write_text("working copy\n", encoding="utf-8")
    now = BORN + timedelta(hours=50)

    def holder_probe(path: Path) -> tuple[str, ...]:
        return ("python (pid 42)",) if path == held else ()

    real_cleanup = ticket_workspace.cleanup
    calls: list[Path] = []

    def tracked_cleanup(candidate: Path, root: Path, **kwargs: object) -> CleanupResult:
        calls.append(candidate)
        return real_cleanup(candidate, root, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ticket_workspace, "cleanup", tracked_cleanup)
    summary = sweep.sweep_orphan_worktrees(
        repo.local,
        now=now,
        holder_probe=holder_probe,
        statuses={"USR-602": "completed", "USR-603": "cancelled"},
    )

    assert summary.blocked == 2 and summary.removed == 0 and summary.errors == 0
    assert {item["reason"] for item in summary.details if item["path"] in {str(dirty), str(held)}} == {
        "worktree_dirty",
        "live_process",
    }
    assert summary.to_dict()["preserved"] == summary.blocked + summary.ignored == 3
    assert summary.to_dict()["failures"] == 0
    assert calls == [dirty, held]
    pending = ticket_workspace.list_cleanup_pending(repo.local)
    assert {item["path"] for item in pending} == {str(dirty.resolve()), str(held.resolve())}
    messages = [record.getMessage() for record in caplog.records]
    assert any(f"path={dirty} reason=worktree_dirty" in message for message in messages)
    assert any(f"path={held} reason=live_process" in message for message in messages)
    assert (dirty / "keep.txt").is_file() and held.is_dir()


def test_pending_worktree_dirty_only_in_generated_ledgers_is_removed(repo: SimpleNamespace) -> None:
    path = _workspace(repo.local, "USR-608")
    ledger = path / ticket_workspace.TICKET_LEDGER_RELATIVE
    ledger.parent.mkdir(parents=True)
    ledger.write_text("[]\n", encoding="utf-8")
    _git(path, "add", "-f", ticket_workspace.TICKET_LEDGER_RELATIVE)
    _git(path, "commit", "--quiet", "-m", "chore: ledger")
    ledger.write_text('[{"generated": true}]\n', encoding="utf-8")
    first = ticket_workspace.cleanup(path, repo.local, holder_probe=_idle, now_fn=lambda: BORN + timedelta(hours=50))
    assert not first.removed and len(ticket_workspace.list_cleanup_pending(repo.local)) == 1

    swept = sweep.sweep_orphan_worktrees(
        repo.local, now=BORN + timedelta(hours=51), holder_probe=_idle, statuses={"USR-608": "completed"}
    )

    assert swept.removed == 1 and swept.errors == swept.blocked == 0
    assert not path.exists()
    assert ticket_workspace.list_cleanup_pending(repo.local) == []


def test_terminal_worktree_with_a_real_change_is_archived_then_removed(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = repo.tmp / "archive"
    monkeypatch.setattr(ticket_workspace, "worktree_archive_root", lambda: archive)
    path = _workspace(repo.local, "USR-610")
    (path / "README.md").write_text("changed\n", encoding="utf-8")
    (path / "new.txt").write_text("untracked\n", encoding="utf-8")

    young = sweep.sweep_orphan_worktrees(
        repo.local, now=BORN + timedelta(days=3), holder_probe=_idle, statuses={"USR-610": "completed"}
    )
    assert young.removed == 0 and path.exists() and not archive.exists()

    old = sweep.sweep_orphan_worktrees(
        repo.local, now=BORN + timedelta(days=8), holder_probe=_idle, statuses={"USR-610": "completed"}
    )

    assert old.removed == 1 and old.errors == 0
    assert not path.exists()
    (saved,) = list(archive.iterdir())
    assert "+changed" in (saved / "changes.patch").read_text(encoding="utf-8")
    assert (saved / "untracked" / "new.txt").read_text(encoding="utf-8") == "untracked\n"


def test_dirty_worktree_for_an_active_ticket_is_not_sent_to_cleanup(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _workspace(repo.local, "USR-609")
    (path / "keep.txt").write_text("working copy\n", encoding="utf-8")

    def forbidden_cleanup(*_args: object, **_kwargs: object) -> CleanupResult:
        raise AssertionError("an active ticket must not reach cleanup")

    monkeypatch.setattr(ticket_workspace, "cleanup", forbidden_cleanup)
    summary = sweep.sweep_orphan_worktrees(
        repo.local,
        now=BORN + timedelta(hours=50),
        holder_probe=_idle,
        statuses={"USR-609": "implementing"},
    )

    assert summary.ignored == 2
    assert any(item["path"] == str(path) and item["reason"] == "open_ticket" for item in summary.details)
    assert ticket_workspace.list_cleanup_pending(repo.local) == []
    assert (path / "keep.txt").is_file()


def test_active_ticket_from_demands_ledger_is_ignored(repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _workspace(repo.local, "USR-604")
    ledger = repo.local / ".factory" / "demands" / "demands.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"demands": [{"id": "USR-604", "status": "implementing"}]}), encoding="utf-8")

    def forbidden_cleanup(*_args: object, **_kwargs: object) -> CleanupResult:
        raise AssertionError("a worktree with an active owner must not be cleaned")

    monkeypatch.setattr(ticket_workspace, "cleanup", forbidden_cleanup)
    summary = sweep.sweep_orphan_worktrees(
        repo.local,
        now=BORN + timedelta(hours=50),
        holder_probe=_idle,
        queue_lookup=lambda _ticket_id: None,
    )

    assert summary.ignored == 2  # primary checkout plus the open-ticket worktree
    assert any(item["path"] == str(path) and item["reason"] == "open_ticket" for item in summary.details)
    assert path.exists()


def test_one_classification_failure_does_not_stop_later_candidates(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = _workspace(repo.local, "USR-605")
    healthy = _workspace(repo.local, "USR-606")
    real_classify = ticket_workspace.classify_ownerless_worktree

    def isolated_classify(path: Path, **kwargs: object):
        if path == broken:
            raise OSError("synthetic status read failure")
        return real_classify(path, **kwargs)  # type: ignore[arg-type]

    cleaned: list[Path] = []

    def cleanup(candidate: Path, root: Path, **_kwargs: object) -> CleanupResult:
        cleaned.append(candidate)
        return CleanupResult(path=candidate, removed=True, method="git", outcome="removed")

    monkeypatch.setattr(ticket_workspace, "classify_ownerless_worktree", isolated_classify)
    monkeypatch.setattr(ticket_workspace, "cleanup", cleanup)
    summary = sweep.sweep_orphan_worktrees(
        repo.local,
        now=BORN + timedelta(hours=50),
        holder_probe=_idle,
        statuses={"USR-605": "completed", "USR-606": "completed"},
    )

    assert summary.errors == 1 and summary.removed == 1
    assert cleaned == [healthy]
    assert any(item["path"] == str(broken) and item["reason"] == "classification_error" for item in summary.details)


def test_scheduler_guard_runs_daily_and_skips_repeated_calls(tmp_path: Path) -> None:
    start = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    runs: list[Path] = []

    def fake_sweep(root: Path) -> sweep.CleanupSummary:
        runs.append(root)
        return sweep.CleanupSummary(ignored=1)

    assert sweep.maybe_run_scheduled(tmp_path, now_fn=lambda: start, sweep_fn=fake_sweep) is not None
    assert sweep.maybe_run_scheduled(tmp_path, now_fn=lambda: start, sweep_fn=fake_sweep) is None
    assert sweep.maybe_run_scheduled(
        tmp_path, now_fn=lambda: start + timedelta(days=1) - timedelta(seconds=1), sweep_fn=fake_sweep
    ) is None
    assert sweep.maybe_run_scheduled(
        tmp_path, now_fn=lambda: start + timedelta(days=1), sweep_fn=fake_sweep
    ) is not None
    assert runs == [tmp_path, tmp_path]


def test_scheduler_serializes_active_sweep_and_retries_after_failure_backoff(tmp_path: Path) -> None:
    started = Event()
    release = Event()
    start = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)

    def interrupted(_root: Path) -> sweep.CleanupSummary:
        started.set()
        assert release.wait(timeout=10)
        raise RuntimeError("interrupted sweep")

    worker = Thread(
        target=sweep.maybe_run_scheduled,
        args=(tmp_path,),
        kwargs={"sweep_fn": interrupted, "now_fn": lambda: start},
    )
    worker.start()
    try:
        assert started.wait(timeout=5)
        assert sweep.maybe_run_scheduled(tmp_path, force=True) is None
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert sweep.maybe_run_scheduled(
        tmp_path,
        now_fn=lambda: start + timedelta(hours=1) - timedelta(seconds=1),
        sweep_fn=lambda _: sweep.CleanupSummary(),
    ) is None
    assert sweep.maybe_run_scheduled(
        tmp_path,
        now_fn=lambda: start + timedelta(hours=1),
        sweep_fn=lambda _: sweep.CleanupSummary(),
    ) is not None


def test_repeated_sweep_failures_are_backed_off_and_recorded(tmp_path: Path) -> None:
    start = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    launches: list[Path] = []

    def failed_sweep(root: Path) -> sweep.CleanupSummary:
        launches.append(root)
        raise OSError("git inventory unavailable")

    first = sweep.maybe_run_scheduled(tmp_path, now_fn=lambda: start, sweep_fn=failed_sweep)
    assert sweep.maybe_run_scheduled(
        tmp_path, now_fn=lambda: start + timedelta(hours=1) - timedelta(seconds=1), sweep_fn=failed_sweep
    ) is None
    second_failure_time = start + timedelta(hours=1)
    second = sweep.maybe_run_scheduled(tmp_path, now_fn=lambda: second_failure_time, sweep_fn=failed_sweep)
    assert sweep.maybe_run_scheduled(
        tmp_path, now_fn=lambda: second_failure_time + timedelta(hours=2) - timedelta(seconds=1), sweep_fn=failed_sweep
    ) is None
    state = json.loads((tmp_path / sweep.SCHEDULE_STATE).read_text(encoding="utf-8"))

    assert first is not None and first.errors == 1
    assert second is not None and second.errors == 1
    assert launches == [tmp_path, tmp_path]
    assert state["status"] == "failed"
    assert state["failure_count"] == 2
    assert state["last_failure"] == "OSError: git inventory unavailable"
    assert state["next_attempt_at"] == (second_failure_time + timedelta(hours=2)).isoformat()


def test_failure_backoff_caps_at_one_day_and_success_resets_to_daily_schedule(tmp_path: Path) -> None:
    start = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    state_path = tmp_path / sweep.SCHEDULE_STATE
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"status": "failed", "failure_count": 8}), encoding="utf-8")

    def failed(_root: Path) -> sweep.CleanupSummary:
        raise OSError("inventory unavailable")

    assert sweep.maybe_run_scheduled(
        tmp_path, now_fn=lambda: start, sweep_fn=failed
    ) is not None
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["failure_count"] == 9
    assert state["next_attempt_at"] == (start + timedelta(hours=24)).isoformat()

    resumed = start + timedelta(hours=24)
    assert sweep.maybe_run_scheduled(
        tmp_path, now_fn=lambda: resumed, sweep_fn=lambda _: sweep.CleanupSummary(errors=1)
    ) is not None
    recovered = json.loads(state_path.read_text(encoding="utf-8"))
    assert recovered["status"] == "succeeded"
    assert recovered["failure_count"] == 0
    assert "next_attempt_at" not in recovered
    assert sweep.maybe_run_scheduled(
        tmp_path, now_fn=lambda: resumed + timedelta(days=1) - timedelta(seconds=1),
        sweep_fn=lambda _: sweep.CleanupSummary(),
    ) is None
    assert sweep.maybe_run_scheduled(
        tmp_path, now_fn=lambda: resumed + timedelta(days=1), sweep_fn=lambda _: sweep.CleanupSummary()
    ) is not None


def test_legacy_last_attempt_state_keeps_daily_interval(tmp_path: Path) -> None:
    from core.git import orphan_worktree_cleanup as scheduler

    start = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    state_path = tmp_path / sweep.SCHEDULE_STATE
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"last_attempt_at": start.isoformat()}), encoding="utf-8")

    assert not scheduler.schedule_due(tmp_path, start + timedelta(hours=23))
    assert scheduler.schedule_due(tmp_path, start + timedelta(days=1))


def test_summary_errors_complete_sweep_on_daily_cadence(tmp_path: Path) -> None:
    from core.git import orphan_worktree_cleanup as scheduler

    start = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    partial = sweep.CleanupSummary(errors=1, details=[{"path": "worktree", "reason": "classification_error"}])
    assert sweep.maybe_run_scheduled(tmp_path, now_fn=lambda: start, sweep_fn=lambda _: partial) is partial
    state = json.loads((tmp_path / sweep.SCHEDULE_STATE).read_text(encoding="utf-8"))
    assert state["status"] == "succeeded"
    assert state["failure_count"] == 0
    assert not scheduler.schedule_due(tmp_path, start + timedelta(hours=1))

