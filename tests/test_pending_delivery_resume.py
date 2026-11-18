"""Pending child failures resume delivery from their durable continuation record (USR-475 T2)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.demands import queue_store
from core.git import ticket_workspace
from core.line import line_lock, pending_delivery, run_outcome, ticket_engine


def test_failed_delivery_child_restarts_from_persisted_delivery_checkpoint(
    tmp_path: Path, monkeypatch,
) -> None:
    ticket_id = "USR-475-RESUME"
    now = 100_000.0
    state_root = tmp_path / "state"
    worktree = tmp_path / "ticket-worktree"
    worktree.mkdir()
    pending_path = tmp_path / "pending_delivery" / f"{ticket_id}.json"
    row = pending_delivery.PendingDelivery(
        ticket_id=ticket_id,
        worktree=str(worktree),
        stage="development",
        next_check=0,
        pid=771,
        started_at=now - 5,
        status="running",
        run_id="build-and-delivery-child-1",
    )
    pending_delivery._save(pending_path, row)
    pending_path.with_suffix(".log").write_text("merge failed after BUILD completed\n", encoding="utf-8")

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(state_root))
    monkeypatch.setattr(pending_delivery, "_children", {})
    monkeypatch.setattr(pending_delivery, "RECHECK_SECONDS", 30)
    monkeypatch.setattr(pending_delivery, "_reconcile_terminal_wait", lambda *_a, **_k: None)
    outcome = SimpleNamespace(
        exit_code=1,
        cause="merge_failed",
        cause_class=SimpleNamespace(value="delivery"),
        stage="merge",
        detail="merge failed after BUILD completed",
    )
    monkeypatch.setattr(run_outcome, "read_outcome", lambda *_a, **_k: outcome)
    monkeypatch.setattr(run_outcome, "main_sha", lambda *_a, **_k: "a" * 40)
    monkeypatch.setattr(run_outcome, "error_detail", lambda text, fallback="unknown": text or fallback)

    class Admission:
        def record_attempt(self, *_args, **_kwargs) -> dict[str, float]:
            return {"next_attempt_at": now + 1}

        def ticket_hold(self, **_kwargs) -> bool:
            return False

        def budget(self, *_args, **_kwargs) -> dict[str, Any]:
            return {"next_attempt_at": 0, "main_sha": "a" * 40, "infra_streak": 0}

        def retry_reason(self, *_args, **_kwargs) -> None:
            return None

    admission = Admission()
    monkeypatch.setattr(pending_delivery, "_budget_store", lambda *_a, **_k: admission)
    finished = pending_delivery._child_finished(pending_path, row, 1, tmp_path, now)
    saved = pending_delivery.PendingDelivery.model_validate_json(pending_path.read_text(encoding="utf-8"))

    assert finished["status"] == "waiting"
    assert saved.stage == "review" and saved.resume_delivery is True
    assert len(saved.child_failures) == 1
    assert saved.child_failures[0].stage == "merge"
    assert saved.child_failures[0].cause == "merge_failed"

    class TicketStore:
        mock_mode = False

        def list_tickets(self) -> list[dict[str, str]]:
            return [{"id": ticket_id, "status": "implementing"}]

    monkeypatch.setattr(queue_store, "control_store", lambda: TicketStore())
    monkeypatch.setattr(ticket_workspace, "list_worktrees", lambda *_a, **_k: [
        {"worktree": str(worktree), "branch": f"refs/heads/ticket/{ticket_id.lower()}"},
    ])
    monkeypatch.setattr(ticket_engine, "artifacts_root", lambda: tmp_path / "artifacts")
    monkeypatch.setattr(line_lock, "is_locked", lambda: False)
    monkeypatch.setattr(pending_delivery, "_route", lambda _row: object())

    launched: list[list[str]] = []

    class Child:
        pid = 9922

    def fake_popen(command: list[str], **_kwargs) -> Child:
        launched.append(command)
        return Child()

    monkeypatch.setattr(pending_delivery.subprocess, "Popen", fake_popen)
    ticked = pending_delivery._tick_row(pending_path, tmp_path, now + 100)
    restarted = pending_delivery.PendingDelivery.model_validate_json(pending_path.read_text(encoding="utf-8"))

    assert ticked["status"] == "running"
    assert launched and launched[0][2:] == [
        "--resume-delivery", ticket_id, "--worktree", str(worktree.resolve()),
    ]
    assert restarted.resume_delivery is True and restarted.stage == "review"
    assert restarted.pid == Child.pid and len(restarted.child_failures) == 1
