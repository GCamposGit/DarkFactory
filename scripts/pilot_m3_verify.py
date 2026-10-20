"""Read-only verifier and evidence writer for USR-279 pilot milestone M3."""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from core.intake.project_routing import resolve_demand
from core.projects import registry as project_registry
from core.projects.registry import ProjectRegistry


@dataclass(frozen=True)
class M3Report:
    checked_at: str
    external_id: str
    ticket_id: str | None
    project_id: str | None
    has_ph_ticket: bool
    has_usr_ticket_for_entry: bool
    real_message_received: bool
    routing_excerpt: str
    oa_047_should_remain_open: bool


def verify_m3(
    *,
    demands_path: Path,
    projects_file: Path | None,
    external_id: str,
    demand_text: str,
) -> M3Report:
    """Correlate a Telegram external id to the ledger and independently exercise project routing."""
    from core.demands.store import DemandsStore

    previous = project_registry._GLOBAL_REGISTRY
    if projects_file is not None:
        project_registry._GLOBAL_REGISTRY = ProjectRegistry(projects_file)
    try:
        tickets = DemandsStore(demands_path).list_tickets()
        matched = [
            ticket
            for ticket in tickets
            if external_id in ticket.tags or any(external_id in step for step in ticket.core_journey)
        ]
        ph_match = next(
            (ticket for ticket in matched if ticket.id.startswith("PH-") and ticket.project_id == "pilot-hello"),
            None,
        )
        match = ph_match or (matched[0] if matched else None)
        # Unknown-project onboarding writes only inside this throwaway directory.
        with tempfile.TemporaryDirectory(prefix="pilot-m3-routing-") as temporary_state:
            route = resolve_demand(
                f"pilot-hello: {demand_text}",
                Path(temporary_state),
                external_id=external_id,
                requested_by=None,
            )
        import inspect

        source = inspect.getsource(resolve_demand)
        lines = source.splitlines()
        start = next((i for i, line in enumerate(lines) if line.startswith("def resolve_demand")), 0)
        routing_excerpt = "\n".join(lines[start : start + 14])
        has_usr = any(ticket.id.startswith("USR-") for ticket in matched)
        has_ph = ph_match is not None
        return M3Report(
            checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
            external_id=external_id,
            ticket_id=match.id if match else None,
            project_id=match.project_id if match else None,
            has_ph_ticket=has_ph,
            has_usr_ticket_for_entry=has_usr,
            real_message_received=match is not None,
            routing_excerpt=f"resolve_demand returned action={route.action}, project_id={route.project_id}\n{routing_excerpt}",
            oa_047_should_remain_open=not has_ph or has_usr,
        )
    finally:
        project_registry._GLOBAL_REGISTRY = previous


def render_evidence(report: M3Report) -> str:
    state = "correlacionada" if report.real_message_received else "nao encontrada no ledger"
    ticket = report.ticket_id or "nenhum"
    project = report.project_id or "nenhum"
    return (
        "# USR-279 M3 - evidencia de intake\n\n"
        f"Verificado em: {report.checked_at}\n\n"
        f"- Mensagem Telegram: `{report.external_id}` ({state})\n"
        f"- Ticket correlacionado: `{ticket}`\n"
        f"- Projeto do ticket: `{project}`\n"
        f"- Ticket PH do projeto pilot-hello: `{str(report.has_ph_ticket).lower()}`\n"
        f"- Ticket USR- criado para esta entrada: `{str(report.has_usr_ticket_for_entry).lower()}`\n"
        f"- OA-047 deve permanecer aberta: `{str(report.oa_047_should_remain_open).lower()}`\n\n"
        "## Trecho exercitado do roteamento\n\n"
        "```text\n" + report.routing_excerpt + "\n```\n\n"
        "A prova de rota acima e local e read-only quanto ao backlog de onboarding. "
        "Ela nao substitui a mensagem real do Owner nem prova a recepcao do webhook.\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demands", type=Path, default=Path(".factory/demands/demands.json"))
    parser.add_argument("--projects", type=Path)
    parser.add_argument("--external-id", default="tg-msg-owner-unobserved")
    parser.add_argument(
        "--text",
        default="pagina inicial com o texto DarkFac pilot e endpoint /healthz que devolve status ok e a versao",
    )
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    report = verify_m3(
        demands_path=args.demands,
        projects_file=args.projects,
        external_id=args.external_id,
        demand_text=args.text,
    )
    output = render_evidence(report)
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(output, encoding="utf-8")
    print(json.dumps(report.__dict__, indent=2, ensure_ascii=False))
    return 0 if report.has_ph_ticket and not report.has_usr_ticket_for_entry else 1


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
