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

from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from core.line.fleet_policy import (
    FleetConfig,
    HostState,
    TicketCandidate,
    evaluate_admission,
    load_fleet_config,
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
