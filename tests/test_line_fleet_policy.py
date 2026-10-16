"""Unit tests for the distributed fleet admission policy (USR-280 / USR-277).

Covers:
- Priority order Grokbox (1) > Notebook (2) > Desktop (3)
- Non-blocking host selection when hosts are busy, offline, or draining
- Capacity limits: 1 slot per host, maximum 3 in fleet
- Queue sorting, readiness, age preservation, and stable tie-breakers
- Dependency checking and diagnostic reason reporting
- Incompatibility / no route handling
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from core.line.fleet_policy import (
    AdmissionDecision,
    FleetAdmissionResult,
    FleetConfig,
    HostConfig,
    HostState,
    TicketCandidate,
    check_host_compatibility,
    evaluate_admission,
    load_fleet_config,
    select_host_for_ticket,
    sort_queue,
)


@pytest.fixture
def default_fleet() -> FleetConfig:
    return FleetConfig(
        max_fleet_slots=3,
        hosts=[
            HostConfig(id="grokbox", name="Grokbox", priority=1, max_slots=1, platform="linux", tags=["remote", "grokbox"]),
            HostConfig(id="notebook", name="Notebook", priority=2, max_slots=1, platform="windows", tags=["local", "notebook"]),
            HostConfig(id="desktop", name="Desktop", priority=3, max_slots=1, platform="windows", tags=["remote", "desktop", "test_worker"]),
        ],
    )


def test_load_fleet_config(tmp_path: Path):
    """Test loading configuration from line_fleet.json with fallback."""
    cfg = load_fleet_config()
    assert cfg.max_fleet_slots == 3
    assert len(cfg.hosts) == 3
    host_ids = [h.id for h in cfg.sorted_hosts()]
    assert host_ids == ["grokbox", "notebook", "desktop"]

    # Test fallback on invalid file
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("invalid json")
    fallback_cfg = load_fleet_config(bad_file)
    assert fallback_cfg.max_fleet_slots == 3
    assert len(fallback_cfg.hosts) == 3


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


def test_three_hosts_free_order(default_fleet: FleetConfig):
    """When all three hosts are free, three tickets fill Grokbox, Notebook, and Desktop in order."""
    base_time = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    t1 = TicketCandidate(ticket_id="USR-280", created_at=base_time)
    t2 = TicketCandidate(ticket_id="USR-281", created_at=base_time + timedelta(seconds=1))
    t3 = TicketCandidate(ticket_id="USR-282", created_at=base_time + timedelta(seconds=2))

    host_states = {
        "grokbox": HostState(host_id="grokbox", status="online", active_slots=0),
        "notebook": HostState(host_id="notebook", status="online", active_slots=0),
        "desktop": HostState(host_id="desktop", status="online", active_slots=0),
    }

    res = evaluate_admission([t1, t2, t3], default_fleet, host_states)
    assert len(res.admissions) == 3
    assert len(res.skipped) == 0
    assert res.active_slots_total == 3

    assert res.admissions[0].ticket_id == "USR-280"
    assert res.admissions[0].host_id == "grokbox"

    assert res.admissions[1].ticket_id == "USR-281"
    assert res.admissions[1].host_id == "notebook"

    assert res.admissions[2].ticket_id == "USR-282"
    assert res.admissions[2].host_id == "desktop"


def test_grokbox_busy_dispatches_to_notebook(default_fleet: FleetConfig):
    """When Grokbox is busy, ticket immediately goes to Notebook without blocking."""
    t = TicketCandidate(ticket_id="USR-280")
    host_states = {
        "grokbox": HostState(host_id="grokbox", status="online", active_slots=1, active_cycle_runs=["run-1"]),
        "notebook": HostState(host_id="notebook", status="online", active_slots=0),
        "desktop": HostState(host_id="desktop", status="online", active_slots=0),
    }

    res = evaluate_admission([t], default_fleet, host_states)
    assert len(res.admissions) == 1
    assert res.admissions[0].ticket_id == "USR-280"
    assert res.admissions[0].host_id == "notebook"


def test_grokbox_and_notebook_busy_dispatches_to_desktop(default_fleet: FleetConfig):
    """When Grokbox and Notebook are busy, ticket immediately goes to Desktop."""
    t = TicketCandidate(ticket_id="USR-280")
    host_states = {
        "grokbox": HostState(host_id="grokbox", status="online", active_slots=1),
        "notebook": HostState(host_id="notebook", status="online", active_slots=1),
        "desktop": HostState(host_id="desktop", status="online", active_slots=0),
    }

    res = evaluate_admission([t], default_fleet, host_states)
    assert len(res.admissions) == 1
    assert res.admissions[0].ticket_id == "USR-280"
    assert res.admissions[0].host_id == "desktop"


def test_offline_and_draining_hosts_are_skipped(default_fleet: FleetConfig):
    """Offline and draining hosts are bypassed in favor of next available healthy host."""
    t = TicketCandidate(ticket_id="USR-280")
    host_states = {
        "grokbox": HostState(host_id="grokbox", status="offline"),
        "notebook": HostState(host_id="notebook", status="draining", drain_reason="reboot_pending"),
        "desktop": HostState(host_id="desktop", status="online", active_slots=0),
    }

    res = evaluate_admission([t], default_fleet, host_states)
    assert len(res.admissions) == 1
    assert res.admissions[0].ticket_id == "USR-280"
    assert res.admissions[0].host_id == "desktop"


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
    assert res.admissions[0].host_id == "grokbox"

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
