"""Pure admission policy and host selection for the distributed DarkFac fleet (USR-280 / USR-277).

Governs admission of tickets across up to three independent hosts:
Grokbox (Priority 1) > Notebook (Priority 2) > Desktop (Priority 3).

Key contracts:
1. Pure policy: operates on in-memory representations (HostConfig, HostState, TicketCandidate).
   No network or database side-effects in policy evaluation functions.
2. Capacity & Affinity: 1 cycle per host, maximum 3 concurrent cycles fleet-wide.
3. Non-blocking selection: a busy, offline, or draining host is skipped immediately;
   available capacity on subsequent hosts is allocated without delay.
4. Stable queue ordering: preserves ticket readiness and age (`created_at ASC`), with
   deterministic tie-breaking by `project_id` and `ticket_id`.
5. Fail-open diagnostic skipping: blocked or incompatible tickets are skipped with
   explicit diagnostic reasons without halting search for eligible candidates.
6. Decoupled from model routing: chooses host machines only; harness/model selection
   remains the sole responsibility of `core.line.routing.pick`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Optional, Sequence

from pydantic import BaseModel, Field

logger = logging.getLogger("darkfac.line.fleet_policy")

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _REPO_ROOT / ".factory" / "config" / "line_fleet.json"

HostStatus = Literal["online", "offline", "draining", "maintenance"]


class HostConfig(BaseModel):
    """Static configuration and capabilities of a fleet machine."""

    id: str = Field(description="Logical host identifier: grokbox, notebook, desktop")
    name: str = Field(description="Human-readable machine name")
    priority: int = Field(ge=1, description="Selection priority (1 is highest: Grokbox > Notebook > Desktop)")
    max_slots: int = Field(default=1, ge=1, description="Maximum concurrent demand cycles allowed on this host (default: 1)")
    platform: str = Field(default="windows", description="OS platform: linux or windows")
    tags: list[str] = Field(default_factory=list, description="Descriptive tags (e.g. remote, local, worker)")
    capabilities: list[str] = Field(default_factory=list, description="Declared capabilities/harnesses available on host")


class FleetConfig(BaseModel):
    """Overall fleet configuration and host topology."""

    max_fleet_slots: int = Field(default=3, ge=1, description="Global ceiling of concurrent cycles across all hosts (default: 3)")
    hosts: list[HostConfig] = Field(default_factory=list, description="Ordered host configurations")

    def get_host(self, host_id: str) -> Optional[HostConfig]:
        """Lookup host configuration by ID."""
        for host in self.hosts:
            if host.id == host_id:
                return host
        return None

    def sorted_hosts(self) -> list[HostConfig]:
        """Return hosts sorted in descending priority order (lowest numerical priority first)."""
        return sorted(self.hosts, key=lambda h: (h.priority, h.id))


class HostState(BaseModel):
    """Dynamic operational state of a host machine."""

    host_id: str
    status: HostStatus = "online"
    active_slots: int = Field(default=0, ge=0, description="Number of cycles currently running on this host")
    active_cycle_runs: list[str] = Field(default_factory=list, description="List of run/ticket IDs currently occupying slots")
    last_heartbeat: Optional[datetime] = None
    drain_reason: Optional[str] = None
    extra_capabilities: list[str] = Field(default_factory=list)

    @property
    def is_available(self) -> bool:
        """True when host is online and not draining or in maintenance."""
        return self.status == "online"

    def is_free(self, max_slots: int = 1) -> bool:
        """True when host is available and has at least one unused slot."""
        return self.is_available and self.active_slots < max_slots


class TicketCandidate(BaseModel):
    """Standardized representation of a demand candidate considered for admission."""

    ticket_id: str
    project_id: str = "darkfac"
    status: str = "planned"
    horizon: str = "now"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    dependencies: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    blocked_reason: Optional[str] = None
    required_capabilities: list[str] = Field(default_factory=list)
    required_platform: Optional[str] = None


class AdmissionDecision(BaseModel):
    """Result of an admission evaluation for a single ticket."""

    admitted: bool
    ticket_id: str
    project_id: str = "darkfac"
    host_id: Optional[str] = None
    reason: str = Field(default="", description="Diagnostic explanation for admission or rejection/skip")


class FleetAdmissionResult(BaseModel):
    """Aggregate result of an admission cycle over the queue."""

    admissions: list[AdmissionDecision] = Field(default_factory=list, description="Tickets admitted to free slots")
    skipped: list[AdmissionDecision] = Field(default_factory=list, description="Tickets skipped with diagnostic reasons")
    active_slots_total: int = 0
    max_fleet_slots: int = 3


def load_fleet_config(path: Optional[Path] = None) -> FleetConfig:
    """Load FleetConfig from JSON file or return canonical defaults."""
    cfg_path = path or DEFAULT_CONFIG_PATH
    if cfg_path.is_file():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            return FleetConfig.model_validate(raw)
        except Exception as exc:
            logger.warning("Failed to parse fleet config at %s: %s; using canonical defaults", cfg_path, exc)

    return FleetConfig(
        max_fleet_slots=3,
        hosts=[
            HostConfig(id="grokbox", name="Grokbox", priority=1, max_slots=1, platform="linux", tags=["remote", "grokbox"]),
            HostConfig(id="notebook", name="Notebook", priority=2, max_slots=1, platform="windows", tags=["local", "notebook"]),
            HostConfig(id="desktop", name="Desktop", priority=3, max_slots=1, platform="windows", tags=["remote", "desktop", "test_worker"]),
        ],
    )


def sort_queue(tickets: Iterable[TicketCandidate]) -> list[TicketCandidate]:
    """Sort ticket candidates deterministically by readiness, creation age, and tie-breakers.

    Ordering:
    1. Unblocked first (tickets with no blocked_reason)
    2. horizon == 'now' ahead of 'later'
    3. `created_at` ASC (oldest first)
    4. Deterministic tie-breaker: `project_id`, `ticket_id`
    """
    def _sort_key(t: TicketCandidate) -> tuple[int, int, float, str, str]:
        is_blocked = 1 if t.blocked_reason else 0
        horizon_rank = 0 if t.horizon == "now" else 1
        ts = t.created_at.timestamp() if t.created_at else 0.0
        return (is_blocked, horizon_rank, ts, t.project_id, t.ticket_id)

    return sorted(tickets, key=_sort_key)


def check_host_compatibility(host: HostConfig, host_state: HostState, ticket: TicketCandidate) -> tuple[bool, str]:
    """Verify if a host meets ticket requirements (platform, capabilities)."""
    # Platform requirement
    if ticket.required_platform and ticket.required_platform.lower() != host.platform.lower():
        return False, f"platform_mismatch: required {ticket.required_platform}, host has {host.platform}"

    # Required capabilities
    all_caps = set(host.capabilities) | set(host_state.extra_capabilities)
    missing_caps = [c for c in ticket.required_capabilities if c not in all_caps]
    if missing_caps:
        return False, f"missing_capabilities: {', '.join(missing_caps)}"

    return True, "compatible"


def select_host_for_ticket(
    hosts: Sequence[HostConfig],
    host_states: dict[str, HostState],
    ticket: TicketCandidate,
    current_fleet_slots: int,
    max_fleet_slots: int = 3,
    verified_capacity: Optional[dict[str, int]] = None,
) -> tuple[Optional[HostConfig], str]:
    """Select the highest-priority free and compatible host for a single ticket.

    Returns:
        (selected_host, diagnostic_reason)
    """
    if current_fleet_slots >= max_fleet_slots:
        return None, f"fleet_capacity_full: {current_fleet_slots}/{max_fleet_slots} slots active"

    # Sort hosts strictly by priority (1 Grokbox > 2 Notebook > 3 Desktop)
    sorted_hosts = sorted(hosts, key=lambda h: (h.priority, h.id))

    compatible_found = False
    busy_compatible_hosts: list[str] = []
    unhealthy_hosts: list[str] = []

    for host in sorted_hosts:
        state = host_states.get(host.id) or HostState(host_id=host.id)

        # Compatibility check
        ok, compat_msg = check_host_compatibility(host, state, ticket)
        if not ok:
            continue

        compatible_found = True

        # Health / availability checks
        if state.status == "offline":
            unhealthy_hosts.append(f"{host.id}:offline")
            continue
        if state.status == "draining":
            unhealthy_hosts.append(f"{host.id}:draining")
            continue
        if state.status == "maintenance":
            unhealthy_hosts.append(f"{host.id}:maintenance")
            continue

        # Never above verified capacity (e.g. free RAM); a busy worker is never reused or interrupted.
        limit = host.max_slots
        if verified_capacity is not None and host.id in verified_capacity:
            limit = min(limit, max(0, verified_capacity[host.id]))
        if state.active_slots >= limit:
            busy_compatible_hosts.append(f"{host.id}:{state.active_slots}/{limit}")
            continue

        # Found the highest-priority free host!
        return host, f"selected: host {host.id} (priority {host.priority})"

    if not compatible_found:
        return None, "no_compatible_hosts_declared"

    if busy_compatible_hosts:
        return None, f"all_compatible_hosts_busy: {', '.join(busy_compatible_hosts)}"

    return None, f"no_healthy_compatible_host: {', '.join(unhealthy_hosts)}"


def evaluate_admission(
    tickets: Sequence[TicketCandidate],
    fleet_config: FleetConfig,
    host_states: dict[str, HostState],
    completed_tickets: Optional[set[str]] = None,
    verified_capacity: Optional[dict[str, int]] = None,
) -> FleetAdmissionResult:
    """Evaluate pure fleet admission for candidate tickets across available hosts.

    Processes the queue in priority order, assigns eligible tickets to available host slots,
    and returns admissions alongside diagnostic reasons for any skipped tickets.
    """
    done_set = completed_tickets or set()
    sorted_q = sort_queue(tickets)

    # In-memory working copies of active slots to track incremental allocation during this tick
    working_host_slots = {
        h.id: (host_states.get(h.id).active_slots if host_states.get(h.id) else 0)
        for h in fleet_config.hosts
    }
    total_active_slots = sum(working_host_slots.values())
    max_slots = fleet_config.max_fleet_slots

    admissions: list[AdmissionDecision] = []
    skipped: list[AdmissionDecision] = []

    for ticket in sorted_q:
        # Ignore already completed or cancelled tickets
        if ticket.status in ("completed", "cancelled"):
            skipped.append(AdmissionDecision(
                admitted=False,
                ticket_id=ticket.ticket_id,
                project_id=ticket.project_id,
                reason=f"status_{ticket.status}",
            ))
            continue

        # Check explicit block reason
        if ticket.blocked_reason:
            skipped.append(AdmissionDecision(
                admitted=False,
                ticket_id=ticket.ticket_id,
                project_id=ticket.project_id,
                reason=f"blocked: {ticket.blocked_reason}",
            ))
            continue

        # Check dependency prerequisites
        unmet_deps = [dep for dep in ticket.dependencies if dep not in done_set]
        if unmet_deps:
            skipped.append(AdmissionDecision(
                admitted=False,
                ticket_id=ticket.ticket_id,
                project_id=ticket.project_id,
                reason=f"unmet_dependencies: {', '.join(unmet_deps)}",
            ))
            continue

        # Check if fleet capacity has been reached
        if total_active_slots >= max_slots:
            skipped.append(AdmissionDecision(
                admitted=False,
                ticket_id=ticket.ticket_id,
                project_id=ticket.project_id,
                reason=f"fleet_capacity_full: {total_active_slots}/{max_slots}",
            ))
            continue

        # Construct ephemeral state for selection reflecting incremental allocation in this cycle
        ephemeral_states: dict[str, HostState] = {}
        for h in fleet_config.hosts:
            base_st = host_states.get(h.id) or HostState(host_id=h.id)
            ephemeral_states[h.id] = base_st.model_copy(update={"active_slots": working_host_slots[h.id]})

        selected_host, diag_reason = select_host_for_ticket(
            hosts=fleet_config.hosts,
            host_states=ephemeral_states,
            ticket=ticket,
            current_fleet_slots=total_active_slots,
            max_fleet_slots=max_slots,
            verified_capacity=verified_capacity,
        )

        if selected_host is not None:
            # Slot granted!
            working_host_slots[selected_host.id] += 1
            total_active_slots += 1
            admissions.append(AdmissionDecision(
                admitted=True,
                ticket_id=ticket.ticket_id,
                project_id=ticket.project_id,
                host_id=selected_host.id,
                reason=diag_reason,
            ))
        else:
            skipped.append(AdmissionDecision(
                admitted=False,
                ticket_id=ticket.ticket_id,
                project_id=ticket.project_id,
                reason=diag_reason,
            ))

    return FleetAdmissionResult(
        admissions=admissions,
        skipped=skipped,
        active_slots_total=total_active_slots,
        max_fleet_slots=max_slots,
    )
