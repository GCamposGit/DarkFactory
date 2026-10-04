"""An exhausted no-route wait must not strand a ticket's retry forever."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.line.owner_intake import _expired_no_route_wait, run_state, submit_ticket_to_line
from core.workflow.control_store import SQLiteControlStore


def _status(*, cause: str = "no_route_available", extra_open: bool = False) -> dict:
    jobs = [{"stage": "development", "status": "waiting_human", "cause_code": cause}]
    if extra_open:
        jobs.append({"stage": "planning", "status": "pending", "cause_code": None})
    return {"created_at": "2026-10-01T00:00:00+00:00", "jobs": jobs}


def test_only_expired_lone_no_route_wait_is_logically_terminal() -> None:
    now = datetime(2026, 10, 1, 7, tzinfo=UTC)
    assert _expired_no_route_wait(_status(), now=now, wall_clock_hours=6)
    assert not _expired_no_route_wait(_status(), now=datetime(2026, 10, 1, 5, tzinfo=UTC), wall_clock_hours=6)
    assert not _expired_no_route_wait(_status(cause="base_red_exhausted"), now=now, wall_clock_hours=6)
    assert not _expired_no_route_wait(_status(extra_open=True), now=now, wall_clock_hours=6)
    assert not _expired_no_route_wait({"jobs": _status()["jobs"]}, now=now, wall_clock_hours=6)


def test_expired_wait_allows_a_new_ticket_attempt(tmp_path: Path) -> None:
    demands = DemandsStore(tmp_path / "demands.json")
    demands.save_ticket(UserTicket(id="USR-62", title="Retry expired no-route ticket"))
    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    first = submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    assert first.ok and first.run_id

    conn = store._connect()
    try:
        conn.execute(
            "UPDATE runs SET created_at = ? WHERE run_id = ?",
            ("2026-10-01T00:00:00+00:00", first.run_id),
        )
        conn.execute(
            "UPDATE jobs SET status = 'waiting_human', cause_code = 'no_route_available' WHERE run_id = ?",
            (first.run_id,),
        )
        conn.commit()
    finally:
        conn.close()

    assert run_state(store, first.run_id, now=datetime(2026, 10, 1, 7, tzinfo=UTC), wall_clock_hours=6) == "failed"
    second = submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    assert second.ok and second.attempt == 2 and second.run_id != first.run_id
