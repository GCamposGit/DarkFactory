#!/usr/bin/env python3
"""DarkFac Canonical Ticket Runner (Skill 19-run-ticket).

Executes development tickets using the unified Model Router with Dynamic Headroom,
15% fail-closed quota protection, explicit user override handling, and deterministic
validation gate enforcement.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.line.agent_cli import AgentRequest, AgentResult, run_agent
from core.line.routing import _HARNESS_TO_PROVIDER, _default_quota_headroom, load_routing_config, pick
from core.roadmap.models import DeliveryStatus

logger = logging.getLogger("darkfac.run_ticket")

_OVERRIDE_PATTERNS = [
    re.compile(r"\b(?:forcar|forçar|force)\b", re.IGNORECASE),
    re.compile(r"\bignorar\s+(?:cota|limite|quota)\b", re.IGNORECASE),
    re.compile(r"\b(?:sem\s+cota|cota\s+critica|cota\s+crítica)\b", re.IGNORECASE),
    re.compile(r"\b(?:allow[-_]critical[-_]quota|override)\b", re.IGNORECASE),
]


def check_explicit_override(prompt_text: Optional[str] = None, force_flag: bool = False) -> bool:
    """Check if the user explicitly authorized running below the 15% quota threshold."""
    if force_flag:
        return True
    if not prompt_text:
        return False
    return any(pattern.search(prompt_text) for pattern in _OVERRIDE_PATTERNS)


def inspect_quotas() -> dict[str, dict[str, Any]]:
    """Inspect current quota headroom across all registered harnesses."""
    quotas: dict[str, dict[str, Any]] = {}
    for harness, provider in _HARNESS_TO_PROVIDER.items():
        headroom = _default_quota_headroom(provider)
        is_critical = headroom is None or headroom <= 15.0
        quotas[harness] = {
            "provider": provider,
            "headroom": headroom,
            "is_critical": is_critical,
            "status": "CRÍTICO (<= 15%)" if is_critical else "SAUDÁVEL",
        }
    return quotas


def format_quota_report(quotas: dict[str, dict[str, Any]]) -> str:
    """Format human-readable quota report table."""
    lines = [
        "=" * 68,
        " [DARKFAC] Telemetria de Cotas em Tempo Real (Skill 19-run-ticket)",
        "=" * 68,
    ]
    for harness, info in sorted(quotas.items()):
        val_str = f"{info['headroom']:.1f}%" if info["headroom"] is not None else "DESCONHECIDO / STALE"
        lines.append(f" - {harness:<12} ({info['provider']:<10}): {val_str:<10} [{info['status']}]")
    lines.append("=" * 68)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DarkFac Canonical Ticket Runner (Skill 19-run-ticket)")
    parser.add_argument("ticket_id", nargs="?", default=None, help="Ticket ID to run (e.g. USR-59)")
    parser.add_argument("--harness", default=None, help="Explicitly requested harness (claude, codex, grok, antigravity)")
    parser.add_argument("--force", action="store_true", help="Explicit override to allow running on a critical quota harness")
    parser.add_argument("--prompt", default="", help="User natural language prompt (checked for explicit override)")
    parser.add_argument("--create", action="store_true", help="Create a new ticket before running")
    parser.add_argument("--title", default="", help="Title for newly created ticket")
    parser.add_argument("--problem", default="", help="Problem statement for newly created ticket")
    parser.add_argument("--criteria", nargs="*", default=[], help="Acceptance criteria for newly created ticket")
    parser.add_argument("--project", default="darkfac", help="Project identifier (default: darkfac)")
    parser.add_argument("--dry-run", action="store_true", help="Inspect quotas and resolve route without modifying code")
    parser.add_argument("--skip-validation", action="store_true", help="Skip running runner.py --quick after development")
    parser.add_argument("--json", action="store_true", help="Output raw JSON result")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    quotas = inspect_quotas()
    if not args.json:
        print(format_quota_report(quotas))

    override_granted = check_explicit_override(args.prompt, args.force)

    # 1. Ticket resolution or creation
    store = DemandsStore(PROJECT_ROOT / ".factory" / "demands" / "demands.json")
    ticket: Optional[UserTicket] = None

    if args.create:
        if not args.title:
            print("[ERRO] --title é obrigatório para criar um novo ticket.", file=sys.stderr)
            return 1
        existing_tickets = store.list_tickets()
        num_ids = [int(m.group(1)) for t in existing_tickets if (m := re.match(r"USR-(\d+)", t.id))]
        next_num = (max(num_ids) + 1) if num_ids else 1
        ticket_id = f"USR-{next_num:02d}"
        ticket = UserTicket(
            id=ticket_id,
            project_id=args.project,
            title=args.title,
            problem_statement=args.problem,
            core_journey=[args.problem] if args.problem else [],
            acceptance_criteria=list(args.criteria),
            status=DeliveryStatus.PLANNED,
        )
        store.save_ticket(ticket)
        if not args.json:
            print(f"\n[+] Novo ticket criado e registrado: {ticket.id} - '{ticket.title}'")
    elif args.ticket_id:
        ticket = store.get_ticket(args.ticket_id)
        if ticket is None:
            print(f"[ERRO] Ticket {args.ticket_id} não encontrado no backlog.", file=sys.stderr)
            return 1
        if not args.json:
            print(f"\n[+] Ticket carregado: {ticket.id} - '{ticket.title}'")
    else:
        # No ticket specified; display quota report and exit
        if not args.json:
            print("\nNenhum ticket especificado. Use: python run_ticket.py <TICKET_ID> ou python run_ticket.py --create --title '...'")
        return 0

    # 2. Routing Decision
    caps = ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"]
    selected_harness: Optional[str] = None
    selected_model: Optional[str] = None

    if args.harness:
        target_harness = args.harness.lower().strip()
        harness_info = quotas.get(target_harness)
        if harness_info and harness_info["is_critical"] and not override_granted:
            print(
                f"\n[BLOQUEIO DE SEGURANÇA - FAIL-CLOSED]\n"
                f"O harness solicitado '{target_harness}' está com cota crítica "
                f"({harness_info['headroom']} restante <= 15.0%).\n"
                f"Execução recusada para proteger o saldo da conta.\n"
                f"Para forçar mesmo assim, forneça --force ou inclua autorização explícita no prompt.\n",
                file=sys.stderr,
            )
            # Find recommended healthy harness
            rec_route = pick("development", caps)
            if rec_route:
                print(f"--> Recomendação do Roteador: {rec_route[0].upper()}", file=sys.stderr)
            return 2

        selected_harness = target_harness
        if not args.json:
            if harness_info and harness_info["is_critical"] and override_granted:
                print(f"[AVISO] Override explícito do usuário ativo: executando em {target_harness} mesmo com cota crítica.")
            else:
                print(f"[+] Harness explicitamente selecionado: {target_harness}")
    else:
        # Automatic router resolution via Dynamic Headroom
        route = pick("development", caps)
        if route is None:
            print(
                "\n[ERRO] Nenhuma rota disponível: todas as contas de assinatura estão <= 15% e OpenRouter sem saldo confirmado.",
                file=sys.stderr,
            )
            return 2
        selected_harness, selected_model = route
        if not args.json:
            print(f"[+] Roteador selecionou automaticamente: {selected_harness.upper()} (Modelo: {selected_model or 'default'})")

    if args.dry_run:
        result_payload = {
            "ticket_id": ticket.id,
            "title": ticket.title,
            "selected_harness": selected_harness,
            "selected_model": selected_model,
            "override_granted": override_granted,
            "dry_run": True,
        }
        if args.json:
            print(json.dumps(result_payload, indent=2))
        else:
            print(f"\n[DRY RUN] Execução concluída sem alterações. Rota validada: {selected_harness}")
        return 0

    # 3. Execution Phase
    dev_prompt = (
        f"Voce e o agente de desenvolvimento autonomo da DarkFac.\n"
        f"Implemente o seguinte ticket:\n"
        f"ID: {ticket.id}\n"
        f"Titulo: {ticket.title}\n"
        f"Problema: {ticket.problem_statement}\n"
        f"Criterios de Aceite:\n" + "\n".join(f"- {c}" for c in ticket.acceptance_criteria) + "\n\n"
        f"Escreva/ajuste testes unitarios e o codigo funcional.\n"
    )

    if not args.json:
        print(f"\n--> Iniciando desenvolvimento via {selected_harness.upper()}...")

    agent_req = AgentRequest(
        prompt=dev_prompt,
        cwd=PROJECT_ROOT,
        mode="write",
        harness=selected_harness,
        model=selected_model,
        timeout_s=1800,
    )

    agent_result = run_agent(agent_req)
    if not agent_result.ok:
        print(f"[FALHA NA EXECUÇÃO DO AGENTE]: {agent_result.text}", file=sys.stderr)
        return 1

    if not args.json:
        print(f"[+] Desenvolvimento concluído com sucesso pelo {selected_harness.upper()}.")

    # 4. Official Validation Gate
    if not args.skip_validation:
        if not args.json:
            print("\n--> Executando portão oficial da fábrica (python core/harness/runner.py --quick)...")
        gate_res = subprocess.run(
            [sys.executable, "core/harness/runner.py", "--quick"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if gate_res.returncode != 0:
            print(f"[ERRO NO PORTÃO OFICIAL]:\n{gate_res.stdout}\n{gate_res.stderr}", file=sys.stderr)
            return gate_res.returncode

        if not args.json:
            print("[+] Portão oficial aprovado com sucesso: [HARNESS_PASS]!")

    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "ticket_id": ticket.id,
                    "harness": selected_harness,
                    "model": selected_model,
                    "duration_s": agent_result.duration_s,
                },
                indent=2,
            )
        )
    else:
        print(f"\n[SUCESSO] Ticket {ticket.id} concluído e validado pelo portão oficial.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
