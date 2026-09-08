"""CLI interface for managing User Demands and Backlog tickets."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.demands.models import DemandInput, UserTicket
from core.demands.service import build_default_demands_service
from core.roadmap.models import DeliveryStatus, PlanningHorizon, RoadmapItemType


def main() -> int:
    parser = argparse.ArgumentParser(description="Dark Factory User Demands & Backlog CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # guide command
    guide_p = subparsers.add_parser("guide", help="Analyze and refine a user demand at zero cost ($0)")
    guide_p.add_argument("--title", required=True, help="Demand title")
    guide_p.add_argument("--problem", default="", help="Problem statement")
    guide_p.add_argument("--journey", default="", help="Core user journey")
    guide_p.add_argument("--non-goals", nargs="*", default=[], help="List of non-goals")
    guide_p.add_argument("--criteria", nargs="*", default=[], help="Acceptance criteria")
    guide_p.add_argument("--project", default="darkfac", help="Project identifier")
    guide_p.add_argument("--force-heuristic", action="store_true", help="Force deterministic script without Ollama")
    guide_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # create command
    create_p = subparsers.add_parser("create", help="Create and insert a specified ticket into the backlog")
    create_p.add_argument("--title", required=True, help="Demand title")
    create_p.add_argument("--problem", default="", help="Problem statement")
    create_p.add_argument("--journey", default="", help="Core user journey")
    create_p.add_argument("--non-goals", nargs="*", default=[], help="List of non-goals")
    create_p.add_argument("--criteria", nargs="*", default=[], help="Acceptance criteria")
    create_p.add_argument("--project", default="darkfac", help="Project identifier")
    create_p.add_argument("--horizon", default="now", choices=["now", "next", "later", "exploratory", "unscheduled"])
    create_p.add_argument("--type", default="feature", choices=["feature", "quality", "infrastructure", "operations", "documentation"])
    create_p.add_argument("--force-heuristic", action="store_true", help="Force deterministic script without Ollama")

    # list command
    list_p = subparsers.add_parser("list", help="List user demand tickets")
    list_p.add_argument("--project", default=None, help="Filter by project")
    list_p.add_argument("--status", default=None, help="Filter by status")
    list_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # show command
    show_p = subparsers.add_parser("show", help="Show details of a ticket")
    show_p.add_argument("ticket_id", help="Ticket ID (e.g. USR-01)")
    show_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # status command
    status_p = subparsers.add_parser("status", help="Update ticket status")
    status_p.add_argument("ticket_id", help="Ticket ID (e.g. USR-01)")
    status_p.add_argument("new_status", choices=[s.value for s in DeliveryStatus])
    status_p.add_argument("--notes", default=None, help="Status change notes")

    # grill command
    grill_p = subparsers.add_parser("grill", help="Conduct clarifying Q&A (Grill) to stress scope and refine a ticket")
    grill_p.add_argument("ticket_id", help="Ticket ID (e.g. USR-09)")
    grill_p.add_argument("--auto-accept", action="store_true", help="Auto-accept recommended options headlessly")
    grill_p.add_argument("--force-heuristic", action="store_true", help="Force deterministic questions without Ollama")
    grill_p.add_argument("--answers", default=None, help="JSON string with custom answers dict")
    grill_p.add_argument("--json", action="store_true", help="Output raw JSON")

    args = parser.parse_args()
    service = build_default_demands_service(PROJECT_ROOT)

    if args.command == "guide":
        inp = DemandInput(
            project_id=args.project,
            title=args.title,
            problem_statement=args.problem,
            core_journey=args.journey,
            non_goals=args.non_goals,
            acceptance_criteria=args.criteria,
        )
        guidance = service.guide_demand(inp, force_heuristic=args.force_heuristic)
        if args.json:
            print(guidance.model_dump_json(indent=2))
        else:
            print(f"=== Orientações de Demanda (Motor: {guidance.engine_used} - Custo: ${guidance.cost_usd:.2f}) ===")
            print(f"Score de Prontidão: {guidance.readiness_score}/100 {'[PRONTO]' if guidance.is_ready else '[INCOMPLETO]'}")
            if guidance.missing_elements:
                print("\nPendências identificadas:")
                for m in guidance.missing_elements:
                    print(f"  - {m}")
            if guidance.suggestions:
                print("\nSugestões:")
                for s in guidance.suggestions:
                    print(f"  * {s}")
            if guidance.suggested_ticket:
                t = guidance.suggested_ticket
                print(f"\nTicket sugerido: [{t.id}] {t.title}")
                print(f"Tags: {', '.join(t.tags)}")
                print(f"Reachability: {t.reachability_contract}")
        return 0

    elif args.command == "create":
        inp = DemandInput(
            project_id=args.project,
            title=args.title,
            problem_statement=args.problem,
            core_journey=args.journey,
            non_goals=args.non_goals,
            acceptance_criteria=args.criteria,
            horizon=PlanningHorizon(args.horizon),
            item_type=RoadmapItemType(args.type),
        )
        ticket = service.create_ticket_from_input(inp, force_heuristic=args.force_heuristic)
        print(f"[OK] Ticket {ticket.id} criado com sucesso e incluído no backlog!")
        print(f"Título: {ticket.title}")
        print(f"Tags: {', '.join(ticket.tags)}")
        print(f"Status: {ticket.status.value}")
        return 0

    elif args.command == "list":
        tickets = service.list_tickets(
            project_id=args.project,
            status=DeliveryStatus(args.status) if args.status else None,
        )
        if args.json:
            print(json.dumps([t.model_dump(mode="json") for t in tickets], indent=2))
        else:
            print(f"Demandas do Usuário ({len(tickets)} encontradas):")
            for t in tickets:
                print(f"  [{t.id}] ({t.status.value}) {t.title} [Tags: {', '.join(t.tags)}]")
        return 0

    elif args.command == "show":
        ticket = service.get_ticket(args.ticket_id)
        if not ticket:
            print(f"[ERRO] Ticket {args.ticket_id} não encontrado.", file=sys.stderr)
            return 1
        if args.json:
            print(ticket.model_dump_json(indent=2))
        else:
            print(f"=== Ticket {ticket.id} ===")
            print(f"Título: {ticket.title}")
            print(f"Status: {ticket.status.value} | Horizonte: {ticket.horizon.value} | Tipo: {ticket.item_type.value}")
            print(f"Tags: {', '.join(ticket.tags)}")
            print(f"Problema: {ticket.problem_statement}")
            print("Non-Goals:")
            for ng in ticket.non_goals:
                print(f"  - {ng}")
            print("Critérios de Aceitação:")
            for ac in ticket.acceptance_criteria:
                print(f"  * {ac}")
            print(f"Reachability: {ticket.reachability_contract}")
        return 0

    elif args.command == "status":
        new_stat = DeliveryStatus(args.new_status)
        try:
            updated = service.update_ticket_status(args.ticket_id, new_stat, notes=args.notes)
            print(f"[OK] Ticket {updated.id} atualizado para status: {updated.status.value}")
            return 0
        except KeyError as exc:
            print(f"[ERRO] {exc}", file=sys.stderr)
            return 1

    elif args.command == "grill":
        try:
            session = service.start_grill_session(args.ticket_id, force_heuristic=args.force_heuristic)
        except KeyError as exc:
            print(f"[ERRO] {exc}", file=sys.stderr)
            return 1

        answers: dict[str, str] = {}
        if args.answers:
            try:
                answers = json.loads(args.answers)
            except Exception as e:
                print(f"[ERRO] Formato de answers inválido (JSON esperado): {e}", file=sys.stderr)
                return 1
        elif args.auto_accept:
            for q in session.questions:
                rec = next((opt for opt in q.options if opt.is_recommended), q.options[0] if q.options else None)
                if rec:
                    answers[q.id] = rec.label
        else:
            print(f"\n🔥 === Sessão de Grill: [{session.ticket_id}] ===")
            if session.doc_insights:
                print("\n📚 Contexto e Insights do Projeto:")
                for ins in session.doc_insights:
                    print(f"  * {ins}")

            print("\nPerguntas de Esclarecimento:")
            for i, q in enumerate(session.questions, 1):
                print(f"\n[{i}] {q.question}")
                if q.context_reason:
                    print(f"    (Motivo: {q.context_reason})")
                for o_idx, opt in enumerate(q.options, 1):
                    rec_tag = " [RECOMENDADO]" if opt.is_recommended else ""
                    print(f"    {o_idx}. {opt.label}{rec_tag}")

                try:
                    choice = input("\n    Escolha o número ou digite resposta personalizada [Padrão: 1]: ").strip()
                except (EOFError, KeyboardInterrupt):
                    choice = "1"

                if not choice or choice == "1":
                    rec = next((opt for opt in q.options if opt.is_recommended), q.options[0] if q.options else None)
                    answers[q.id] = rec.label if rec else ""
                elif choice.isdigit() and 1 <= int(choice) <= len(q.options):
                    answers[q.id] = q.options[int(choice) - 1].label
                else:
                    answers[q.id] = choice

        result = service.submit_grill_answers(args.ticket_id, answers, session=session)
        if args.json:
            print(result.model_dump_json(indent=2))
        else:
            print(f"\n[OK] Ticket {result.ticket_id} refinado com sucesso!")
            print(f"Alterações aplicadas:")
            for ch in result.summary_of_changes:
                print(f"  ✓ {ch}")
            print(f"\nNon-goals atuais:")
            for ng in result.refined_ticket.non_goals:
                print(f"  - {ng}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
