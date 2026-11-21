"""Session-loss continuity for an admitted run and its next regular admission."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from core.line import fleet_dispatcher as fd
from core.line.admission_store import AdmissionStore


class _Handle:
    def poll(self) -> int | None:
        return None


def test_restart_finishes_persisted_delivery_before_admitting_next_ticket(
    tmp_path: Path, monkeypatch,
) -> None:
    now = datetime(2026, 11, 21, tzinfo=timezone.utc).timestamp()
    state_path = tmp_path / "fleet.json"
    config = fd.DispatcherConfig(
        hosts=[fd.HostSpec("notebook", 1)], state_path=state_path,
        deploy_window_sec=30,
    )
    store = AdmissionStore(
        tmp_path / "admission.sqlite3",
        clock=lambda: datetime.fromtimestamp(now, timezone.utc),
    )
    first_session = fd.FleetDispatcher(config, admission_store=store, clock=lambda: now)
    first_session._record_pending_delivery(fd.InFlight(
        "USR-539-A", "notebook", _Handle(), project_id="product-x",
        candidate_digest="candidate-sha256", run_id="run-admitted-before-session-close",
        log_path=tmp_path / "delivery.log",
    ))
    resumed_at = now + 31

    launches: list[tuple[str, str]] = []
    deploys: list[str] = []

    def launch(ticket_id: str, host: fd.HostSpec, **kwargs) -> fd.InFlight:
        launches.append((ticket_id, kwargs["run_id"]))
        return fd.InFlight(ticket_id, host.id, _Handle(), run_id=kwargs["run_id"])

    second_session = fd.FleetDispatcher(
        config,
        demands_loader=lambda: [{"id": "USR-539-B", "status": "planned", "project_id": "product-x",
                                 "candidate_digest": "next-candidate", "priority": 20}],
        probe=lambda _host: fd.HostProbe(True),
        launch=launch,
        deploy=lambda: deploys.append("canonical-deploy") or 0,
        clock=lambda: resumed_at,
        admission_store=store,
    )
    second_session._resumptions = lambda: []  # session A vanished after the child completion record
    second_session._reconcile_stale_active = lambda: []
    second_session._read_canonical_reservations = lambda _at: {}
    second_session._publish_cycle_policy = lambda _occupied: ([{"host_id": "notebook", "eligible": True}], {})
    monkeypatch.setattr(fd, "PROJECT_ROOT", tmp_path)
    cycle = second_session.run_cycle()

    assert deploys == ["canonical-deploy"]
    assert launches and launches[0][0] == "USR-539-B"
    saved = __import__("json").loads((tmp_path / "pending_deliveries.json").read_text(encoding="utf-8"))
    assert saved[0]["run_id"] == "run-admitted-before-session-close"
    assert saved[0]["candidate_digest"] == "candidate-sha256"
    assert saved[0]["status"] == "deployed"
    assert saved[0]["stage"] == "deploy" and saved[0]["evidence_refs"] == [str(tmp_path / "delivery.log")]
    assert cycle["dispatched"][0]["ticket"] == "USR-539-B"

    third_session = fd.FleetDispatcher(config, admission_store=store, clock=lambda: resumed_at)
    assert third_session.pending_deploy is False
    assert third_session.delivery_journal[0]["status"] == "deployed"


def test_resume_only_closes_a_durable_delivery_without_admitting_builds(tmp_path: Path) -> None:
    now = datetime(2026, 11, 21, tzinfo=timezone.utc).timestamp()
    config = fd.DispatcherConfig(
        resume_only=True, hosts=[fd.HostSpec("notebook", 1)],
        state_path=tmp_path / "fleet.json", deploy_window_sec=0,
    )
    store = AdmissionStore(tmp_path / "admission.sqlite3", clock=lambda: datetime.fromtimestamp(now, timezone.utc))
    first = fd.FleetDispatcher(config, admission_store=store, clock=lambda: now)
    first._record_pending_delivery(fd.InFlight(
        "USR-539-A", "notebook", _Handle(), run_id="run-resume-only",
        candidate_digest="candidate", project_id="product-x",
    ))
    launches: list[str] = []
    dispatcher = fd.FleetDispatcher(
        config, demands_loader=lambda: [{"id": "USR-539-B", "status": "planned"}],
        launch=lambda ticket, _host, **_kwargs: launches.append(ticket),
        deploy=lambda: 0, clock=lambda: now, admission_store=store,
    )
    dispatcher._resumptions = lambda: []
    result = dispatcher.run_cycle()

    assert result["mode"] == "resume_only"
    assert result["deployed"] is True
    assert launches == []
