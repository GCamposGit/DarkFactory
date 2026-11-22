"""Deterministic entry point for interactive factory coordination."""

from __future__ import annotations

import argparse
import json
import os
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from core.ops import briefing
from core.paths import project_root, state_root

logger = logging.getLogger(__name__)

HARNESS_NAMES = frozenset({"claude", "codex", "grok", "antigravity"})


class CoordinationReceipt(BaseModel):
    project_id: str
    harness: str
    role: str
    status: str
    in_flight: list[str]
    ticket_id: str | None = None
    first_action: str
    route: dict[str, str | None] | None = None
    route_event_id: str | None = None
    active_run: dict[str, Any] | None = None
    cost_available_usd: float | None = None
    created_at: str


def _project_id(root: Path) -> str:
    explicit = os.environ.get("DARKFAC_PROJECT_ID", "").strip()
    if explicit:
        return explicit
    if (root / "MISSION.md").is_file():
        return "darkfac"
    return root.name


def _candidates() -> list[Any]:
    """Use the canonical feeder eligibility checks; this read path never admits work."""
    from core.demands.store import DemandsStore
    from core.line.backlog_feeder import BacklogFeeder
    from core.line.owner_intake import open_line_store

    demands = DemandsStore(project_root() / ".factory" / "demands" / "demands.json")
    return BacklogFeeder(demands, control_store=open_line_store()).list_candidates()


def _in_flight() -> list[str]:
    try:
        from core.line.fleet_dispatcher import running_fleet_tickets

        return sorted(running_fleet_tickets())
    except Exception:  # noqa: BLE001 - receipt remains useful when fleet telemetry is offline
        return []


def _active_run_details(active: list[str]) -> dict[str, Any] | None:
    """Recover the durable run boundary and evidence before admitting another ticket."""
    try:
        from core.paths import state_root

        journal = state_root() / "runs" / "pending_deliveries.json"
        if journal.is_file():
            rows = json.loads(journal.read_text(encoding="utf-8"))
            pending = [row for row in rows if isinstance(row, dict) and row.get("status") == "awaiting_deploy"] \
                if isinstance(rows, list) else []
            for row in pending:
                if not active or row.get("ticket_id") in active:
                    return {
                        "ticket_id": row["ticket_id"], "run_id": row["run_id"],
                        "stage": row.get("stage", "deploy"),
                        "evidence_refs": list(row.get("evidence_refs") or []),
                    }
        from core.line.live_run import state_directory, load_state

        if not active:
            return None
        ticket_id = sorted(active)[0]
        live_path = state_directory(state_root()) / f"{ticket_id}.json"
        live = load_state(state_directory(state_root()), ticket_id)
        if live:
            return {
                "ticket_id": live.ticket_id, "run_id": live.run_id,
                "stage": live.active_phase or live.last_phase or "in_flight",
                "evidence_refs": [str(live_path)] if live_path.is_file() else [],
            }
    except (OSError, ValueError, TypeError):
        logger.warning("Could not recover the active delivery journal")
    if not active:
        return None
    return {"ticket_id": sorted(active)[0], "run_id": None, "stage": "in_flight", "evidence_refs": []}


def _route(ticket: Any, harness: str, stage: str) -> tuple[dict[str, str | None] | None, float | None, str | None]:
    """Ask the stage router once, passing the ticket so route logs and budget share identity."""
    from core.line.routing import pick
    from core.usage.ledger import ModelUsageLedger

    spent = ModelUsageLedger().spent_usd(ticket.id, "openrouter")
    route = pick(
        stage, [f"harness:{name}" for name in sorted(HARNESS_NAMES - {"antigravity"})],
        ticket_id=ticket.id, project_id=ticket.project_id, spent_usd=spent,
        execution_context="interactive", operating_harness=harness,
    )
    if route is None:
        return None, None, None
    available: float | None = None
    try:
        from core.line.routing import load_routing_config

        available = max(0.0, load_routing_config().run_caps.openrouter_usd - spent)
    except (AttributeError, OSError, ValueError):
        pass
    # `pick` does not issue a durable route-event id. Link to the actual usage event
    # when one exists; otherwise leave the reference absent instead of inventing a UUID.
    try:
        events = ModelUsageLedger()._store.read().get("events", [])
        matching = [event for event in events if event.get("ticket_id") == ticket.id
                    and event.get("project_id") == ticket.project_id]
        event_id = matching[-1].get("invocation_id") if matching else None
    except (AttributeError, OSError, TypeError, ValueError):
        event_id = None
    return {"stage": stage, "harness": route[0], "model": route[1]}, available, event_id


def coordinate(*, harness: str | None = None, entry: str = "short-request") -> CoordinationReceipt:
    """Run the shared short-request or explicit-skill-file coordination entry."""
    from core.line.operating_harness import resolve_operating_harness

    selected_harness = (harness or resolve_operating_harness() or "codex").strip().lower()
    if selected_harness not in HARNESS_NAMES:
        raise ValueError(f"harness invalido: {selected_harness}")
    if entry not in {"short-request", "skill-file"}:
        raise ValueError(f"entrada invalida: {entry}")
    # Both entry modes deliberately share this exact read and recovery path.
    root = project_root()
    candidates = _candidates()
    eligible = [item for item in candidates if item.eligible and item.ticket is not None]
    active = _in_flight()
    active_run = _active_run_details(active)
    if active_run and active_run.get("ticket_id") not in active:
        active.append(str(active_run["ticket_id"]))
    if active or active_run:
        status = "in_flight"
        first = (f"Retomar o run {active_run['run_id']} do ticket {active_run['ticket_id']} "
                 f"na etapa {active_run['stage']}; preservar as evidencias existentes." if active_run and active_run.get("run_id")
                 else "Aguardar a retomada do trabalho em voo antes de admitir outro ticket.")
        ticket = None
        route = None
        cost = None
        route_event_id = None
    elif not eligible:
        status = "idle"
        first = "Fila sem trabalho elegível; aguardar evento do despachante."
        ticket = None
        route = None
        cost = None
        route_event_id = None
    else:
        # The feeder preserves creation order for admission. Interactive coordination must
        # honor the dispatcher's explicit priority among the already eligible candidates.
        eligible.sort(key=lambda item: (
            -int(getattr(item.ticket, "priority", 0)),
            getattr(item.ticket, "created_at", datetime.min.replace(tzinfo=timezone.utc)),
            item.ticket.id,
        ))
        candidate = eligible[0]
        ticket = candidate.ticket
        status = "ready"
        ticket_status = getattr(ticket, "status", "implementing")
        status_name = str(getattr(ticket_status, "value", ticket_status)).lower()
        stage = {
            "planned": "planning", "implementing": "development",
            "validating": "review", "reviewing": "review",
        }.get(status_name, "development")
        route, cost, route_event_id = _route(ticket, selected_harness, stage)
        first = (f"Delegar {ticket.id} na etapa {stage} pelo roteador." if route
                 else f"Aguardar rota para {ticket.id} no despachante.")
        if route is None:
            status = "wait"
    receipt = CoordinationReceipt(
        project_id=(
            ticket.project_id if ticket else
            next((item.ticket.project_id for item in eligible
                  if getattr(item.ticket, "project_id", None)), _project_id(root))
        ),
        harness=selected_harness,
        role="coordinator", status=status, in_flight=active, ticket_id=ticket.id if ticket else None,
        first_action=first, route=route, route_event_id=route_event_id,
        active_run=active_run, cost_available_usd=cost, created_at=datetime.now(timezone.utc).isoformat(),
    )
    target = state_root() / "coordination" / "last_receipt.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
    briefing.render_briefing()
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inicia a coordenacao autonoma da DarkFac")
    parser.add_argument("--harness", choices=sorted(HARNESS_NAMES))
    parser.add_argument("--entry", choices=("short-request", "skill-file"), default="short-request")
    args = parser.parse_args(argv)
    receipt = coordinate(harness=args.harness, entry=args.entry)
    sys.stdout.write(receipt.model_dump_json(indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
