"""Deterministic entry point for interactive factory coordination."""

from __future__ import annotations

import argparse
import os
import sys
import logging
import uuid
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


def _route(ticket: Any, harness: str) -> tuple[dict[str, str | None] | None, float | None, str | None]:
    """Ask the stage router once, passing the ticket so route logs and budget share identity."""
    from core.line.routing import pick
    from core.usage.ledger import ModelUsageLedger

    spent = ModelUsageLedger().spent_usd(ticket.id, "openrouter")
    route = pick(
        "development", [f"harness:{name}" for name in sorted(HARNESS_NAMES - {"antigravity"})],
        ticket_id=ticket.id, project_id=ticket.project_id, spent_usd=spent,
        execution_context="interactive", operating_harness=harness,
    )
    event_id = str(uuid.uuid4())
    if route is None:
        return None, None, event_id
    available: float | None = None
    try:
        from core.line.routing import load_routing_config

        available = max(0.0, load_routing_config().run_caps.openrouter_usd - spent)
    except (AttributeError, OSError, ValueError):
        pass
    logger.info(
        "coordination_route_decision event_id=%s project_id=%s ticket_id=%s stage=development harness=%s cost_available_usd=%s",
        event_id, ticket.project_id, ticket.id, route[0], available,
        extra={"route_event": {"event_id": event_id, "project_id": ticket.project_id,
                                "ticket_id": ticket.id, "stage": "development",
                                "harness": route[0], "cost_available_usd": available}},
    )
    return {"stage": "development", "harness": route[0], "model": route[1]}, available, event_id


def coordinate(*, harness: str | None = None, entry: str = "short-request") -> CoordinationReceipt:
    """Run the shared short-request or explicit-skill-file coordination entry."""
    from core.line.operating_harness import resolve_operating_harness

    selected_harness = (harness or resolve_operating_harness() or "codex").strip().lower()
    if selected_harness not in HARNESS_NAMES:
        raise ValueError(f"harness invalido: {selected_harness}")
    if entry not in {"short-request", "skill-file"}:
        raise ValueError(f"entrada invalida: {entry}")
    # Both entry modes deliberately share this exact read and recovery path.
    briefing.render_briefing()
    root = project_root()
    candidates = _candidates()
    eligible = [item for item in candidates if item.eligible and item.ticket is not None]
    active = _in_flight()
    if not eligible:
        status = "in_flight" if active else "idle"
        first = "Aguardar recuperação do trabalho em voo." if active else "Fila sem trabalho elegível; aguardar evento do despachante."
        ticket = None
        route = None
        cost = None
        route_event_id = None
    else:
        candidate = eligible[0]
        ticket = candidate.ticket
        status = "ready"
        route, cost, route_event_id = _route(ticket, selected_harness)
        first = (f"Delegar {ticket.id} na etapa development pelo roteador." if route
                 else f"Persistir espera de rota para {ticket.id}; retomar pelo despachante.")
        if route is None:
            status = "wait"
    receipt = CoordinationReceipt(
        project_id=ticket.project_id if ticket else _project_id(root), harness=selected_harness,
        role="coordinator", status=status, in_flight=active, ticket_id=ticket.id if ticket else None,
        first_action=first, route=route, route_event_id=route_event_id,
        cost_available_usd=cost, created_at=datetime.now(timezone.utc).isoformat(),
    )
    target = state_root() / "coordination" / "last_receipt.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
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
