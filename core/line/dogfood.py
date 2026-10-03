"""HF-27-10 -- Dogfood submitter: the factory builds its own backlog.

Once `core.line.canary.green_streak` reaches the configured threshold, the
factory feeds its approved demands and roadmap through the line. Tickets in
`.factory/demands/demands.json` take precedence over roadmap items, and both
sources require the `line-ok` tag. Only one dogfood run is submitted at a time.

Gating (all three must hold):
- `green_streak(reports) >= min_green_streak()` (code default 7, overridable
  with env `DARKFAC_DOGFOOD_MIN_STREAK`; invalid values fall back to 7).
- No `dogfood:*` demand already has an active run (one-at-a-time).
- The candidate item does not touch a `guard.py`-protected governance path
  (reuses `core.orchestrator.guard.PROTECTED_PATTERNS` -- never a second,
  drifting copy of that list).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.demands.autonomous_intake import AutonomousIntakeService
from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.line.canary import REPORTS_DIR, green_streak, load_reports
from core.line.owner_intake import (
    LineSubmission,
    _ticket_attempts,
    max_ticket_attempts_from_env,
    run_state,
    submit_ticket_to_line,
)
from core.line.store_selection import default_control_store
from core.orchestrator.guard import PROTECTED_PATTERNS
from core.roadmap.models import DeliveryStatus, PlanningHorizon, RoadmapItem
from core.workflow.control_contracts import IdempotencyConflict, IntakeCommand, IntakeReceipt
from core.workflow.control_store import ControlStore

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOGFOOD_PROJECT_ID = "darkfac"
DOGFOOD_CHANNEL = "dogfood"
ROADMAP_PATH = PROJECT_ROOT / ".factory" / "roadmap" / "darkfac.json"
LINE_OK_TAG = "line-ok"
MIN_GREEN_STREAK = 7
MIN_STREAK_ENV = "DARKFAC_DOGFOOD_MIN_STREAK"


def min_green_streak_from_env() -> int:
    """`DARKFAC_DOGFOOD_MIN_STREAK` as a positive int; unset/invalid/<1 -> `MIN_GREEN_STREAK`."""
    raw = os.environ.get(MIN_STREAK_ENV, "").strip()
    if not raw:
        return MIN_GREEN_STREAK
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using %d", MIN_STREAK_ENV, raw, MIN_GREEN_STREAK)
        return MIN_GREEN_STREAK
    if value < 1:
        logger.warning("%s=%r is < 1; using %d", MIN_STREAK_ENV, raw, MIN_GREEN_STREAK)
        return MIN_GREEN_STREAK
    return value


# --------------------------------------------------------------------------
# Roadmap selection (domain, no I/O beyond the one JSON read)
# --------------------------------------------------------------------------


def load_roadmap_items(roadmap_path: Path = ROADMAP_PATH) -> list[RoadmapItem]:
    if not roadmap_path.exists():
        return []
    data = json.loads(roadmap_path.read_text(encoding="utf-8"))
    items: list[RoadmapItem] = []
    for raw in data.get("items", []):
        try:
            items.append(RoadmapItem.model_validate(raw))
        except Exception as exc:  # pragma: no cover - defensive against drifted schema
            logger.warning("Skipping malformed roadmap item %s: %s", raw.get("id"), exc)
    return items


def _text_touches_protected_path(haystacks: list[str]) -> bool:
    for text in haystacks:
        if not text:
            continue
        for raw_token in text.replace(",", " ").split():
            token = raw_token.strip("`'\"()[]{}:;")
            if not token:
                continue
            for pattern in PROTECTED_PATTERNS:
                if fnmatch.fnmatch(token, pattern) or fnmatch.fnmatch(Path(token).name, pattern):
                    return True
    return False


def touches_protected_path(item: RoadmapItem) -> bool:
    """True when the item's own text mentions a `guard.py`-protected path.

    `RoadmapItem` has no dedicated "touched files" field, so this is a
    best-effort scan of `description`, `completion_criteria` and
    `source_refs[].locator` for a path-shaped token matching one of
    `core.orchestrator.guard.PROTECTED_PATTERNS`.
    """
    haystacks: list[str] = [item.description, *item.completion_criteria]
    haystacks += [ref.locator for ref in item.source_refs if ref.locator]
    return _text_touches_protected_path(haystacks)


def ticket_touches_protected_path(ticket: UserTicket) -> bool:
    """Reject tickets that name a protected path, including suggested files."""
    return _text_touches_protected_path([
        ticket.problem_statement,
        ticket.reachability_contract,
        *ticket.core_journey,
        *ticket.acceptance_criteria,
        *ticket.suggested_files,
    ])


_HORIZON_ORDER = {
    PlanningHorizon.NOW: 0,
    PlanningHorizon.NEXT: 1,
    PlanningHorizon.LATER: 2,
    PlanningHorizon.EXPLORATORY: 3,
    PlanningHorizon.UNSCHEDULED: 4,
}


def eligible_ticket_candidates(tickets: list[UserTicket]) -> list[UserTicket]:
    """Prioritize approved internal tickets by horizon, creation time and ID."""
    completed = {ticket.id for ticket in tickets if ticket.status == DeliveryStatus.COMPLETED}
    candidates = [
        ticket for ticket in tickets
        if ticket.project_id == DOGFOOD_PROJECT_ID
        and ticket.status in {DeliveryStatus.PLANNED, DeliveryStatus.IMPLEMENTING}
        and LINE_OK_TAG in ticket.tags
        and all(dependency in completed for dependency in ticket.dependencies)
        and not ticket_touches_protected_path(ticket)
    ]
    return sorted(candidates, key=lambda ticket: (
        _HORIZON_ORDER[ticket.horizon], ticket.created_at, ticket.id,
    ))


def pick_candidate(items: list[RoadmapItem], *, exclude_ids: frozenset[str] = frozenset()) -> RoadmapItem | None:
    """First `planned` item tagged `line-ok` that does not touch governance,
    in roadmap order (stable, so repeated calls with the same input agree)."""
    for item in items:
        if item.id in exclude_ids:
            continue
        if item.delivery_status != DeliveryStatus.PLANNED:
            continue
        if LINE_OK_TAG not in item.tags:
            continue
        if touches_protected_path(item):
            continue
        return item
    return None


# --------------------------------------------------------------------------
# One-at-a-time in-flight check
# --------------------------------------------------------------------------


_IN_FLIGHT_QUERY_SQLITE = """
    SELECT r.status FROM intake_commands ic
    JOIN runs r ON r.run_id = ic.run_id
    WHERE ic.channel = ? AND ic.external_id LIKE 'dogfood:%'
"""

_IN_FLIGHT_QUERY_POSTGRES = """
    SELECT r.status FROM intake_commands ic
    JOIN runs r ON r.run_id = ic.run_id
    WHERE ic.channel = %s AND ic.external_id LIKE 'dogfood:%%'
"""


def _sqlite_connect(store: Any) -> Any | None:
    """A raw sqlite3 connection for `store`, if it (or its mock-mode
    backend) is SQLite-backed -- `SQLiteControlStore` directly, or a
    mock-mode `PostgresControlStore` (whose `_backend` *is* one)."""
    connect = getattr(store, "_connect", None)
    if connect is not None:
        return connect()
    backend = getattr(store, "_backend", None)
    backend_connect = getattr(backend, "_connect", None) if backend is not None else None
    if backend_connect is not None:
        return backend_connect()
    return None


def has_in_flight_dogfood(store: ControlStore) -> bool:
    """True when a `dogfood:*` intake already has an `active` run.

    Works against `SQLiteControlStore`, a mock-mode `PostgresControlStore`
    (HF-27-10 review item 2 -- the real cloud line runs on Postgres), and a
    real (non-mock) `PostgresControlStore` via its own `psycopg` connection.
    A store none of these apply to is treated as "unknown, so don't submit"
    -- fail-closed rather than risking a second concurrent dogfood run.
    """
    conn = _sqlite_connect(store)
    if conn is not None:
        try:
            cur = conn.cursor()
            cur.execute(_IN_FLIGHT_QUERY_SQLITE, (DOGFOOD_CHANNEL,))
            return any(row[0] == "active" for row in cur.fetchall())
        finally:
            conn.close()

    psycopg = getattr(store, "_psycopg", None)
    raw_url = getattr(store, "raw_url", None)
    if psycopg is not None and raw_url:
        try:
            with psycopg.connect(raw_url) as pg_conn:
                with pg_conn.cursor() as cur:
                    cur.execute(_IN_FLIGHT_QUERY_POSTGRES, (DOGFOOD_CHANNEL,))
                    return any(row[0] == "active" for row in cur.fetchall())
        except Exception as exc:
            logger.warning("Postgres in-flight dogfood query failed (%s); refusing to submit", exc)
            return True

    logger.warning("Store %r cannot be queried for in-flight dogfood runs; refusing to submit", type(store))
    return True


def has_in_flight_line_ticket(store: ControlStore, tickets: list[UserTicket]) -> bool:
    """Count owner-channel runs for line-ok tickets in the same one-at-a-time gate."""
    if not callable(getattr(store, "find_intake_runs", None)):
        logger.warning("Store cannot list owner-channel runs; refusing dogfood submission")
        return True
    try:
        for ticket in tickets:
            if ticket.project_id != DOGFOOD_PROJECT_ID or LINE_OK_TAG not in ticket.tags:
                continue
            attempts = _ticket_attempts(store, ticket.id)
            if attempts and run_state(store, attempts[-1][1]) == "in_flight":
                return True
    except Exception as exc:
        logger.warning("Could not check in-flight line tickets; refusing dogfood submission: %s", exc)
        return True
    return False


# --------------------------------------------------------------------------
# Submission
# --------------------------------------------------------------------------


def _build_intake_command(item: RoadmapItem) -> IntakeCommand:
    return IntakeCommand(
        project_id=DOGFOOD_PROJECT_ID,
        channel=DOGFOOD_CHANNEL,
        external_id=f"dogfood:{item.id}",
        mode="autonomous",
        policy_ref="darkfac://line/dogfood/v1",
        payload={
            "title": item.title,
            "problem": item.description or item.title,
            "journey": "; ".join(item.completion_criteria) or item.title,
            "non_goals": [],
            "criteria": list(item.completion_criteria),
            "roadmap_item_id": item.id,
        },
    )


def submit_dogfood_item(
    *,
    store: ControlStore,
    demands_store: DemandsStore | None = None,
    roadmap_path: Path | None = None,
    reports_dir: Path = REPORTS_DIR,
    now: datetime | None = None,
    min_green_streak: int | None = None,
) -> IntakeReceipt | LineSubmission | None:
    """Submit at most one dogfood demand, or `None` if the gate is closed.

    `roadmap_path` defaults to the *current* value of the module-level
    `ROADMAP_PATH` (read at call time, not import time), so tests can
    monkeypatch `core.line.dogfood.ROADMAP_PATH` the usual way.
    """
    roadmap_path = roadmap_path if roadmap_path is not None else ROADMAP_PATH
    min_green_streak = min_green_streak if min_green_streak is not None else min_green_streak_from_env()
    streak = green_streak(load_reports(reports_dir, limit=min_green_streak))
    if streak < min_green_streak:
        logger.info("Dogfood gate closed: green streak %d < %d", streak, min_green_streak)
        return None

    if has_in_flight_dogfood(store):
        logger.info("Dogfood gate closed: a dogfood demand is already in flight")
        return None

    demands_store = demands_store or DemandsStore()
    tickets = demands_store.list_tickets(project_id=DOGFOOD_PROJECT_ID)
    if has_in_flight_line_ticket(store, tickets):
        logger.info("Dogfood gate closed: a line-ok ticket is already in flight")
        return None

    effective_now = now or datetime.now(UTC)
    for ticket in eligible_ticket_candidates(tickets):
        attempts = _ticket_attempts(store, ticket.id)
        if ticket.status == DeliveryStatus.IMPLEMENTING and not attempts:
            # An agent or owner may be implementing this ticket outside dogfood.
            continue
        if attempts:
            state = run_state(store, attempts[-1][1])
            if state == "succeeded" or len(attempts) >= max_ticket_attempts_from_env():
                continue
        submission = submit_ticket_to_line(
            ticket.id, demands_store=demands_store, store=store, now=effective_now,
        )
        if submission.ok and submission.run_id:
            logger.info("Dogfood: submitted ticket %s (run_id=%s)", ticket.id, submission.run_id)
            return submission
        logger.warning("Dogfood: ticket %s could not be submitted: %s", ticket.id, submission.message)
        return None

    candidate = pick_candidate(
        load_roadmap_items(roadmap_path), exclude_ids=frozenset(ticket.id for ticket in tickets),
    )
    if candidate is None:
        logger.info("Dogfood: no eligible planned/line-ok ticket or roadmap item found")
        return None

    command = _build_intake_command(candidate)
    service = AutonomousIntakeService(store=store, demands_store=demands_store)
    try:
        receipt = service.accept(command, effective_now)
    except IdempotencyConflict as exc:  # pragma: no cover - defensive, item.id is unique
        logger.error("Dogfood intake conflict for %s: %s", command.external_id, exc)
        return None
    logger.info("Dogfood: submitted %s (run_id=%s)", command.external_id, receipt.run_id)
    return receipt


# --------------------------------------------------------------------------
# CLI (python -m core.line.dogfood run) -- HF-27-10 review item 4b
# --------------------------------------------------------------------------
#
# `core.line.canary.run_daily` already calls `submit_dogfood_item` in-process
# at the end of a `passed` run when `DARKFAC_DOGFOOD_ENABLED` is set. This
# CLI is the alternative standalone entry point for a *separate* cron
# schedule (e.g. once a day, right after the canary's own cron, independent
# of whether that particular canary run happened to pass) -- either wiring
# is env-gated off by default, so plain `canary run` never starts feeding
# the backlog on its own.


def _dogfood_enabled_from_env() -> bool:
    return os.environ.get("DARKFAC_DOGFOOD_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _cli_run(args: argparse.Namespace) -> int:
    if not (args.force or _dogfood_enabled_from_env()):
        print(json.dumps({"submitted": False, "reason": "DARKFAC_DOGFOOD_ENABLED is not set"}, indent=2))
        return 0
    store = default_control_store()
    receipt = submit_dogfood_item(store=store)
    if receipt is None:
        print(json.dumps({"submitted": False, "reason": "gate closed or no eligible item"}, indent=2))
        return 0
    print(json.dumps({"submitted": True, "receipt": receipt.model_dump(mode="json")}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="DarkFac dogfood submitter (HF-27-10)")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Submit at most one eligible dogfood demand, if the gate is open")
    run_p.add_argument(
        "--force",
        action="store_true",
        help="Run even if DARKFAC_DOGFOOD_ENABLED is unset (still subject to the streak/in-flight/guard gates)",
    )

    args = parser.parse_args(argv)
    if args.command == "run":
        return _cli_run(args)
    return 1  # pragma: no cover - argparse enforces `required=True`


if __name__ == "__main__":
    sys.exit(main())
