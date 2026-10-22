"""USR-337: local admission respects measured capacity; explicit reopen of completed tickets."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.orchestrator.cloud_coordinator import CloudCoordinator
from core.roadmap.models import DeliveryStatus


class _FakeStore:
    def __init__(self, jobs=None, active=None, conflicts=None) -> None:
        self.reserved: list[str] = []
        self.active = list(active or [])
        self.jobs = jobs or [{"project_id": "darkfac", "ticket_id": "USR-1", "run_id": "r1"}]
        self.conflicts = set(conflicts or [])
        self.max_fleet_slots: list[int] = []

    def list_active_reservations(self):
        return list(self.active)

    def list_eligible_admission_jobs(self, limit=50):
        return list(self.jobs)

    def reserve_cycle(self, *, project_id, ticket_id, run_id, host_id, now, max_fleet_slots=3):
        self.max_fleet_slots.append(max_fleet_slots)
        if (ticket_id, host_id) in self.conflicts:
            self.conflicts.remove((ticket_id, host_id))
            raise RuntimeError("reservation conflict")
        self.reserved.append(host_id)
        reservation = SimpleNamespace(
            reservation_id=f"{ticket_id}-{host_id}", host_id=host_id,
            project_id=project_id, ticket_id=ticket_id, run_id=run_id,
        )
        self.active.append(reservation)
        return reservation


def _coordinator(monkeypatch, capacity: int, hosts=("notebook", "desktop"), **store_kwargs):
    monkeypatch.setattr("core.line.line_lock.resolve_host_id", lambda host_id=None: "notebook")
    store = _FakeStore(**store_kwargs)
    coord = CloudCoordinator(
        store=store,
        fleet_hosts=list(hosts),
        host_health_prober=lambda h: True,
        capacity_probe=lambda: capacity,
    )
    return coord, store


def test_default_probe_uses_fleet_policy_with_only_local_verified_capacity(monkeypatch):
    from core.line import fleet_policy

    monkeypatch.setattr("core.line.line_lock.resolve_host_id", lambda host_id=None: "notebook")
    monkeypatch.setattr("core.line.gate_ram.measure_workers", lambda: SimpleNamespace(workers=2))
    calls = []
    original = fleet_policy.select_host_for_ticket

    def observe(**kwargs):
        calls.append(kwargs["verified_capacity"])
        return original(**kwargs)

    monkeypatch.setattr(fleet_policy, "select_host_for_ticket", observe)
    store = _FakeStore()
    coord = CloudCoordinator(store=store, fleet_hosts=["notebook", "desktop"], host_health_prober=lambda h: True)

    coord.admit_eligible_cycles()

    assert calls and calls[0] == {"notebook": 2}
    assert store.reserved == ["notebook"]


@pytest.mark.parametrize("capacity", [0, 2])
def test_local_capacity_is_measured_in_unmanaged_mode(monkeypatch, capacity):
    monkeypatch.setattr("core.orchestrator.cloud_coordinator.fleet_managed_enabled", lambda: False)
    monkeypatch.setattr("core.line.gate_ram.measure_workers", lambda: SimpleNamespace(workers=capacity))
    monkeypatch.setattr("core.line.line_lock.resolve_host_id", lambda host_id=None: "notebook")
    store = _FakeStore()
    coord = CloudCoordinator(store=store, fleet_hosts=["notebook", "desktop"], host_health_prober=lambda h: True)

    coord.admit_eligible_cycles()

    assert ("notebook" in store.reserved) is (capacity > 0)


def test_measurement_exception_blocks_local_but_keeps_remote_admission(monkeypatch):
    monkeypatch.setattr("core.orchestrator.cloud_coordinator.fleet_managed_enabled", lambda: False)

    def boom():
        raise RuntimeError("measurement unavailable")

    monkeypatch.setattr("core.line.gate_ram.measure_workers", boom)
    monkeypatch.setattr("core.line.line_lock.resolve_host_id", lambda host_id=None: "notebook")
    store = _FakeStore()
    coord = CloudCoordinator(store=store, fleet_hosts=["notebook", "desktop"], host_health_prober=lambda h: True)

    coord.admit_eligible_cycles()

    assert store.reserved == ["desktop"]


def test_admission_respects_busy_hosts_existing_slots_and_global_ceiling(monkeypatch):
    monkeypatch.setattr("core.line.line_lock.resolve_host_id", lambda host_id=None: "notebook")
    monkeypatch.setattr("core.line.gate_ram.measure_workers", lambda: SimpleNamespace(workers=4))
    active = [SimpleNamespace(project_id="darkfac", ticket_id="old", run_id="old-run", host_id="grokbox")]
    jobs = [{"project_id": "darkfac", "ticket_id": f"USR-{i}", "run_id": f"r{i}"} for i in range(5)]
    store = _FakeStore(jobs=jobs, active=active)
    coord = CloudCoordinator(store=store, max_concurrent_slots=2, fleet_hosts=["grokbox", "notebook", "desktop"], host_health_prober=lambda h: True)

    admitted = coord.admit_eligible_cycles()

    assert len(admitted) == 1
    assert store.reserved == ["notebook"]
    assert store.max_fleet_slots == [2]
    assert active[0].host_id == "grokbox"


def test_occupied_local_host_keeps_reservation_and_dispatches_remotely(monkeypatch):
    monkeypatch.setattr("core.line.line_lock.resolve_host_id", lambda host_id=None: "notebook")
    monkeypatch.setattr("core.line.gate_ram.measure_workers", lambda: SimpleNamespace(workers=3))
    existing = SimpleNamespace(
        project_id="darkfac", ticket_id="existing", run_id="existing-run", host_id="notebook",
    )
    store = _FakeStore(active=[existing])
    coord = CloudCoordinator(
        store=store,
        fleet_hosts=["notebook", "desktop"],
        host_health_prober=lambda h: True,
    )

    admitted = coord.admit_eligible_cycles()

    assert [reservation.host_id for reservation in admitted] == ["desktop"]
    assert store.reserved == ["desktop"]
    assert existing.host_id == "notebook" and existing.run_id == "existing-run"


def test_conflict_reselects_next_host_and_repeated_cycle_has_no_duplicates(monkeypatch):
    coord, store = _coordinator(
        monkeypatch, capacity=2, hosts=("notebook", "desktop"),
        jobs=[{"project_id": "darkfac", "ticket_id": "USR-1", "run_id": "r1"}],
        conflicts={("USR-1", "notebook")},
    )

    first = coord.admit_eligible_cycles()
    second = coord.admit_eligible_cycles()

    assert [r.host_id for r in first] == ["desktop"]
    assert second == []
    assert store.reserved == ["desktop"]


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


def test_reopen_completed_is_idempotent_and_invalidates_delivery_evidence(tmp_path):
    store = DemandsStore(tmp_path / "d.json")
    _ticket(store, "USR-903", DeliveryStatus.COMPLETED)
    first, changed_first = store.requeue_ticket("USR-903", reason="[LIVE_CONVERGENCE_FAILURE] retry", reopen=True)
    second, changed_second = store.requeue_ticket("USR-903", reason="[LIVE_CONVERGENCE_FAILURE] retry", reopen=True)
    assert changed_first is True
    assert changed_second is False
    assert first.delivery_evidence is None and second.delivery_evidence is None
    assert second.problem_statement.count("LIVE_CONVERGENCE_FAILURE") == 1


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
