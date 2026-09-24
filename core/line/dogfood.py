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
from core.demands.store import DemandsStore
from core.line.canary import REPORTS_DIR, green_streak, load_reports
from core.line.store_selection import default_control_store
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
    min_green_streak: int = MIN_GREEN_STREAK,
) -> IntakeReceipt | None:
    """Submit at most one dogfood demand, or `None` if the gate is closed.

    `roadmap_path` defaults to the *current* value of the module-level
    `ROADMAP_PATH` (read at call time, not import time), so tests can
    monkeypatch `core.line.dogfood.ROADMAP_PATH` the usual way.
    """
    roadmap_path = roadmap_path if roadmap_path is not None else ROADMAP_PATH
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
