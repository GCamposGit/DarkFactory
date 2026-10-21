"""USR-337: local admission respects measured capacity; explicit reopen of completed tickets."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.orchestrator.cloud_coordinator import CloudCoordinator
from core.roadmap.models import DeliveryStatus


class _FakeStore:
    def __init__(self) -> None:
        self.reserved: list[str] = []

    def list_active_reservations(self):
        return []

    def list_eligible_admission_jobs(self, limit=50):
        return [{"project_id": "darkfac", "ticket_id": "USR-1", "run_id": "r1"}]

    def reserve_cycle(self, *, project_id, ticket_id, run_id, host_id, now):
        self.reserved.append(host_id)
        return SimpleNamespace(reservation_id="x", host_id=host_id)


def _coordinator(monkeypatch, capacity: int, hosts=("notebook", "desktop")):
    monkeypatch.setattr("core.line.line_lock.resolve_host_id", lambda host_id=None: "notebook")
    store = _FakeStore()
    coord = CloudCoordinator(
        store=store,
        fleet_hosts=list(hosts),
        host_health_prober=lambda h: True,
        capacity_probe=lambda: capacity,
    )
    return coord, store


def test_local_host_skipped_when_no_capacity(monkeypatch):
    coord, store = _coordinator(monkeypatch, capacity=0)
    coord.admit_eligible_cycles()
    assert store.reserved == ["desktop"]  # never the starved local host


def test_local_host_admitted_with_capacity(monkeypatch):
    coord, store = _coordinator(monkeypatch, capacity=2)
    coord.admit_eligible_cycles()
    assert store.reserved == ["notebook"]


def test_probe_failure_fail_closed_in_managed_mode(monkeypatch):
    monkeypatch.setattr("core.orchestrator.cloud_coordinator.fleet_managed_enabled", lambda: True)
    coord, store = _coordinator(monkeypatch, capacity=1)

    def boom():
        raise RuntimeError("no ram probe")

    coord.capacity_probe = boom
    coord.admit_eligible_cycles()
    assert "notebook" not in store.reserved


def _ticket(store: DemandsStore, tid: str, status: DeliveryStatus) -> None:
    ev = "sha" if status == DeliveryStatus.COMPLETED else None
    store.save_ticket(UserTicket(id=tid, title=tid, status=status, delivery_evidence=ev, problem_statement="orig"))


def test_reopen_completed_preserves_history(tmp_path):
    store = DemandsStore(tmp_path / "d.json")
    _ticket(store, "USR-900", DeliveryStatus.COMPLETED)
    t, changed = store.requeue_ticket("USR-900", reason="[LIVE_CONVERGENCE_FAILURE] x", reopen=True)
    assert changed and t.status == DeliveryStatus.PLANNED and t.delivery_evidence is None
    assert t.problem_statement.startswith("orig") and "LIVE_CONVERGENCE_FAILURE" in t.problem_statement


def test_reopen_requires_reason_and_default_still_refuses(tmp_path):
    store = DemandsStore(tmp_path / "d.json")
    _ticket(store, "USR-901", DeliveryStatus.COMPLETED)
    with pytest.raises(ValueError):
        store.requeue_ticket("USR-901", reason="  ", reopen=True)
    with pytest.raises(ValueError):
        store.requeue_ticket("USR-901", reason="x")


def test_reopen_never_reopens_cancelled(tmp_path):
    store = DemandsStore(tmp_path / "d.json")
    _ticket(store, "USR-902", DeliveryStatus.CANCELLED)
    with pytest.raises(ValueError):
        store.requeue_ticket("USR-902", reason="x", reopen=True)
