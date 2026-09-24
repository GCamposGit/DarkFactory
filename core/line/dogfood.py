"""HF-27-10 -- Dogfood submitter: the factory builds its own backlog.

Once `core.line.canary.green_streak` reaches 7 consecutive green days, the
factory starts feeding its own roadmap back through the line: a single
`planned` item from `.factory/roadmap/darkfac.json` tagged `line-ok` is
submitted through the same public intake (`AutonomousIntakeService`) used
by every other demand, as project `darkfac`, one item at a time.

Gating (all three must hold):
- `green_streak(reports) >= MIN_GREEN_STREAK` (default 7).
- No `dogfood:*` demand already has an active run (one-at-a-time).
- The candidate item does not touch a `guard.py`-protected governance path
  (reuses `core.orchestrator.guard.PROTECTED_PATTERNS` -- never a second,
  drifting copy of that list).
"""

from __future__ import annotations

import fnmatch
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.demands.autonomous_intake import AutonomousIntakeService
from core.demands.store import DemandsStore
from core.line.canary import REPORTS_DIR, green_streak, load_reports
from core.orchestrator.guard import PROTECTED_PATTERNS
from core.roadmap.models import DeliveryStatus, RoadmapItem
from core.workflow.control_contracts import IdempotencyConflict, IntakeCommand, IntakeReceipt
from core.workflow.control_store import ControlStore

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOGFOOD_PROJECT_ID = "darkfac"
DOGFOOD_CHANNEL = "dogfood"
ROADMAP_PATH = PROJECT_ROOT / ".factory" / "roadmap" / "darkfac.json"
LINE_OK_TAG = "line-ok"
MIN_GREEN_STREAK = 7


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


def touches_protected_path(item: RoadmapItem) -> bool:
    """True when the item's own text mentions a `guard.py`-protected path.

    `RoadmapItem` has no dedicated "touched files" field, so this is a
    best-effort scan of `description`, `completion_criteria` and
    `source_refs[].locator` for a path-shaped token matching one of
    `core.orchestrator.guard.PROTECTED_PATTERNS`.
    """
    haystacks: list[str] = [item.description, *item.completion_criteria]
    haystacks += [ref.locator for ref in item.source_refs if ref.locator]
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


def pick_candidate(items: list[RoadmapItem]) -> RoadmapItem | None:
    """First `planned` item tagged `line-ok` that does not touch governance,
    in roadmap order (stable, so repeated calls with the same input agree)."""
    for item in items:
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


def has_in_flight_dogfood(store: ControlStore) -> bool:
    """True when a `dogfood:*` intake already has an `active` run.

    Uses the store's own SQLite connection (available on
    `SQLiteControlStore` and the mock-mode `PostgresControlStore` adapter)
    to join `intake_commands` (channel, external_id) with `runs.status`.
    A store that exposes neither is treated as "unknown, so don't submit"
    -- fail-closed rather than risking a second concurrent dogfood run.
    """
    connect = getattr(store, "_connect", None)
    if connect is None:
        logger.warning("Store %r cannot be queried for in-flight dogfood runs; refusing to submit", type(store))
        return True
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT r.status FROM intake_commands ic
            JOIN runs r ON r.run_id = ic.run_id
            WHERE ic.channel = ? AND ic.external_id LIKE 'dogfood:%'
            """,
            (DOGFOOD_CHANNEL,),
        )
        return any(row[0] == "active" for row in cur.fetchall())
    finally:
        conn.close()


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
    roadmap_path: Path = ROADMAP_PATH,
    reports_dir: Path = REPORTS_DIR,
    now: datetime | None = None,
    min_green_streak: int = MIN_GREEN_STREAK,
) -> IntakeReceipt | None:
    """Submit at most one dogfood demand, or `None` if the gate is closed."""
    streak = green_streak(load_reports(reports_dir, limit=min_green_streak))
    if streak < min_green_streak:
        logger.info("Dogfood gate closed: green streak %d < %d", streak, min_green_streak)
        return None

    if has_in_flight_dogfood(store):
        logger.info("Dogfood gate closed: a dogfood demand is already in flight")
        return None

    candidate = pick_candidate(load_roadmap_items(roadmap_path))
    if candidate is None:
        logger.info("Dogfood: no eligible planned/line-ok roadmap item found")
        return None

    command = _build_intake_command(candidate)
    service = AutonomousIntakeService(store=store, demands_store=demands_store)
    effective_now = now or datetime.now(UTC)
    try:
        receipt = service.accept(command, effective_now)
    except IdempotencyConflict as exc:  # pragma: no cover - defensive, item.id is unique
        logger.error("Dogfood intake conflict for %s: %s", command.external_id, exc)
        return None
    logger.info("Dogfood: submitted %s (run_id=%s)", command.external_id, receipt.run_id)
    return receipt
