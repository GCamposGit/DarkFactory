"""Unit tests for the distributed fleet admission policy (USR-280 / USR-277).

Covers:
- Priority order Notebook (1) > Grokbox (2) > Desktop (3)
- Non-blocking host selection when hosts are busy, offline, or draining
- Capacity limits: 1 slot per host, maximum 3 in fleet
- Queue sorting, readiness, age preservation, and stable tie-breakers
- Dependency checking and diagnostic reason reporting
- Incompatibility / no route handling
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from core.line.fleet_policy import (
    FleetConfig,
    HostConfig,
    HostState,
    TicketCandidate,
    evaluate_admission,
    effective_host_policy,
    load_fleet_config,
    policy_snapshot_report,
    select_host_for_ticket,
    sort_queue,
)


def _assert_canonical_order(cfg: FleetConfig) -> None:
    ordered = cfg.sorted_hosts()
    assert [h.id for h in ordered] == ["notebook", "grokbox", "desktop"]
    assert [h.priority for h in ordered] == [1, 2, 3]
    assert [h.max_slots for h in ordered] == [1, 1, 1]
    assert cfg.max_fleet_slots == 3


@pytest.fixture
def default_fleet() -> FleetConfig:
    return load_fleet_config()


def test_load_fleet_config(tmp_path: Path):
    """Config vigente e fallback de JSON invalido usam a mesma ordem canonica."""
    _assert_canonical_order(load_fleet_config())

    bad_file = tmp_path / "bad.json"
    bad_file.write_text("invalid json")
    _assert_canonical_order(load_fleet_config(bad_file))


def test_sort_queue_ordering():
    """Verify queue sorts unblocked, now horizon, oldest first, with stable tie-breaker."""
    base_time = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    
    t_blocked = TicketCandidate(ticket_id="USR-100", created_at=base_time, blocked_reason="waiting_human")
    t_later = TicketCandidate(ticket_id="USR-101", created_at=base_time, horizon="later")
    t_older = TicketCandidate(ticket_id="USR-102", created_at=base_time - timedelta(hours=2))
    t_newer_a = TicketCandidate(ticket_id="USR-103", created_at=base_time, project_id="darkfac")
    t_newer_b = TicketCandidate(ticket_id="USR-104", created_at=base_time, project_id="atrium")

    queue = [t_blocked, t_later, t_newer_a, t_newer_b, t_older]
    sorted_q = sort_queue(queue)
    
    # Expected order:
    # 1. t_older (unblocked, now, oldest)
    # 2. t_newer_b (unblocked, now, atrium < darkfac)
    # 3. t_newer_a (unblocked, now, darkfac)
    # 4. t_later (unblocked, later)
    # 5. t_blocked (blocked)
    assert [t.ticket_id for t in sorted_q] == ["USR-102", "USR-104", "USR-103", "USR-101", "USR-100"]


def _online_states() -> dict[str, HostState]:
    return {
        "notebook": HostState(host_id="notebook", status="online", active_slots=0),
        "grokbox": HostState(host_id="grokbox", status="online", active_slots=0),
        "desktop": HostState(host_id="desktop", status="online", active_slots=0),
    }


def test_three_hosts_free_order(default_fleet: FleetConfig):
    """Com os tres livres, o mais antigo vai ao Notebook, o seguinte ao Grokbox e o ultimo ao Desktop."""
    base_time = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    t1 = TicketCandidate(ticket_id="USR-280", created_at=base_time)
    t2 = TicketCandidate(ticket_id="USR-281", created_at=base_time + timedelta(seconds=1))
    t3 = TicketCandidate(ticket_id="USR-282", created_at=base_time + timedelta(seconds=2))

    res = evaluate_admission([t1, t2, t3], default_fleet, _online_states())
    assert len(res.admissions) == 3
    assert len(res.skipped) == 0
    assert res.active_slots_total == 3

    assert res.admissions[0].ticket_id == "USR-280"
    assert res.admissions[0].host_id == "notebook"

    assert res.admissions[1].ticket_id == "USR-281"
    assert res.admissions[1].host_id == "grokbox"

    assert res.admissions[2].ticket_id == "USR-282"
    assert res.admissions[2].host_id == "desktop"


def test_notebook_busy_dispatches_to_grokbox(default_fleet: FleetConfig):
    """Notebook ocupado entrega o ticket ao Grokbox sem esperar."""
    t = TicketCandidate(ticket_id="USR-280")
    host_states = _online_states()
    host_states["notebook"] = HostState(host_id="notebook", status="online", active_slots=1, active_cycle_runs=["run-1"])

    res = evaluate_admission([t], default_fleet, host_states)
    assert len(res.admissions) == 1
    assert res.admissions[0].ticket_id == "USR-280"
    assert res.admissions[0].host_id == "grokbox"


def test_notebook_and_grokbox_busy_dispatches_to_desktop(default_fleet: FleetConfig):
    """Notebook e Grokbox ocupados entregam o ticket ao Desktop."""
    t = TicketCandidate(ticket_id="USR-280")
    host_states = _online_states()
    host_states["notebook"] = HostState(host_id="notebook", status="online", active_slots=1)
    host_states["grokbox"] = HostState(host_id="grokbox", status="online", active_slots=1)

    res = evaluate_admission([t], default_fleet, host_states)
    assert len(res.admissions) == 1
    assert res.admissions[0].ticket_id == "USR-280"
    assert res.admissions[0].host_id == "desktop"


def test_offline_host_is_skipped_and_admission_continues(default_fleet: FleetConfig):
    """Host offline ou em drain e pulado e a admissao segue para o proximo saudavel."""
    t = TicketCandidate(ticket_id="USR-280")
    notebook_offline = _online_states()
    notebook_offline["notebook"] = HostState(host_id="notebook", status="offline")

    res = evaluate_admission([t], default_fleet, notebook_offline)
    assert len(res.admissions) == 1
    assert res.admissions[0].ticket_id == "USR-280"
    assert res.admissions[0].host_id == "grokbox"

    both_unavailable = _online_states()
    both_unavailable["notebook"] = HostState(host_id="notebook", status="draining", drain_reason="reboot_pending")
    both_unavailable["grokbox"] = HostState(host_id="grokbox", status="offline")
    res_desktop = evaluate_admission([t], default_fleet, both_unavailable)
    assert len(res_desktop.admissions) == 1
    assert res_desktop.admissions[0].host_id == "desktop"


def test_fleet_capacity_full_stops_admission(default_fleet: FleetConfig):
    """When all 3 slots are occupied, additional tickets are reported as skipped with capacity full."""
    t = TicketCandidate(ticket_id="USR-285")
    host_states = {
        "grokbox": HostState(host_id="grokbox", status="online", active_slots=1),
        "notebook": HostState(host_id="notebook", status="online", active_slots=1),
        "desktop": HostState(host_id="desktop", status="online", active_slots=1),
    }

    res = evaluate_admission([t], default_fleet, host_states)
    assert len(res.admissions) == 0
    assert len(res.skipped) == 1
    assert "fleet_capacity_full" in res.skipped[0].reason


def test_dependency_checking_skips_unmet(default_fleet: FleetConfig):
    """A ticket with unmet dependency is skipped with diagnostic reason, allowing next eligible ticket to run."""
    t_blocked_dep = TicketCandidate(ticket_id="USR-281", dependencies=["USR-280"])
    t_ready = TicketCandidate(ticket_id="USR-289", dependencies=[])

    host_states = {
        "grokbox": HostState(host_id="grokbox", status="online", active_slots=0),
        "notebook": HostState(host_id="notebook", status="online", active_slots=0),
        "desktop": HostState(host_id="desktop", status="online", active_slots=0),
    }

    # USR-280 is not in completed_tickets set
    res = evaluate_admission([t_blocked_dep, t_ready], default_fleet, host_states, completed_tickets=set())
    assert len(res.admissions) == 1
    assert res.admissions[0].ticket_id == "USR-289"
    assert res.admissions[0].host_id == "notebook"

    assert len(res.skipped) == 1
    assert res.skipped[0].ticket_id == "USR-281"
    assert "unmet_dependencies: USR-280" in res.skipped[0].reason

    # When USR-280 is completed, USR-281 is admitted
    res_done = evaluate_admission([t_blocked_dep], default_fleet, host_states, completed_tickets={"USR-280"})
    assert len(res_done.admissions) == 1
    assert res_done.admissions[0].ticket_id == "USR-281"


def test_platform_incompatibility_routing(default_fleet: FleetConfig):
    """Tickets requiring Linux only match Grokbox; Windows tickets match Notebook or Desktop."""
    t_linux = TicketCandidate(ticket_id="USR-L", required_platform="linux")
    t_win = TicketCandidate(ticket_id="USR-W", required_platform="windows")

    # If Grokbox is busy, Linux ticket cannot be assigned to Windows hosts
    host_states = {
        "grokbox": HostState(host_id="grokbox", status="online", active_slots=1),
        "notebook": HostState(host_id="notebook", status="online", active_slots=0),
        "desktop": HostState(host_id="desktop", status="online", active_slots=0),
    }

    res_linux = evaluate_admission([t_linux], default_fleet, host_states)
    assert len(res_linux.admissions) == 0
    assert "all_compatible_hosts_busy" in res_linux.skipped[0].reason

    # Windows ticket can proceed to Notebook
    res_win = evaluate_admission([t_win], default_fleet, host_states)
    assert len(res_win.admissions) == 1
    assert res_win.admissions[0].host_id == "notebook"


def test_missing_capabilities_rejected(default_fleet: FleetConfig):
    """Ticket requiring capabilities unavailable in fleet is skipped with missing_capabilities."""
    t = TicketCandidate(ticket_id="USR-SPECIAL", required_capabilities=["harness:quantum-agi"])
    host_states = {
        "grokbox": HostState(host_id="grokbox", status="online", active_slots=0),
        "notebook": HostState(host_id="notebook", status="online", active_slots=0),
        "desktop": HostState(host_id="desktop", status="online", active_slots=0),
    }

    res = evaluate_admission([t], default_fleet, host_states)
    assert len(res.admissions) == 0
    assert len(res.skipped) == 1
    assert "no_compatible_hosts_declared" in res.skipped[0].reason


def test_effective_policy_exclusions_stale_resources_and_reservation_expiry(default_fleet: FleetConfig):
    now = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
    current = now.isoformat()
    readings = {
        "grokbox": {"workers": 1, "available_gib": 8, "observed_at": current},
        "notebook": {"workers": 1, "available_gib": 2.89, "observed_at": current},
        "desktop": {"workers": 1, "available_gib": 16, "observed_at": (now - timedelta(seconds=90)).isoformat()},
    }
    config = default_fleet.model_copy(update={"disabled_hosts": ["grokbox"]})
    rows = effective_host_policy(
        config,
        disabled_from_environment=["desktop"],
        readings=readings,
        reservations={
            "notebook": {"active": True, "expires_at": (now - timedelta(seconds=1)).isoformat()},
        },
        now=now,
    )
    by_id = {row["host_id"]: row for row in rows}
    assert by_id["grokbox"]["eligible"] is False
    assert by_id["grokbox"]["exclusion_sources"] == ["config.disabled_hosts"]
    assert by_id["notebook"]["eligible"] is True  # exactly one worker; expired reservation no longer blocks
    assert by_id["desktop"]["eligible"] is False
    assert by_id["desktop"]["reason"] == "excluded"
    assert by_id["desktop"]["exclusion_sources"] == ["DARKFAC_FLEET_DISABLED_HOSTS"]


def test_effective_policy_rejects_zero_workers_and_active_canonical_reservation(default_fleet: FleetConfig):
    now = datetime.now(timezone.utc)
    rows = effective_host_policy(
        default_fleet,
        readings={
            "grokbox": {"workers": 3, "available_gib": 32, "observed_at": now.isoformat()},
            "notebook": {"workers": 0, "available_gib": 2.89, "observed_at": now.isoformat()},
            "desktop": {"workers": 1, "available_gib": 16, "observed_at": now.isoformat()},
        },
        reservations={"desktop": {"active": True, "expires_at": (now + timedelta(seconds=20)).isoformat()}},
        now=now,
    )
    by_id = {row["host_id"]: row for row in rows}
    assert by_id["grokbox"]["eligible"] is True  # measured capacity is capped to the configured one worker
    assert by_id["grokbox"]["workers_calculated"] == 1
    assert by_id["notebook"]["reason"] == "zero_workers"
    assert by_id["desktop"]["reason"] == "reservation_active"


def test_config_disabled_host_is_exposed_as_exclusion_even_when_not_in_disabled_hosts():
    now = datetime.now(timezone.utc)
    config = FleetConfig(hosts=[
        HostConfig(id="desktop", name="Desktop", priority=1, enabled=False,
                   endpoint="http://desktop:8080"),
    ])
    decisions = effective_host_policy(config, now=now)

    assert decisions[0]["eligible"] is False
    assert decisions[0]["reason"] == "disabled_by_config"
    assert decisions[0]["exclusion_sources"] == ["config.hosts.enabled=false"]

    report = policy_snapshot_report(
        config, decisions, now=now, config_source="line_fleet.json",
    )
    assert report["origin"]["exclusions"]["config"] == ["desktop"]


def test_effective_policy_fails_closed_for_missing_or_stale_resource_reading(default_fleet: FleetConfig):
    now = datetime.now(timezone.utc)
    rows = effective_host_policy(
        default_fleet,
        readings={"grokbox": {"workers": 1, "observed_at": (now - timedelta(seconds=91)).isoformat()}},
        now=now,
    )
    by_id = {row["host_id"]: row for row in rows}
    assert by_id["grokbox"]["reason"] == "resource_reading_missing_or_stale"
    assert by_id["notebook"]["reason"] == "resource_reading_missing_or_stale"


def test_effective_policy_fails_closed_when_canonical_reservation_store_is_missing(
    fleet_with_endpoints: FleetConfig, monkeypatch, tmp_path: Path
):
    from core.line import fleet_policy

    now = datetime.now(timezone.utc)
    monkeypatch.setattr("core.paths.state_root", lambda: tmp_path)
    monkeypatch.setattr(
        fleet_policy,
        "measure_resource_readings",
        lambda cfg: {
            "grokbox": {"workers": 1, "available_gib": 8.0, "observed_at": now.isoformat()},
            "notebook": {"workers": 1, "available_gib": 8.0, "observed_at": now.isoformat()},
            "desktop": {"workers": 1, "available_gib": 8.0, "observed_at": now.isoformat()},
        },
    )

    report = fleet_policy.build_effective_policy_report(fleet_with_endpoints, now=now)
    by_id = {row["host_id"]: row for row in report["hosts"]}

    assert report["reservation_state"] == "unavailable"
    assert by_id["grokbox"]["eligible"] is False
    assert by_id["grokbox"]["reason"] == "reservation_state_unavailable"
    assert by_id["notebook"]["eligible"] is False
    assert by_id["notebook"]["reason"] == "reservation_state_unavailable"
    assert by_id["desktop"]["eligible"] is False
    assert by_id["desktop"]["reason"] == "reservation_state_unavailable"


# --- live effective policy: measurement, snapshot, CLI (USR-471) --------------------------------


@pytest.fixture
def fleet_with_endpoints() -> FleetConfig:
    """Mirrors `.factory/config/line_fleet.json`: the local host (notebook) has no `endpoint`;
    grokbox and desktop are remote workers probed over HTTP."""
    return FleetConfig(
        max_fleet_slots=3,
        hosts=[
            HostConfig(id="grokbox", name="Grokbox", priority=1, platform="linux",
                       endpoint="http://100.77.130.5:8080"),
            HostConfig(id="notebook", name="Notebook", priority=2, platform="windows",
                       endpoint=None, min_free_memory_gib=3.0),
            HostConfig(id="desktop", name="Desktop", priority=3, platform="windows",
                       endpoint="http://100.78.181.90:8080"),
        ],
    )


def test_measure_resource_readings_local_via_gate_ram_remote_via_probe(
    fleet_with_endpoints: FleetConfig, monkeypatch
):
    """The endpoint-less host is measured locally (gate_ram); an offline remote worker
    (Grokbox) yields no reading at all instead of a fabricated one."""
    from core.line import fleet_policy, gate_ram

    monkeypatch.setattr(gate_ram, "measure_workers", lambda: gate_ram.RamReading(available_gib=2.89, workers=0))

    def fake_probe_health(url: str, timeout: float | None = None):
        if "100.77.130.5" in url:
            return None  # Grokbox offline
        return {"workers": 1, "available_gib": 16.0, "observed_at": datetime.now(timezone.utc).isoformat()}

    monkeypatch.setattr("core.harness.remote_dispatch.probe_health", fake_probe_health)

    readings = fleet_policy.measure_resource_readings(fleet_with_endpoints)
    assert readings["notebook"] == {"workers": 0, "available_gib": 2.89, "observed_at": readings["notebook"]["observed_at"]}
    assert "grokbox" not in readings
    assert readings["desktop"]["workers"] == 1


def test_build_effective_policy_report_exclusion_precedes_health(
    fleet_with_endpoints: FleetConfig, monkeypatch
):
    """A healthy, idle Desktop stays ineligible once excluded by environment; a Grokbox with no
    reading at all (offline) fails closed; Notebook's measured 2.89 GiB yields zero workers."""
    from core.line import fleet_policy

    now = datetime.now(timezone.utc)

    def fake_measure(cfg: FleetConfig) -> dict:
        return {
            "notebook": {"workers": 0, "available_gib": 2.89, "observed_at": now.isoformat()},
            "desktop": {"workers": 1, "available_gib": 16.0, "observed_at": now.isoformat()},
        }

    monkeypatch.setattr(fleet_policy, "measure_resource_readings", fake_measure)
    report = fleet_policy.build_effective_policy_report(
        fleet_with_endpoints, disabled_from_environment=["desktop"], now=now,
    )
    assert report["schema_version"] == fleet_policy.SNAPSHOT_SCHEMA_VERSION
    assert report["generated_at"] == now.isoformat()
    by_id = {row["host_id"]: row for row in report["hosts"]}
    assert by_id["desktop"]["eligible"] is False
    assert by_id["desktop"]["reason"] == "excluded"
    assert by_id["desktop"]["exclusion_sources"] == ["DARKFAC_FLEET_DISABLED_HOSTS"]
    assert by_id["notebook"]["reason"] == "zero_workers"
    assert by_id["grokbox"]["reason"] == "resource_reading_missing_or_stale"


def test_effective_host_policy_redacts_endpoint_secrets_but_keeps_worker_origin():
    now = datetime.now(timezone.utc)
    config = FleetConfig(hosts=[HostConfig(
        id="desktop", name="Desktop", priority=1,
        endpoint="https://worker-user:credential@desktop.internal:8443/run?token=secret-value#fragment",
        max_workers=2, min_free_memory_gib=4,
    )])

    report = effective_host_policy(
        config,
        readings={"desktop": {
            "workers": 1, "available_gib": 8, "observed_at": now.isoformat(),
            "endpoint_token": "secret-reading-token",
        }},
        now=now,
    )[0]

    assert report["config"]["endpoint"] == "https://desktop.internal:8443"
    assert report["config"]["max_workers"] == 2
    assert report["config"]["min_free_memory_gib"] == 4
    assert "credential" not in json.dumps(report)
    assert "secret-value" not in json.dumps(report)
    assert "secret-reading-token" not in json.dumps(report)


def test_measure_resource_readings_never_probes_excluded_host(
    fleet_with_endpoints: FleetConfig, monkeypatch
):
    from core.line import fleet_policy

    monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", "desktop")
    probed: list[str] = []

    def fake_probe(url: str, timeout: float | None = None):
        probed.append(url)
        return {"workers": 1, "available_gib": 8.0, "observed_at": datetime.now(timezone.utc).isoformat()}

    monkeypatch.setattr("core.harness.remote_dispatch.probe_health", fake_probe)
    readings = fleet_policy.measure_resource_readings(fleet_with_endpoints)

    assert "http://100.78.181.90:8080" not in probed
    assert "desktop" not in readings


def test_remote_resource_reading_without_timestamp_remains_stale(
    fleet_with_endpoints: FleetConfig, monkeypatch
):
    """A successful health response without an observation timestamp cannot be
    promoted to a fresh resource reading by the collector."""
    from core.line import fleet_policy

    monkeypatch.setattr(
        "core.harness.remote_dispatch.probe_health",
        lambda _url, timeout=None: {"workers": 1, "available_gib": 8.0},
    )

    readings = fleet_policy.measure_resource_readings(fleet_with_endpoints)

    assert readings["grokbox"]["observed_at"] is None
    rows = fleet_policy.effective_host_policy(fleet_with_endpoints, readings=readings)
    by_id = {row["host_id"]: row for row in rows}
    assert by_id["grokbox"]["eligible"] is False
    assert by_id["grokbox"]["reason"] == "resource_reading_missing_or_stale"


def test_effective_policy_reports_occupancy_and_active_reservation(
    fleet_with_endpoints: FleetConfig, monkeypatch, tmp_path: Path
):
    from core.line import fleet_policy

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        fleet_policy,
        "measure_resource_readings",
        lambda cfg: {
            "grokbox": {"workers": 1, "available_gib": 8.0, "busy": True, "observed_at": now.isoformat()},
            "notebook": {"workers": 1, "available_gib": 8.0, "observed_at": now.isoformat()},
            "desktop": {"workers": 1, "available_gib": 8.0, "observed_at": now.isoformat()},
        },
    )

    class FakeStore:
        def __init__(self, _path):
            pass

        def active_host_reservations(self):
            return [{"host_id": "desktop", "expires_at": (now + timedelta(minutes=1)).isoformat()}]

    from datetime import timedelta

    db_path = tmp_path / "admission.sqlite3"
    db_path.write_bytes(b"placeholder")
    monkeypatch.setattr("core.paths.state_root", lambda: tmp_path)
    monkeypatch.setattr("core.line.admission_store.AdmissionStore", FakeStore)

    report = fleet_policy.build_effective_policy_report(fleet_with_endpoints, now=now)
    by_id = {row["host_id"]: row for row in report["hosts"]}
    assert by_id["grokbox"]["reason"] == "occupied"
    assert by_id["grokbox"]["occupied_by"] == "remote_worker_busy"
    assert by_id["desktop"]["reason"] == "reservation_active"
    assert by_id["desktop"]["canonical_reservation"]["active"] is True


def test_effective_policy_snapshot_persists_and_round_trips(tmp_path: Path, monkeypatch):
    """The persisted snapshot is exactly the report returned, written atomically to `path`."""
    from core.line import fleet_policy

    config = FleetConfig(hosts=[HostConfig(id="notebook", name="Notebook", priority=1, platform="windows")])
    now = datetime.now(timezone.utc)
    (tmp_path / "admission.sqlite3").write_bytes(b"placeholder")
    monkeypatch.setattr("core.paths.state_root", lambda: tmp_path)

    class AvailableStore:
        def __init__(self, _path):
            pass

        def active_host_reservations(self):
            return []

    monkeypatch.setattr("core.line.admission_store.AdmissionStore", AvailableStore)
    monkeypatch.setattr(
        fleet_policy, "measure_resource_readings",
        lambda cfg: {"notebook": {"workers": 2, "available_gib": 8.0, "observed_at": now.isoformat()}},
    )
    target = tmp_path / "fleet_policy_snapshot.json"
    report = fleet_policy.effective_policy_snapshot(config, path=target)

    assert target.is_file()
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    assert on_disk == report
    assert report["hosts"][0]["host_id"] == "notebook"
    assert report["hosts"][0]["eligible"] is True
    assert report["updated_at"] == report["generated_at"]
    assert report["origin"]["configuration"] == str(fleet_policy.DEFAULT_CONFIG_PATH)
    assert report["origin"]["resource_readings"] == "live_health_and_gate_ram"
    assert report["hosts"][0]["workers_calculated"] == 1


def test_snapshot_path_uses_explicit_state_root(tmp_path: Path, monkeypatch):
    from core.line import fleet_policy

    state = tmp_path / "persistent-state"
    monkeypatch.setenv("STATE_ROOT", str(state))

    assert fleet_policy.snapshot_path() == state / "runs" / "fleet_policy_snapshot.json"


def test_atomic_snapshot_write_preserves_previous_report_on_serialization_error(tmp_path: Path):
    from core.line import fleet_policy

    target = tmp_path / "runs" / "fleet_policy_snapshot.json"
    target.parent.mkdir()
    target.write_text('{"previous":true}', encoding="utf-8")

    with pytest.raises(TypeError):
        fleet_policy.write_effective_policy_snapshot({"bad": object()}, path=target)

    assert target.read_text(encoding="utf-8") == '{"previous":true}'
    assert list(target.parent.iterdir()) == [target]


def test_cli_effective_prints_json_and_writes_snapshot(tmp_path: Path, monkeypatch, capsys):
    from core.line import fleet_policy

    config = FleetConfig(hosts=[HostConfig(id="notebook", name="Notebook", priority=1, platform="windows")])
    now = datetime.now(timezone.utc)
    (tmp_path / "admission.sqlite3").write_bytes(b"placeholder")
    monkeypatch.setattr("core.paths.state_root", lambda: tmp_path)

    class AvailableStore:
        def __init__(self, _path):
            pass

        def active_host_reservations(self):
            return []

    monkeypatch.setattr("core.line.admission_store.AdmissionStore", AvailableStore)
    monkeypatch.setattr(fleet_policy, "load_fleet_config", lambda *a, **k: config)
    monkeypatch.setattr(
        fleet_policy, "measure_resource_readings",
        lambda cfg: {"notebook": {"workers": 1, "available_gib": 8.0, "observed_at": now.isoformat()}},
    )
    snapshot_target = tmp_path / "snap.json"
    monkeypatch.setattr(fleet_policy, "snapshot_path", lambda root=None: snapshot_target)

    rc = fleet_policy._cli(["--effective"])

    assert rc == 0
    assert snapshot_target.is_file()
    printed = json.loads(capsys.readouterr().out)
    assert printed["hosts"][0]["host_id"] == "notebook"
    assert printed["hosts"][0]["eligible"] is True


def _hub_service(tmp_path: Path):
    from hub.backend.service import HubService

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "default_services.json").write_text("[]", encoding="utf-8")
    (data_dir / "default_prompts.json").write_text("[]", encoding="utf-8")
    return HubService(
        data_dir=data_dir,
        usage_dir=tmp_path / "usage",
        roadmap_root=tmp_path,
        state_path=tmp_path / "state.json",
        orchestrator_path=tmp_path / "orchestrator.sqlite3",
        control_db_path=tmp_path / "missing" / "control.db",
        control_database_url="",
    )


def test_hub_endpoint_serves_the_same_effective_policy_content(tmp_path: Path, monkeypatch):
    """The Hub endpoint (`HubService.get_fleet_policy`) serves exactly what
    `effective_policy_snapshot` computes -- same source, no second dispatcher."""
    expected = {"schema_version": 1, "generated_at": "now", "max_fleet_slots": 3, "hosts": []}
    monkeypatch.setattr("core.line.fleet_policy.effective_policy_snapshot", lambda *a, **k: expected)

    service = _hub_service(tmp_path)
    assert service.get_fleet_policy() == expected


def test_hub_endpoint_degrades_instead_of_raising_when_policy_computation_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "core.line.fleet_policy.effective_policy_snapshot",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    service = _hub_service(tmp_path)
    report = service.get_fleet_policy()

    assert report["hosts"] == []
    assert "boom" in report["error"]
