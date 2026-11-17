from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.line import fleet_dispatcher as fd
from core.line import pending_delivery as pending
from core.line.admission_store import AdmissionStore


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, capture_output=True, text=True, encoding="utf-8",
    )


def _commit_file(repo: Path, content: str, message: str) -> None:
    target = repo / "docs" / "manual" / "motor.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", "docs/manual/motor.md")
    _git(repo, "commit", "-m", message)


def _conflicted_worktree(tmp_path: Path) -> tuple[Path, str, bytes]:
    repo = tmp_path / "ticket-worktree"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Dispatcher Test")
    _git(repo, "config", "user.email", "dispatcher@darkfac.local")
    _commit_file(repo, "## Motor\nbase\n", "base")
    _git(repo, "checkout", "-b", "ticket/usr-475-conflict")
    _commit_file(repo, "## Motor\nticket candidate\n", "ticket candidate")
    _git(repo, "checkout", "main")
    _commit_file(repo, "## Motor\nmain version\n", "main version")
    _git(repo, "checkout", "ticket/usr-475-conflict")
    result = _git(repo, "rebase", "main", check=False)
    assert result.returncode != 0
    candidate = _git(repo, "rev-parse", "HEAD").stdout.strip()
    conflicted_file = (repo / "docs" / "manual" / "motor.md").read_bytes()
    assert b"<<<<<<<" in conflicted_file
    return repo, candidate, conflicted_file


def test_terminal_proof_precedes_stale_pid_failure_and_blocked_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    path = tmp_path / "pending" / "USR-475-OLD.json"
    row = pending.PendingDelivery(
        ticket_id="USR-475-OLD", worktree=str(tmp_path), stage="development",
        pid=91731, started_at=10, status="blocked", cause="launcher_exit:1",
    )
    pending._save(path, row)
    monkeypatch.setattr(pending, "_children", {})
    monkeypatch.setattr(pending, "_pid_running", lambda _row: False)
    monkeypatch.setattr(pending, "_ticket_status", lambda _ticket: "cancelled")
    monkeypatch.setattr(
        pending, "_child_finished",
        lambda *_args: pytest.fail("terminal proof must be reconciled before stale PID failure"),
    )

    result = pending._tick_row(path, tmp_path, 100)

    assert result == {
        "ticket": "USR-475-OLD", "status": "terminal", "proof": "cancelled", "stage": "development",
    }
    assert not path.exists()
    reconciliation = path.parent / "reconciliations" / "USR-475-OLD.json"
    assert reconciliation.is_file()


def test_git_conflict_survives_restarts_while_independent_ticket_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_800_000_000.0
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    repo, candidate, conflicted_file = _conflicted_worktree(tmp_path)
    pending_dir = tmp_path / "pending_delivery"
    pending_path = pending_dir / "USR-475-CONFLICT.json"
    pending._save(pending_path, pending.PendingDelivery(
        ticket_id="USR-475-CONFLICT", worktree=str(repo), stage="development",
        status="blocked", cause="technical_conflict_exhausted",
    ))
    monkeypatch.setattr(pending, "_children", {})
    monkeypatch.setattr(pending, "_ticket_status", lambda _ticket: "planned")
    monkeypatch.setattr(pending.run_outcome, "main_sha", lambda _repo: "a" * 40)
    monkeypatch.setattr("core.line.line_lock.is_locked", lambda: False)

    class PendingBudget:
        def budget(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {"next_attempt_at": 0, "main_sha": "a" * 40, "infra_streak": 0}

        def retry_reason(self, *_args: object, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr(pending, "_budget_store", lambda *_args, **_kwargs: PendingBudget())

    demands = [
        {"id": "USR-475-CONFLICT", "status": "planned", "created_at": "2026-01-01",
         "candidate_digest": "conflict-candidate"},
        {"id": "USR-475-INDEPENDENT", "status": "planned", "created_at": "2026-01-02",
         "candidate_digest": "independent-candidate"},
    ]
    host_free = False
    launched: list[str] = []

    class Child:
        rc: int | None = None

        def poll(self) -> int | None:
            return self.rc

    child = Child()
    log_path = tmp_path / "independent.log"

    def launch(ticket_id: str, host: fd.HostSpec, *, run_id: str | None = None,
               candidate_digest: str = "unknown", **_kwargs: object) -> fd.InFlight:
        assert ticket_id == "USR-475-INDEPENDENT"
        launched.append(ticket_id)
        return fd.InFlight(
            ticket_id, host.id, child, log_path=log_path, run_id=run_id,
            candidate_digest=candidate_digest,
        )

    def new_dispatcher() -> fd.FleetDispatcher:
        admission = AdmissionStore(
            tmp_path / "dispatcher-admission.sqlite3",
            clock=lambda: datetime.fromtimestamp(now, timezone.utc),
        )
        dispatcher = fd.FleetDispatcher(
            fd.DispatcherConfig(
                hosts=[fd.HostSpec("grokbox", 1, "http://worker")],
                state_path=tmp_path / "dispatcher-state.json", deploy_window_sec=0,
            ),
            demands_loader=lambda: demands,
            probe=lambda _host: fd.HostProbe(host_free, "ready"),
            launch=launch,
            deploy=lambda: 0,
            clock=lambda: now,
            admission_store=admission,
        )
        # Exercise the durable pending queue even though this test injects the independent worker.
        dispatcher._resumptions = lambda: pending.tick(repo, directory=pending_dir, now=now)
        return dispatcher

    first = new_dispatcher()
    first.run_cycle()  # Reconcile and persist the blocked conflict before the restart.
    pending_before = pending_path.read_bytes()
    host_free = True
    restarted = new_dispatcher()
    cycle = restarted.run_cycle()

    assert cycle["dispatched"] == [{"ticket": "USR-475-INDEPENDENT", "host": "grokbox"}]
    assert cycle["skipped"]["USR-475-CONFLICT"] == "pending_delivery:technical_conflict_exhausted"
    assert launched == ["USR-475-INDEPENDENT"]
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == candidate
    assert (repo / "docs" / "manual" / "motor.md").read_bytes() == conflicted_file
    assert pending_path.read_bytes() == pending_before

    log_path.write_text("[DELIVERY_MERGED]\n", encoding="utf-8")
    child.rc = 0
    demands[1]["status"] = "completed"
    completed = restarted.run_cycle()
    assert completed["deployed_this_cycle"] is True

    after_completion = pending_path.read_bytes()
    again = new_dispatcher()
    repeated = again.run_cycle()
    assert repeated["dispatched"] == []
    assert launched == ["USR-475-INDEPENDENT"]
    assert pending_path.read_bytes() == after_completion
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == candidate
    assert (repo / "docs" / "manual" / "motor.md").read_bytes() == conflicted_file
