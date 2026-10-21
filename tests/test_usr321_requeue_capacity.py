"""USR-321 slice 2: guarded requeue, ledger-only classification, verified dispatch capacity."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.git.ci_checks import classify_delivery
from core.line.fleet_policy import (
    FleetConfig, HostConfig, HostState, TicketCandidate, evaluate_admission,
)
from core.roadmap.models import DeliveryStatus


def _ticket(store: DemandsStore, tid: str, status: DeliveryStatus) -> None:
    evidence = "e" if status == DeliveryStatus.COMPLETED else None
    store.save_ticket(UserTicket(id=tid, title=tid, status=status, delivery_evidence=evidence))


def test_requeue_concurrent_stores_no_lost_update(tmp_path):
    path = tmp_path / "demands.json"
    seed = DemandsStore(path)
    ids = [f"USR-{900 + i}" for i in range(12)]
    for tid in ids:
        _ticket(seed, tid, DeliveryStatus.IMPLEMENTING)
    results: list[bool] = []
    barrier = threading.Barrier(len(ids))

    def work(tid: str) -> None:
        store = DemandsStore(path)  # distinct instance, same file
        barrier.wait()
        results.append(store.requeue_ticket(tid, "test")[1])

    threads = [threading.Thread(target=work, args=(t,)) for t in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == len(ids) and all(results)
    assert {t.status for t in DemandsStore(path).list_tickets()} == {DeliveryStatus.PLANNED}


def test_requeue_same_ticket_idempotent_and_terminal_refused(tmp_path):
    path = tmp_path / "demands.json"
    store = DemandsStore(path)
    _ticket(store, "USR-901", DeliveryStatus.IMPLEMENTING)
    _ticket(store, "USR-902", DeliveryStatus.COMPLETED)
    out: list[bool] = []
    barrier = threading.Barrier(8)

    def work() -> None:
        barrier.wait()
        out.append(DemandsStore(path).requeue_ticket("USR-901")[1])

    ts = [threading.Thread(target=work) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert out.count(True) == 1
    with pytest.raises(ValueError):
        store.requeue_ticket("USR-902")


def test_classify_delivery():
    assert classify_delivery([]) == "empty"
    assert classify_delivery([".factory/demands/demands.json"]) == "ledger_only"
    assert classify_delivery([".factory/owner_actions/owner_actions.json"]) == "ledger_only"
    assert classify_delivery([".factory/demands/demands.json", "core/x.py"]) == "implementation"


def _fleet() -> FleetConfig:
    return FleetConfig(max_fleet_slots=5, hosts=[
        HostConfig(id="notebook", name="N", priority=1, max_slots=3, tags=["local"]),
    ])


def _cands(n: int) -> list[TicketCandidate]:
    base = datetime.now(timezone.utc) - timedelta(days=1)
    return [TicketCandidate(ticket_id=f"USR-{i}", created_at=base + timedelta(minutes=i)) for i in range(n)]


def test_dispatch_capped_by_verified_capacity():
    res = evaluate_admission(_cands(5), _fleet(), {}, verified_capacity={"notebook": 2})
    assert len(res.admissions) == 2


def test_busy_worker_not_reused_when_capacity_shrinks():
    states = {"notebook": HostState(host_id="notebook", active_slots=2, active_cycle_runs=["a", "b"])}
    res = evaluate_admission(_cands(3), _fleet(), states, verified_capacity={"notebook": 1})
    assert res.admissions == []
    assert states["notebook"].active_slots == 2


def test_no_capacity_info_keeps_static_limit():
    res = evaluate_admission(_cands(5), _fleet(), {})
    assert len(res.admissions) == 3
