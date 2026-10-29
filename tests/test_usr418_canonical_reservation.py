"""USR-418 concurrent channel ownership regression tests."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.demands import DemandsStore, UserTicket
from core.line import owner_intake
from core.workflow.control_contracts import TicketAlreadyActiveError
from core.workflow.control_store import SQLiteControlStore


def test_line_intake_observes_canonical_reservation_and_releases_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    demands = DemandsStore(tmp_path / "demands.json")
    ticket = UserTicket(
        id="USR-418", title="Canonical owner", problem_statement="Do not duplicate active execution.",
        non_goals=["Keep the existing execution intact"], acceptance_criteria=["Second channel is refused"],
    )
    distinct = UserTicket(
        id="USR-419", title="Distinct ticket", problem_statement="Independent work.",
        non_goals=["Do not block unrelated work"], acceptance_criteria=["May enter while USR-418 runs"],
    )
    demands.save_ticket(ticket)
    demands.save_ticket(distinct)
    monkeypatch.setattr(owner_intake, "find_active_pull_request", lambda *_a, **_k: None)
    reservation = store.reserve_cycle(
        project_id="darkfac", ticket_id=ticket.id, run_id="manual-418", host_id="notebook",
        now=now, lease_duration_sec=600,
    )

    blocked = owner_intake.submit_ticket_to_line(ticket.id, demands_store=demands, store=store, now=now)
    assert not blocked.ok
    assert blocked.state == "canonical_ownership_conflict"
    assert blocked.run_id == "manual-418"
    assert "notebook" in blocked.message

    independent = owner_intake.submit_ticket_to_line(distinct.id, demands_store=demands, store=store, now=now)
    assert independent.ok

    store.release_reservation(
        reservation_id=reservation.reservation_id, fencing_token=reservation.fencing_token,
        status="released", now=now,
    )
    resumed = owner_intake.submit_ticket_to_line(ticket.id, demands_store=demands, store=store, now=now)
    assert resumed.ok


def test_concurrent_channel_reservations_have_one_winner_and_release_reopens_ticket(tmp_path: Path) -> None:
    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    now = datetime.now(UTC)
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def reserve(host: str) -> None:
        barrier.wait()
        try:
            outcomes.append(store.reserve_cycle(
                project_id="darkfac", ticket_id="USR-418", run_id=f"{host}-run", host_id=host,
                now=now, lease_duration_sec=600,
            ))
        except TicketAlreadyActiveError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=reserve, args=(host,)) for host in ("notebook", "desktop")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    winners = [result for result in outcomes if not isinstance(result, TicketAlreadyActiveError)]
    losers = [result for result in outcomes if isinstance(result, TicketAlreadyActiveError)]
    assert len(winners) == len(losers) == 1

    winner = winners[0]
    store.release_reservation(
        reservation_id=winner.reservation_id, fencing_token=winner.fencing_token,
        status="released", now=now,
    )
    next_owner = store.reserve_cycle(
        project_id="darkfac", ticket_id="USR-418", run_id="retry-run", host_id="desktop",
        now=now, lease_duration_sec=600,
    )
    assert next_owner.run_id == "retry-run"


def test_postgres_canonical_ownership_lookup_is_scoped_and_returns_existing_run() -> None:
    from core.orchestrator.adapters.control_postgres import PostgresControlStore

    now = datetime.now(UTC)
    store = object.__new__(PostgresControlStore)
    store.mock_mode = False
    store.raw_url = "postgresql://example.invalid/control"
    store._psycopg = MagicMock()
    cursor = store._psycopg.connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (
        "reservation-1", "darkfac", "USR-418", "run-418", "notebook", "active", 1,
        now, now, now, {},
    )

    reservation = store.get_active_ticket_reservation("darkfac", "USR-418", now=now)

    assert reservation is not None
    assert reservation.run_id == "run-418"
    assert cursor.execute.call_args.args[1] == ("darkfac", "USR-418", now)
