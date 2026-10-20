"""USR-306: fleet availability, reservation renewal and managed-mode claim/admission (no network, no Postgres)."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.orchestrator.cloud_coordinator import CloudCoordinator
from core.orchestrator.cloud_worker import CloudWorker
from core.workflow.control_contracts import (
    FLEET_MANAGED_ENV,
    IntakeCommand,
    JobKey,
    RuntimeOwner,
    StageResult,
)
from core.workflow.control_store import SQLiteControlStore
from core.workflow.line_live import FLEET_HEALTH_FRESH_SEC, _build_fleet

CAPS = ["harness:codex", "grill_engine"]


def _stores():
    return [
        SQLiteControlStore(db_path=":memory:", runtime_owner=RuntimeOwner.HF05_SQLITE.value, lease_duration_sec=30),
        PostgresControlStore(
            mock_mode=True, runtime_owner=RuntimeOwner.CLOUD_DBOS_POSTGRES.value, lease_duration_sec=30
        ),
    ]


@pytest.fixture(params=[0, 1], ids=["sqlite", "postgres_mock"])
def store(request):
    return _stores()[request.param]


def _accept(store, now: datetime, name: str = "darkfac"):
    cmd = IntakeCommand(
        project_id=name,
        channel="cli",
        external_id=f"ext-{uuid4().hex[:8]}",
        payload={
            "title": "USR-306",
            "problem": "fleet availability",
            "journey": ["step"],
            "non_goals": ["none"],
            "criteria": ["c"],
        },
        mode="autonomous",
        policy_ref="default",
    )
    return store.accept(cmd, now=now)


def _reserve(store, receipt, now: datetime, host: str = "desktop"):
    return store.reserve_cycle(
        project_id="darkfac", ticket_id="darkfac", run_id=receipt.run_id, host_id=host, now=now
    )


# --------------------------------------------------------------------------- _build_fleet


def test_build_fleet_without_health_source_is_unknown_not_online():
    fleet = _build_fleet(None, datetime.now(UTC))
    assert {h.host_id for h in fleet.hosts} == {"grokbox", "notebook", "desktop"}
    assert all(h.status == "unknown" for h in fleet.hosts)
    assert all(h.status != "online" for h in fleet.hosts)


def test_build_fleet_online_only_with_fresh_reservation_heartbeat():
    now = datetime.now(UTC)
    fresh = (now - timedelta(seconds=5)).isoformat()
    stale = (now - timedelta(seconds=FLEET_HEALTH_FRESH_SEC + 60)).isoformat()
    expires = (now + timedelta(seconds=30)).isoformat()
    raw = SimpleNamespace(
        reservations=[
            {"host_id": "grokbox", "status": "active", "run_id": "r1", "ticket_id": "T1",
             "heartbeat_at": fresh, "expires_at": expires},
            {"host_id": "notebook", "status": "active", "run_id": "r2", "ticket_id": "T2",
             "heartbeat_at": stale, "expires_at": expires},
        ]
    )
    by_id = {h.host_id: h for h in _build_fleet(raw, now).hosts}  # type: ignore[arg-type]
    assert by_id["grokbox"].status == "online"
    assert by_id["notebook"].status == "unknown"
    assert by_id["desktop"].status == "unknown"


# --------------------------------------------------------------------------- admission


def test_managed_mode_requires_verified_health(store, monkeypatch):
    monkeypatch.setenv(FLEET_MANAGED_ENV, "1")
    now = datetime.now(UTC)
    _accept(store, now)

    assert CloudCoordinator(store=store).admit_eligible_cycles(now) == []  # no prober => unknown
    assert CloudCoordinator(store=store, host_health_prober=lambda h: None).admit_eligible_cycles(now) == []
    assert CloudCoordinator(store=store, host_health_prober=lambda h: False).admit_eligible_cycles(now) == []

    def boom(host):
        raise RuntimeError("probe down")

    assert CloudCoordinator(store=store, host_health_prober=boom).admit_eligible_cycles(now) == []

    admitted = CloudCoordinator(
        store=store, host_health_prober=lambda h: h == "notebook"
    ).admit_eligible_cycles(now)
    assert [r.host_id for r in admitted] == ["notebook"]


def test_unmanaged_legacy_mode_keeps_presuming_hosts_available(store, monkeypatch):
    monkeypatch.delenv(FLEET_MANAGED_ENV, raising=False)
    now = datetime.now(UTC)
    _accept(store, now)
    admitted = CloudCoordinator(store=store).admit_eligible_cycles(now)
    assert [r.host_id for r in admitted] == ["grokbox"]


# --------------------------------------------------------------------------- claim path


def test_managed_mode_blocks_legacy_claim_without_reservation(store, monkeypatch):
    now = datetime.now(UTC)
    receipt = _accept(store, now)

    monkeypatch.setenv(FLEET_MANAGED_ENV, "1")
    assert store.claim(worker="desktop-worker", capabilities=CAPS, now=now) is None

    _reserve(store, receipt, now)
    claim = store.claim(worker="desktop-worker", capabilities=CAPS, now=now)
    assert claim is not None and claim.reservation_id.startswith("fres-")


def test_unmanaged_legacy_claim_without_reservation_still_works(store, monkeypatch):
    monkeypatch.delenv(FLEET_MANAGED_ENV, raising=False)
    now = datetime.now(UTC)
    _accept(store, now)
    claim = store.claim(worker="desktop-worker", capabilities=CAPS, now=now)
    assert claim is not None and claim.reservation_id.startswith("res-")


# --------------------------------------------------------------------------- renewal


def test_claim_renews_reservation_heartbeat(store, monkeypatch):
    monkeypatch.delenv(FLEET_MANAGED_ENV, raising=False)
    now = datetime.now(UTC)
    receipt = _accept(store, now)
    res = _reserve(store, receipt, now)
    later = now + timedelta(seconds=20)
    claim = store.claim(worker="desktop-worker", capabilities=CAPS, now=later)
    assert claim is not None and claim.reservation_id == res.reservation_id

    renewed = store.get_reservation(res.reservation_id)
    assert datetime.fromisoformat(renewed.heartbeat_at) > datetime.fromisoformat(renewed.acquired_at)
    assert datetime.fromisoformat(renewed.expires_at) > datetime.fromisoformat(res.expires_at)


def test_coordinator_renews_reservation_while_run_waits_between_stages(store, monkeypatch):
    monkeypatch.delenv(FLEET_MANAGED_ENV, raising=False)
    now = datetime.now(UTC)
    receipt = _accept(store, now)
    res = _reserve(store, receipt, now)

    # Job still pending (next stage / CI wait) 20 s later: well past the first renewal, before expiry.
    t1 = now + timedelta(seconds=20)
    assert store.renew_open_run_reservations(t1) == 1
    r1 = store.get_reservation(res.reservation_id)
    assert datetime.fromisoformat(r1.heartbeat_at) > datetime.fromisoformat(r1.acquired_at)
    assert datetime.fromisoformat(r1.expires_at) > datetime.fromisoformat(res.expires_at)

    # Repeated coordinator ticks keep it alive well beyond the original 30 s TTL.
    for step in range(2, 6):
        assert store.renew_open_run_reservations(now + timedelta(seconds=20 * step)) == 1
    assert store.get_reservation(res.reservation_id).is_active

    # A dead host never claims: past the pending grace window the reservation is no longer renewed.
    far = now + timedelta(seconds=20 * 5 + 1000)
    assert store.renew_open_run_reservations(far) == 0


def test_expired_reservation_is_not_resurrected_by_renewal(store, monkeypatch):
    monkeypatch.delenv(FLEET_MANAGED_ENV, raising=False)
    now = datetime.now(UTC)
    receipt = _accept(store, now)
    _reserve(store, receipt, now)
    assert store.renew_open_run_reservations(now + timedelta(seconds=300)) == 0


# --------------------------------------------------------------------------- worker heartbeat


class _HeartbeatStore:
    """Minimal store recording lease and reservation beats."""

    lease_duration_sec = 0.3  # => heartbeat interval 0.1 s with HEARTBEAT_MIN_INTERVAL_S lowered

    def __init__(self, bound: bool = True) -> None:
        self.lease_beats: list[datetime] = []
        self.res_beats: list[datetime] = []
        self._res = SimpleNamespace(reservation_id="fres-1", fencing_token=1, is_active=True)
        self._bound = bound

    def heartbeat(self, claim, now):
        self.lease_beats.append(now)
        return claim

    def get_reservation(self, reservation_id):
        return self._res if (self._bound and reservation_id == "fres-1") else None

    def get_active_reservation_for_run(self, run_id):
        return self._res

    def heartbeat_reservation(self, reservation_id, fencing_token, now, lease_duration_sec=None):
        self.res_beats.append(now)
        return self._res


def _claim(reservation_id: str):
    return SimpleNamespace(
        lease_id="lease-1",
        acquired_at=datetime.now(UTC).isoformat(),
        reservation_id=reservation_id,
        job_key=JobKey(run_id="run-1", ticket_id="T", plan_version="v1", stage="review", iteration=0),
    )


@pytest.mark.parametrize("claim_res_id", ["fres-1", "res-legacy01"])
def test_long_handler_keeps_renewing_reservation(monkeypatch, claim_res_id):
    monkeypatch.setattr(CloudWorker, "HEARTBEAT_MIN_INTERVAL_S", 0.05)
    st = _HeartbeatStore(bound=claim_res_id == "fres-1")
    worker = CloudWorker(worker_id="w-test", max_slots=1, store=st)  # type: ignore[arg-type]
    stop, thread = worker._start_lease_heartbeat(_claim(claim_res_id))  # type: ignore[arg-type]
    try:
        time.sleep(0.6)  # long handler / CI wait inside the handler
    finally:
        stop.set()
        thread.join(timeout=2.0)

    assert len(st.res_beats) >= 3, st.res_beats
    assert len(st.lease_beats) >= 2
    # Beats are spread over the whole wait, not only the first one (the first is immediate).
    assert st.res_beats[-1] > st.res_beats[0]


def test_short_handler_still_gets_one_reservation_renewal(monkeypatch):
    monkeypatch.setattr(CloudWorker, "HEARTBEAT_MIN_INTERVAL_S", 5.0)
    st = _HeartbeatStore()
    st.lease_duration_sec = 45
    worker = CloudWorker(worker_id="w-test", max_slots=1, store=st)  # type: ignore[arg-type]
    done = threading.Event()
    stop, thread = worker._start_lease_heartbeat(_claim("fres-1"))  # type: ignore[arg-type]
    time.sleep(0.2)  # handler far shorter than the 15 s interval
    stop.set()
    thread.join(timeout=2.0)
    done.set()
    assert len(st.res_beats) == 1
