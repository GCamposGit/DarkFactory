"""Tests for USR-283: Continuous backlog feeder across all registered and eligible projects.

Validates:
1. Multi-project eligibility: open tickets from two distinct registered projects are both eligible
   and admitted without depending on the default 'darkfac'-only allowlist.
2. Unregistered projects are blocked with 'unregistered_project'.
3. Gate G1 ambiguity: tickets failing G1 clarity criteria are blocked with 'g1_ambiguity'.
4. Dependency resolution: unmet dependencies block with 'unmet_dependencies', and state change
   (dependency completed) unblocks them dynamically.
5. V0 re-evaluation vs intentional postponement: tickets postponed solely by V0 ('congelado-v0')
   are re-evaluated as eligible, while intentional business postponements remain blocked.
6. Replay & idempotency: in-flight runs are replayed without duplicating intake.
7. Store unavailability: fail-closed with explicit error.
8. Deterministic ordering: sorted by (created_at, project_id, id).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.demands.models import DemandOrigin, UserTicket
from core.demands.store import DemandsStore
from core.line import owner_intake
from core.line.backlog_feeder import BacklogFeeder, BacklogFeederReport, TicketEligibility
from core.projects.models import ProjectDescriptor, ProjectKind
from core.projects.registry import ProjectRegistry
from core.roadmap.models import DeliveryStatus, PlanningHorizon
from core.workflow.control_contracts import StoreUnavailableError
from core.workflow.control_store import SQLiteControlStore


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*owner_intake.DATABASE_URL_ENVS, owner_intake.AUTOSUBMIT_ENV, owner_intake.PROJECTS_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def store(tmp_path: Path) -> SQLiteControlStore:
    return SQLiteControlStore(db_path=tmp_path / "control.db")


@pytest.fixture
def demands(tmp_path: Path) -> DemandsStore:
    return DemandsStore(path=tmp_path / "demands.json")


@pytest.fixture
def registry(tmp_path: Path) -> ProjectRegistry:
    reg_file = tmp_path / "projects.json"
    reg = ProjectRegistry(projects_file=reg_file)
    reg.register_project(
        ProjectDescriptor(
            id="proj-b",
            name="Project B (Client)",
            path=str(tmp_path / "proj_b"),
            kind=ProjectKind.CLIENT_PORTFOLIO,
            prefix="PRB",
        )
    )
    return reg


def _make_ticket(
    demands: DemandsStore,
    ticket_id: str,
    project_id: str = "darkfac",
    *,
    title: str = "Feature title",
    problem: str = "Detailed problem description with enough characters to satisfy G1 minimums",
    journey: list[str] | None = None,
    non_goals: list[str] | None = None,
    criteria: list[str] | None = None,
    status: DeliveryStatus = DeliveryStatus.PLANNED,
    horizon: PlanningHorizon = PlanningHorizon.NOW,
    tags: list[str] | None = None,
    dependencies: list[str] | None = None,
    created_at: datetime | None = None,
) -> UserTicket:
    ticket = UserTicket(
        id=ticket_id,
        project_id=project_id,
        title=title,
        problem_statement=problem,
        core_journey=journey or ["Step 1: user opens dashboard", "Step 2: user clicks button"],
        non_goals=non_goals or ["Do not break existing API"],
        acceptance_criteria=criteria or ["Feature must return 200 OK with valid JSON"],
        status=status,
        horizon=horizon,
        tags=tags or ["user-demand"],
        dependencies=dependencies or [],
        created_at=created_at or datetime.now(UTC),
    )
    return demands.save_ticket(ticket)


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


def test_feeder_two_registered_projects_are_eligible(store, demands, registry) -> None:
    """Open tickets from darkfac and proj-b are both admitted without default allowlist blockage."""
    t_df = _make_ticket(demands, "USR-101", "darkfac", title="DarkFac core demand")
    t_pb = _make_ticket(demands, "PRB-01", "proj-b", title="Project B client demand")

    feeder = BacklogFeeder(
        demands_store=demands,
        control_store=store,
        project_registry=registry,
        allow_registered_projects=True,
    )

    candidates = feeder.list_candidates()
    assert len(candidates) == 2
    assert all(c.eligible for c in candidates)

    report = feeder.reconcile_and_feed()
    assert report.evaluated_count == 2
    assert report.eligible_count == 2
    assert report.submitted_count == 2
    assert len(report.submissions) == 2
    assert report.submissions[0].ok is True
    assert report.submissions[1].ok is True
    assert {s.project_id for s in report.submissions} == {"darkfac", "proj-b"}


def test_feeder_unregistered_project_is_blocked(store, demands, registry) -> None:
    """Ticket from unregistered project is refused and marked as unregistered_project."""
    _make_ticket(demands, "UNREG-1", "phantom-project", title="Unregistered project demand")
    _make_ticket(demands, "USR-102", "darkfac", title="DarkFac valid demand")

    feeder = BacklogFeeder(
        demands_store=demands,
        control_store=store,
        project_registry=registry,
        allow_registered_projects=True,
    )

    candidates = feeder.list_candidates()
    assert len(candidates) == 2

    by_id = {c.ticket_id: c for c in candidates}
    assert by_id["UNREG-1"].eligible is False
    assert by_id["UNREG-1"].reason == "unregistered_project"
    assert "não está registrado" in by_id["UNREG-1"].message

    assert by_id["USR-102"].eligible is True

    # Feeder feeds only the eligible ticket
    report = feeder.reconcile_and_feed()
    assert report.submitted_count == 1
    assert report.submissions[0].ticket_id == "USR-102"


def test_feeder_g1_ambiguity_blocks_intake(store, demands, registry) -> None:
    """Ticket failing G1 clarity criteria (e.g. empty non-goals) is blocked."""
    _make_ticket(
        demands,
        "USR-AMB",
        "darkfac",
        problem="Too short",  # less than 20 chars
        non_goals=[],        # empty non-goals
        criteria=[],         # empty criteria
    )

    feeder = BacklogFeeder(
        demands_store=demands,
        control_store=store,
        project_registry=registry,
    )

    candidates = feeder.list_candidates()
    assert len(candidates) == 1
    c = candidates[0]
    assert c.eligible is False
    assert c.reason == "g1_ambiguity"
    assert "Gate G1" in c.message

    # Feed does nothing
    sub = feeder.feed_next()
    assert sub is None


def test_feeder_dependencies_and_state_change(store, demands, registry) -> None:
    """Unmet dependencies block the ticket; completing dependency unblocks dynamically (mudanca de estado)."""
    dep = _make_ticket(demands, "DEP-1", "darkfac", status=DeliveryStatus.PLANNED)
    t = _make_ticket(demands, "USR-DEP", "darkfac", dependencies=["DEP-1"])

    feeder = BacklogFeeder(
        demands_store=demands,
        control_store=store,
        project_registry=registry,
    )

    # Initially blocked because DEP-1 is not completed
    c = feeder.evaluate_ticket(t)
    assert c.eligible is False
    assert c.reason == "unmet_dependencies"
    assert "DEP-1" in c.message

    # State change: DEP-1 is completed
    demands.update_status("DEP-1", DeliveryStatus.COMPLETED, delivery_evidence="commit:abc1234")

    # Now USR-DEP becomes eligible
    c_after = feeder.evaluate_ticket(t)
    assert c_after.eligible is True
    assert c_after.reason is None


def test_feeder_v0_freeze_vs_intentional_postponement(store, demands, registry) -> None:
    """Tickets postponed exclusively by V0 ('congelado-v0') are re-evaluated as eligible,

    while intentional business postponements remain blocked.
    """
    # Ticket postponed solely by historic V0 freeze: should be re-evaluated as eligible
    t_v0 = _make_ticket(
        demands,
        "USR-V0",
        "darkfac",
        tags=["user-demand", "congelado-v0"],
        horizon=PlanningHorizon.LATER,
    )

    # Ticket with intentional business hold: should remain blocked
    t_hold = _make_ticket(
        demands,
        "USR-HOLD",
        "darkfac",
        tags=["user-demand", "postponed-business"],
    )

    # Ticket with future planning horizon without V0 tag: intentional postponement
    t_future = _make_ticket(
        demands,
        "USR-FUTURE",
        "darkfac",
        tags=["user-demand"],
        horizon=PlanningHorizon.LATER,
    )

    feeder = BacklogFeeder(
        demands_store=demands,
        control_store=store,
        project_registry=registry,
    )

    e_v0 = feeder.evaluate_ticket(t_v0)
    assert e_v0.eligible is True, "V0-exclusive freeze must be re-evaluated as eligible post-V0"

    e_hold = feeder.evaluate_ticket(t_hold)
    assert e_hold.eligible is False
    assert e_hold.reason == "intentional_postponement"

    e_future = feeder.evaluate_ticket(t_future)
    assert e_future.eligible is False
    assert e_future.reason == "intentional_postponement"


def test_feeder_replay_and_idempotency_without_duplicate_intake(store, demands, registry) -> None:
    """Submitting an in-flight ticket replays the existing run and does not duplicate intake."""
    t = _make_ticket(demands, "USR-REP", "darkfac")

    feeder = BacklogFeeder(
        demands_store=demands,
        control_store=store,
        project_registry=registry,
    )

    # First feed: creates line run
    sub1 = feeder.feed_next()
    assert sub1 is not None and sub1.ok is True and sub1.replayed is False
    run_id = sub1.run_id
    assert run_id is not None

    # Ticket is now in_flight: evaluate_ticket reports in_flight
    c = feeder.evaluate_ticket(t)
    assert c.eligible is False
    assert c.reason == "in_flight"
    assert c.run_id == run_id

    # Second feed_next finds nothing new to admit
    sub2 = feeder.feed_next()
    assert sub2 is None


def test_feeder_waiting_human_status_is_blocked(store, demands, registry) -> None:
    """A ticket in WAITING_HUMAN status is parked and not admitted to the line."""
    _make_ticket(demands, "USR-WH", "darkfac", status=DeliveryStatus.WAITING_HUMAN)

    feeder = BacklogFeeder(
        demands_store=demands,
        control_store=store,
        project_registry=registry,
    )

    candidates = feeder.list_candidates()
    assert len(candidates) == 1
    assert candidates[0].eligible is False
    assert candidates[0].reason == "waiting_human"


def test_feeder_store_unavailability_fails_closed(demands, registry) -> None:
    """Store unavailability raises StoreUnavailableError (fail-closed)."""
    _make_ticket(demands, "USR-FAIL", "darkfac")

    class BrokenStore:
        def find_intake_runs(self, channel, external_id):
            raise StoreUnavailableError("Postgres unreachable")

        def accept(self, command, now):
            raise StoreUnavailableError("Postgres unreachable")

    feeder = BacklogFeeder(
        demands_store=demands,
        control_store=BrokenStore(),
        project_registry=registry,
    )

    with pytest.raises(StoreUnavailableError):
        feeder.list_candidates()


def test_feeder_deterministic_order(store, demands, registry) -> None:
    """Tickets are evaluated and fed strictly by created_at ASC with stable tie-break."""
    now = datetime.now(UTC) - timedelta(hours=1)
    t3 = _make_ticket(demands, "USR-C", "darkfac", created_at=now + timedelta(minutes=20))
    t1 = _make_ticket(demands, "PRB-A", "proj-b", created_at=now)
    t2 = _make_ticket(demands, "USR-B", "darkfac", created_at=now + timedelta(minutes=10))

    feeder = BacklogFeeder(
        demands_store=demands,
        control_store=store,
        project_registry=registry,
        allow_registered_projects=True,
    )

    eligible = feeder.list_eligible_tickets()
    assert [t.id for t in eligible] == ["PRB-A", "USR-B", "USR-C"]

    # First feed feeds the oldest
    sub = feeder.feed_next()
    assert sub is not None and sub.ticket_id == "PRB-A"
