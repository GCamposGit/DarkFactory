"""USR-330: a manual run_ticket start reserves ticket/project canonically, competing with the line."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.line import line_lock
from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.workflow.control_contracts import (
    ReservationConflictError,
    RuntimeOwner,
    TicketAlreadyActiveError,
)
from core.workflow.control_store import SQLiteControlStore

NOW = datetime.now(UTC)


def _manual(db: Path, root: Path, host: str, errors: list[str], wins: list[str], gate: threading.Barrier,
            hold: threading.Event) -> None:
    store = SQLiteControlStore(db_path=db)
    lock = line_lock.LineLock(
        "USR-330", root=root, store=store, host_id=host, project_id="darkfac", require_reservation=True,
    )
    gate.wait()
    try:
        lock.acquire()
    except ReservationConflictError as exc:
        errors.append(f"manual:{type(exc).__name__}")
        return
    wins.append("manual")
    hold.wait(10)
    lock.release()


def _coordinator(db: Path, host: str, errors: list[str], wins: list[str], gate: threading.Barrier) -> None:
    store = SQLiteControlStore(db_path=db)
    gate.wait()
    try:
        store.reserve_cycle("darkfac", "USR-330", f"run-{host}", host, datetime.now(UTC), lease_duration_sec=300)
        wins.append(host)
    except ReservationConflictError as exc:
        errors.append(f"{host}:{type(exc).__name__}")


def test_two_coordinators_and_manual_start_yield_single_owner(tmp_path: Path) -> None:
    db = tmp_path / "control.db"
    SQLiteControlStore(db_path=db)  # create schema once
    wins: list[str] = []
    errors: list[str] = []
    gate = threading.Barrier(3)
    hold = threading.Event()
    threads = [
        threading.Thread(target=_manual, args=(db, tmp_path / "lock", "notebook", errors, wins, gate, hold)),
        threading.Thread(target=_coordinator, args=(db, "grokbox", errors, wins, gate)),
        threading.Thread(target=_coordinator, args=(db, "desktop", errors, wins, gate)),
    ]
    for t in threads:
        t.start()
    for t in threads[1:]:
        t.join(15)
    hold.set()
    threads[0].join(15)
    assert len(wins) == 1, (wins, errors)
    assert len(errors) == 2
    assert all("TicketAlreadyActiveError" in e or "HostBusy" in e or "Conflict" in e for e in errors)
    active = SQLiteControlStore(db_path=db).list_active_reservations()
    assert len(active) <= 1


def test_manual_holds_then_line_refused_then_release_frees(tmp_path: Path) -> None:
    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    lock = line_lock.LineLock("USR-330", root=tmp_path / "l", store=store, host_id="notebook",
                              require_reservation=True)
    lock.acquire()
    assert lock.reservation is not None and lock.reservation.metadata == {"origin": "manual"}
    with pytest.raises(TicketAlreadyActiveError):
        store.reserve_cycle("darkfac", "USR-330", "line-run", "grokbox", datetime.now(UTC))
    lock.release()  # terminal exit
    store.reserve_cycle("darkfac", "USR-330", "line-run", "grokbox", datetime.now(UTC))


def test_crash_is_bounded_by_lease_expiry_and_fencing(tmp_path: Path) -> None:
    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    first = store.reserve_cycle("darkfac", "USR-330", "manual", "notebook", NOW, lease_duration_sec=45,
                                metadata={"origin": "manual"})
    with pytest.raises(TicketAlreadyActiveError):
        store.reserve_cycle("darkfac", "USR-330", "line", "grokbox", NOW + timedelta(seconds=10))
    # Crashed holder never heartbeats: the canonical lease expires and the line may admit.
    second = store.reserve_cycle("darkfac", "USR-330", "line", "grokbox", NOW + timedelta(seconds=46))
    assert second.reservation_id != first.reservation_id


def test_store_failure_fails_closed_without_local_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "lock"

    def boom() -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr("core.line.store_selection.default_control_store", boom)
    with pytest.raises(ReservationConflictError, match="falhou fechada"):
        line_lock.LineLock("USR-330", root=root, require_reservation=True).acquire()
    assert not line_lock.is_locked(root)

    class Broken:
        def reserve_cycle(self, **_: object) -> None:
            raise OSError("connection lost")

    with pytest.raises(ReservationConflictError, match="falhou fechada"):
        line_lock.LineLock("USR-330", root=root, store=Broken(), require_reservation=True).acquire()
    assert not line_lock.is_locked(root)

    mock_pg = PostgresControlStore(mock_mode=True, runtime_owner=RuntimeOwner.CLOUD_DBOS_POSTGRES.value)
    with pytest.raises(ReservationConflictError, match="sem fallback"):
        line_lock.LineLock("USR-330", root=root, store=mock_pg, require_reservation=True).acquire()


def test_postgres_adapter_has_same_ticket_uniqueness_contract() -> None:
    pg = PostgresControlStore(mock_mode=True, runtime_owner=RuntimeOwner.CLOUD_DBOS_POSTGRES.value)
    pg.reserve_cycle("darkfac", "USR-330", "manual", "notebook", NOW, metadata={"origin": "manual"})
    with pytest.raises(TicketAlreadyActiveError):
        pg.reserve_cycle("darkfac", "USR-330", "line", "grokbox", NOW)
