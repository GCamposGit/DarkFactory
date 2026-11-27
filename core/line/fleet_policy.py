"""Pure admission policy and host selection for the distributed DarkFac fleet (USR-280 / USR-277).

Governs admission of tickets across up to three independent hosts:
Notebook (Priority 1) > Grokbox (Priority 2) > Desktop (Priority 3).

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

``effective_host_policy`` above is the pure evaluator (contract 1: no I/O). A live host
reservation blocks that host unless ``owner_run_id`` matches the lease's ``run_id``. A lease
with a blank owner, or any other run's lease, stays blocking. USR-471 adds the one impure
entry point every caller outside the fleet dispatcher uses instead of re-deriving it:
``effective_policy_snapshot`` gathers live resource readings (``measure_resource_readings``),
combines them with ``line_fleet.json`` and ``DARKFAC_FLEET_DISABLED_HOSTS`` through
``effective_host_policy``, and persists the result. ``build_effective_policy_report`` passes
``DARKFAC_RUN_ID`` as the owner so the gate that holds the lease can still use that host.
Callers that omit the owner, including the dispatcher admitting a new ticket, keep every
live lease blocking. A host excluded by configuration or by environment is never eligible
regardless of health (exclusion precedes the health/resource checks in
``effective_host_policy`` itself). The persisted snapshot is regenerated on every read --
it is audit visibility, never a run's state.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Optional, Sequence

from pydantic import BaseModel, Field

logger = logging.getLogger("darkfac.line.fleet_policy")

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _REPO_ROOT / ".factory" / "config" / "line_fleet.json"
#: USR-471: schema of `build_effective_policy_report`'s output / the persisted snapshot.
SNAPSHOT_SCHEMA_VERSION = 1
ENV_FLEET_DISABLED_HOSTS = "DARKFAC_FLEET_DISABLED_HOSTS"

HostStatus = Literal["online", "offline", "draining", "maintenance"]


def _safe_host_config(host: "HostConfig") -> dict[str, Any]:
    """Serialize configured host limits without exposing endpoint credentials or tokens."""
    config = host.model_dump(mode="json")
    endpoint = host.endpoint
    if not endpoint:
        config["endpoint"] = endpoint
        return config
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        hostname = None
        port = None
        parsed = None
    if not hostname or parsed is None or not parsed.scheme:
        config["endpoint"] = "<redacted-invalid-endpoint>"
        return config
    safe_hostname = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{safe_hostname}:{port}" if port is not None else safe_hostname
    config["endpoint"] = urllib.parse.urlunsplit((parsed.scheme, netloc, "", "", ""))
    return config


def _safe_resource_reading(reading: dict[str, Any]) -> dict[str, Any]:
    """Keep only typed capacity measurements in the public effective-policy report."""
    safe: dict[str, Any] = {}
    workers = reading.get("workers")
    if isinstance(workers, int) and not isinstance(workers, bool):
        safe["workers"] = workers
    available = reading.get("available_gib")
    if isinstance(available, (int, float)) and not isinstance(available, bool):
        safe["available_gib"] = available
    observed = reading.get("observed_at")
    if observed is not None:
        try:
            stamp = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            observed = stamp.astimezone(timezone.utc).isoformat()
        except (OverflowError, TypeError, ValueError):
            observed = None
    if isinstance(observed, str):
        safe["observed_at"] = observed
    if isinstance(reading.get("busy"), bool):
        safe["busy"] = reading["busy"]
    return safe


class HostConfig(BaseModel):
    """Static configuration and capabilities of a fleet machine."""

    id: str = Field(description="Logical host identifier: grokbox, notebook, desktop")
    name: str = Field(description="Human-readable machine name")
    priority: int = Field(ge=1, description="Selection priority (1 is highest: Notebook > Grokbox > Desktop)")
    max_slots: int = Field(default=1, ge=1, description="Maximum concurrent demand cycles allowed on this host (default: 1)")
    platform: str = Field(default="windows", description="OS platform: linux or windows")
    tags: list[str] = Field(default_factory=list, description="Descriptive tags (e.g. remote, local, worker)")
    capabilities: list[str] = Field(default_factory=list, description="Declared capabilities/harnesses available on host")
    enabled: bool = True
    endpoint: Optional[str] = None
    max_workers: int = Field(default=1, ge=0)
    min_free_memory_gib: float = Field(default=0, ge=0)


class FleetConfig(BaseModel):
    """Overall fleet configuration and host topology."""

    max_fleet_slots: int = Field(default=3, ge=1, description="Global ceiling of concurrent cycles across all hosts (default: 3)")
    hosts: list[HostConfig] = Field(default_factory=list, description="Ordered host configurations")
    disabled_hosts: list[str] = Field(default_factory=list)

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


def load_fleet_config(path: Optional[Path] = None, *, strict: bool = False) -> FleetConfig:
    """Load FleetConfig from JSON; optionally refuse missing or invalid canonical state."""
    cfg_path = path or DEFAULT_CONFIG_PATH
    if cfg_path.is_file():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            return FleetConfig.model_validate(raw)
        except Exception as exc:
            if strict:
                raise ValueError(f"fleet configuration is unavailable: {cfg_path}") from exc
            logger.warning("Failed to parse fleet config at %s: %s; using canonical defaults", cfg_path, exc)
    elif strict:
        raise FileNotFoundError(f"fleet configuration is unavailable: {cfg_path}")

    return FleetConfig(
        max_fleet_slots=3,
        hosts=[
            HostConfig(id="notebook", name="Notebook", priority=1, max_slots=1, platform="windows", tags=["local", "notebook"]),
            HostConfig(id="grokbox", name="Grokbox", priority=2, max_slots=1, platform="linux", tags=["remote", "grokbox"]),
            HostConfig(id="desktop", name="Desktop", priority=3, max_slots=1, platform="windows", tags=["remote", "desktop", "test_worker"]),
        ],
    )


def _reservation_claim(
    reservation: Optional[dict[str, Any]],
    *,
    now: datetime,
    owner_run_id: Optional[str] = None,
) -> tuple[bool, bool]:
    """Return whether a host lease is still live and whether this run owns it.

    An expired timestamp is not live. A live lease belongs to the caller only when both
    sides carry the same non-empty ``run_id``. A blank owner is not verifiable, so the
    lease stays blocking for everyone, including the process that thinks it holds it.
    """
    active = bool(reservation and reservation.get("active"))
    if active and reservation and reservation.get("expires_at"):
        try:
            expiry = datetime.fromisoformat(str(reservation["expires_at"]).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            active = expiry.astimezone(timezone.utc) > now.astimezone(timezone.utc)
        except (TypeError, ValueError):
            active = True
    owner = str((reservation or {}).get("run_id") or "").strip()
    current = str(owner_run_id or "").strip()
    return active, bool(active and owner and current and owner == current)


def effective_host_policy(
    config: FleetConfig,
    *,
    disabled_from_environment: Iterable[str] = (),
    readings: Optional[dict[str, dict[str, Any]]] = None,
    occupied: Optional[dict[str, str]] = None,
    reservations: Optional[dict[str, dict[str, Any]]] = None,
    now: Optional[datetime] = None,
    max_reading_age: timedelta = timedelta(seconds=90),
    owner_run_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Build the single auditable eligibility decision for every configured host.

    ``owner_run_id`` is the fleet run evaluating the policy. Its own live lease does not
    make that host ineligible. Omitting it, or passing a blank id, keeps every live lease
    blocking, which is what admission of a new ticket must do.
    """
    instant = now or datetime.now(timezone.utc)
    readings, occupied, reservations = readings or {}, occupied or {}, reservations or {}
    configured_exclusions = {host.casefold() for host in config.disabled_hosts}
    environment_exclusions = {host.strip().casefold() for host in disabled_from_environment if host.strip()}
    exclusions = configured_exclusions | environment_exclusions
    decisions: list[dict[str, Any]] = []
    for host in config.sorted_hosts():
        reading = readings.get(host.id) or {}
        reservation = reservations.get(host.id)
        reservation_active, owned_by_current_run = _reservation_claim(
            reservation, now=instant, owner_run_id=owner_run_id,
        )
        excluded = host.id.casefold() in exclusions
        observed = reading.get("observed_at")
        age_seconds: Optional[float] = None
        if observed:
            try:
                stamp = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                age_seconds = max(0.0, (instant - stamp.astimezone(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                age_seconds = None
        measured_workers = reading.get("workers")
        workers = (
            min(measured_workers, host.max_workers)
            if isinstance(measured_workers, int) and not isinstance(measured_workers, bool)
            else None
        )
        reason = "eligible"
        if excluded:
            reason = "excluded"
        elif not host.enabled:
            reason = "disabled_by_config"
        elif host.id in occupied:
            reason = "occupied"
        elif reservation_active and not owned_by_current_run:
            reason = "reservation_active"
        elif age_seconds is None or age_seconds > max_reading_age.total_seconds():
            reason = "resource_reading_missing_or_stale"
        elif not isinstance(workers, int) or workers <= 0:
            reason = "zero_workers"
        elif float(reading.get("available_gib", 0)) < host.min_free_memory_gib:
            reason = "insufficient_memory"
        decisions.append({
            "host_id": host.id,
            "config": _safe_host_config(host),
            "exclusion_sources": (["config.disabled_hosts"] if host.id.casefold() in configured_exclusions else [])
                + (["config.hosts.enabled=false"] if not host.enabled else [])
                + (["DARKFAC_FLEET_DISABLED_HOSTS"] if host.id.casefold() in environment_exclusions else []),
            "resource_reading": _safe_resource_reading(reading),
            "resource_age_seconds": age_seconds,
            "workers_calculated": workers,
            "canonical_reservation": (
                {**reservation, "active": reservation_active, "owned_by_current_run": owned_by_current_run}
                if reservation else None
            ),
            "occupied_by": occupied.get(host.id),
            "eligible": reason == "eligible",
            "reason": reason,
        })
    return decisions


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

    # Sort hosts strictly by priority (1 Notebook > 2 Grokbox > 3 Desktop)
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


# --- live effective policy (USR-471): the one impure entry point, isolated from the pure
# evaluator above ------------------------------------------------------------------------------


def measure_resource_readings(config: FleetConfig) -> dict[str, dict[str, Any]]:
    """Current `{workers, available_gib, observed_at}` reading for every configured host.

    The one host with no persisted `endpoint` is THIS machine (`.factory/config/line_fleet.json`'s
    own convention, shared with `core.line.fleet_dispatcher.default_probe`): measured directly via
    `core.line.gate_ram` (the same formula the official gate uses to size its own workers). Every
    other host is probed over HTTP via `core.harness.remote_dispatch.probe_health`; an offline or
    unreachable host yields no reading at all -- `effective_host_policy` already treats a missing
    reading as `resource_reading_missing_or_stale` (fail-closed), so a dead Grokbox never needs a
    special case here.
    """
    from core.harness import remote_dispatch as rd
    from core.line import gate_ram

    now_iso = datetime.now(timezone.utc).isoformat()
    readings: dict[str, dict[str, Any]] = {}
    excluded = {host.casefold() for host in config.disabled_hosts}
    excluded.update(
        host.strip().casefold()
        for host in os.environ.get(ENV_FLEET_DISABLED_HOSTS, "").split(",")
        if host.strip()
    )
    for host in config.hosts:
        # Exclusion is authoritative: do not even probe a healthy endpoint the owner disabled.
        if not host.enabled or host.id.casefold() in excluded or rd.is_worker_disabled(host.endpoint or host.id, excluded):
            continue
        if not host.endpoint:
            try:
                reading = gate_ram.measure_workers()
            except Exception as exc:  # noqa: BLE001 - one host's measurement must not sink the rest
                logger.warning("local resource measurement failed for host %s: %s", host.id, exc)
                continue
            readings[host.id] = {
                "workers": reading.workers,
                "available_gib": reading.available_gib,
                "observed_at": now_iso,
            }
            continue
        health = rd.probe_health(host.endpoint)
        if health is None:
            continue
        readings[host.id] = {
            "workers": health.get("workers"),
            "available_gib": health.get("available_gib", 0),
                # Missing timestamps are unknown freshness, not a fresh reading.
                # effective_host_policy fails closed when observed_at is absent.
                "observed_at": health.get("observed_at") or health.get("resource_observed_at"),
            "busy": bool(health.get("busy")),
            "node_id": health.get("node_id"),
            "hostname": health.get("hostname"),
        }
    return readings


def build_effective_policy_report(
    config: Optional[FleetConfig] = None,
    *,
    disabled_from_environment: Optional[Iterable[str]] = None,
    readings: Optional[dict[str, dict[str, Any]]] = None,
    now: Optional[datetime] = None,
    config_source: Optional[str | Path] = None,
    owner_run_id: Optional[str] = None,
) -> dict[str, Any]:
    """The single auditable effective fleet policy report: host config, live resource readings,
    exclusions (config + environment) and the resulting eligibility, one entry per host.

    When ``owner_run_id`` is omitted, the process ``DARKFAC_RUN_ID`` is the owner. Pass an
    empty string to evaluate as a caller that cannot prove a lease, which keeps every live
    reservation blocking.
    """
    cfg = config if config is not None else load_fleet_config(strict=True)
    instant = now or datetime.now(timezone.utc)
    resolved_owner = os.environ.get("DARKFAC_RUN_ID", "") if owner_run_id is None else owner_run_id
    env_disabled = (
        list(disabled_from_environment)
        if disabled_from_environment is not None
        else os.environ.get(ENV_FLEET_DISABLED_HOSTS, "").split(",")
    )
    measured = readings if readings is not None else measure_resource_readings(cfg)
    reservations: dict[str, dict[str, Any]] = {}
    reservation_store_available = True
    try:
        from core.line.admission_store import AdmissionStore
        from core.paths import state_root

        admission_path = state_root() / "admission.sqlite3"
        if not admission_path.is_file():
            raise FileNotFoundError("canonical admission store is unavailable")
        for reservation in AdmissionStore(admission_path).active_host_reservations():
            host_id = str(reservation.get("host_id") or "")
            if host_id:
                reservations[host_id] = {**reservation, "active": True}
    except Exception as exc:  # noqa: BLE001 - unavailable reservation state must fail closed below
        logger.warning("could not read active fleet reservations: %s", exc)
        reservation_store_available = False
    occupied = {
        host_id: "remote_worker_busy"
        for host_id, reading in measured.items()
        if bool(reading.get("busy"))
    }
    hosts = effective_host_policy(
        cfg, disabled_from_environment=env_disabled, readings=measured,
        occupied=occupied, reservations=reservations, now=instant,
        owner_run_id=resolved_owner,
    )
    if not reservation_store_available:
        for host in hosts:
            if host["eligible"]:
                host["eligible"] = False
                host["reason"] = "reservation_state_unavailable"
        reservation_state = "unavailable"
    else:
        reservation_state = "available"
    return policy_snapshot_report(
        cfg,
        hosts,
        now=instant,
        config_source=config_source or DEFAULT_CONFIG_PATH,
        reservation_state=reservation_state,
        resource_source="live_health_and_gate_ram",
        disabled_from_environment=env_disabled,
    )


def policy_snapshot_report(
    config: FleetConfig,
    hosts: list[dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    config_source: str | Path,
    reservation_state: str = "available",
    resource_source: str = "dispatcher_probe_results",
    disabled_from_environment: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Wrap effective host decisions in the durable, auditable policy envelope.

    The dispatcher passes the exact decisions it used for admission, so writing the
    operational snapshot does not probe hosts a second time or change active work.
    """
    instant = now or datetime.now(timezone.utc)
    env_exclusions = (
        list(disabled_from_environment)
        if disabled_from_environment is not None
        else os.environ.get(ENV_FLEET_DISABLED_HOSTS, "").split(",")
    )
    configured = {str(host).strip() for host in config.disabled_hosts if str(host).strip()}
    configured.update(host.id for host in config.hosts if not host.enabled)
    configured = sorted(configured)
    environment = sorted({str(host).strip() for host in env_exclusions if str(host).strip()})
    updated_at = instant.isoformat()
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": updated_at,
        "updated_at": updated_at,
        "origin": {
            "configuration": str(config_source),
            "exclusions": {"config": configured, "environment": environment},
            "resource_readings": resource_source,
        },
        "max_fleet_slots": config.max_fleet_slots,
        "reservation_state": reservation_state,
        "hosts": hosts,
    }


def snapshot_path(root: Optional[Path] = None) -> Path:
    """Where the effective-policy snapshot is persisted, under the shared state root."""
    from core.runtime_artifacts import runtime_sidecar_path

    return runtime_sidecar_path(
        "fleet_policy_snapshot.json",
        state_root=root,
    )


def write_effective_policy_snapshot(report: dict[str, Any], *, path: Optional[Path] = None) -> Path:
    """Persist `report` atomically, using a unique same-directory temporary file.

    Same-directory replacement keeps readers from observing a partial JSON document and
    unique temporary names allow the Hub and dispatcher to refresh concurrently.
    """
    from core.runtime_artifacts import atomic_write_json

    target = path if path is not None else snapshot_path()
    return atomic_write_json(target, report)


def effective_policy_snapshot(
    config: Optional[FleetConfig] = None,
    *,
    path: Optional[Path] = None,
    config_source: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Build the effective fleet policy fresh (live resource readings, current exclusions) and
    persist it. The single entry point the CLI (`--effective`) and the Hub endpoint both call --
    neither ever reads the persisted file for a decision; both recompute it here, every time."""
    report = build_effective_policy_report(config, config_source=config_source)
    write_effective_policy_snapshot(report, path=path)
    return report


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="DarkFac fleet policy (USR-471)")
    parser.add_argument(
        "--effective", action="store_true",
        help="Print the current effective fleet host policy as JSON and persist its snapshot",
    )
    parser.add_argument("--config", type=Path, default=None, help="Override line_fleet.json path")
    args = parser.parse_args(argv)
    if not args.effective:
        parser.print_help()
        return 1
    config = load_fleet_config(args.config) if args.config is not None else None
    report = effective_policy_snapshot(config, config_source=args.config or DEFAULT_CONFIG_PATH)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_cli())
