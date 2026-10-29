"""USR-418 regression: recheck active PRs when a queued run is dispatched."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.demands import DemandsStore, UserTicket
from core.line import bindings, owner_intake
from core.line.active_pull_request import ActivePullRequest
from core.workflow.control_store import SQLiteControlStore


def test_development_rechecks_active_pr_after_admission_under_execution_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PR opened after admission must stop the queued run before its agent starts."""
    ticket = UserTicket(
        id="USR-418", title="Active PR", problem_statement="Do not duplicate active implementation.",
        core_journey=["Detect the open PR"], non_goals=["Do not change PR"],
        acceptance_criteria=["Refuse a second implementation after dispatch"],
    )
    demands = DemandsStore(tmp_path / "demands.json")
    demands.save_ticket(ticket)
    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    active_pr: ActivePullRequest | None = None
    reservation = None

    def lookup(_project_id: str, _ticket_id: str, **_kwargs: object) -> ActivePullRequest | None:
        assert reservation is not None
        stored_reservation = store.get_active_ticket_reservation(
            "darkfac", ticket.id, now=datetime.now(UTC),
        )
        assert stored_reservation is not None
        assert stored_reservation.run_id == reservation.run_id
        return active_pr

    monkeypatch.setattr(owner_intake, "find_active_pull_request", lambda *_a, **_k: active_pr)
    admitted = owner_intake.submit_ticket_to_line(
        ticket.id, demands_store=demands, store=store, now=datetime.now(UTC),
    )
    assert admitted.ok

    # Another channel completes the ticket and opens a PR before the queued run is dispatched.
    reservation = store.reserve_cycle(
        project_id="darkfac", ticket_id=ticket.id, run_id=admitted.run_id, host_id="notebook",
        now=datetime.now(UTC), lease_duration_sec=600,
    )
    active_pr = ActivePullRequest(779, "https://github.com/org/repo/pull/779", "USR-418", "ticket/usr-418")
    called: list[str] = []

    class DevelopmentStage:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(self, *_args: object) -> object:
            called.append("implementation")
            pytest.fail("an active PR must prevent the second implementation")

    monkeypatch.setattr(bindings, "find_active_pull_request", lookup)
    monkeypatch.setattr(bindings.stage_build, "DevelopmentStage", DevelopmentStage)
    handler = bindings.DevelopmentStageHandler(
        project_resolver=lambda _project_id: SimpleNamespace(id="darkfac"), store=store,
    )
    context = SimpleNamespace(
        project_id="darkfac",
        claim=SimpleNamespace(job_key=SimpleNamespace(ticket_id=ticket.id, run_id=admitted.run_id)),
    )

    result = handler.handle(context)  # type: ignore[arg-type]

    assert result.outcome == "failed"
    assert result.cause_code == "active_pull_request"
    assert result.evidence_refs == [f"PR #779: {active_pr.url}"]
    assert called == []


def test_development_refuses_another_run_canonical_reservation_without_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket_id = "USR-418"
    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    now = datetime.now(UTC)
    reservation = store.reserve_cycle(
        project_id="darkfac", ticket_id=ticket_id, run_id="existing-run", host_id="desktop",
        now=now, lease_duration_sec=600,
    )
    looked_up: list[str] = []
    monkeypatch.setattr(
        bindings, "find_active_pull_request", lambda *_args, **_kwargs: looked_up.append("pr")
    )

    class DevelopmentStage:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(self, *_args: object) -> object:
            pytest.fail("canonical ownership must prevent development from running")

    monkeypatch.setattr(bindings.stage_build, "DevelopmentStage", DevelopmentStage)
    handler = bindings.DevelopmentStageHandler(
        project_resolver=lambda _project_id: SimpleNamespace(id="darkfac"), store=store,
    )
    context = SimpleNamespace(
        project_id="darkfac",
        claim=SimpleNamespace(job_key=SimpleNamespace(ticket_id=ticket_id, run_id="queued-run")),
    )

    result = handler.handle(context)  # type: ignore[arg-type]

    assert result.outcome == "failed"
    assert result.cause_code == "canonical_ownership_conflict"
    assert result.evidence_refs == [f"Run {reservation.run_id} on host {reservation.host_id}"]
    assert looked_up == []
