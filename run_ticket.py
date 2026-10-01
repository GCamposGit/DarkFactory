#!/usr/bin/env python3
"""DarkFac Canonical Ticket Runner (Skill 19-run-ticket).

Executes development tickets using the unified Model Router with Dynamic Headroom,
15% fail-closed quota protection, explicit user override handling, and deterministic
validation gate enforcement.

Inner delivery cycle (USR-69): every ticket runs in its OWN worktree
(``.worktrees/<id>-<timestamp>`` on ``ticket/<id>``, created from ``origin/main``), never in the
shared checkout. The agent, the official gate, the commit and the delivery all happen there.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

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
from core.line import agent_retry
from core.line.agent_cli import HARNESS_CAPABILITIES, AgentRequest, AgentResult, run_agent, supports
from core.line.routing import _HARNESS_TO_PROVIDER, _default_quota_headroom, load_routing_config, pick
from core.roadmap.models import DeliveryStatus

if TYPE_CHECKING:
    from core.git.ticket_workspace import TicketWorkspace

logger = logging.getLogger("darkfac.run_ticket")

# The launcher asks the agent to edit files in the checkout, so the route must declare `write`.
DEVELOPMENT_MODE = "write"

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


def _format_optional(value: Any, empty: str = "-") -> str:
    return empty if value is None or value == "" else str(value)


def format_agent_failure(report: agent_retry.RetryReport) -> str:
    """Human-readable failure report: never empty, always harness/model/error kind/exit code/duration/stderr."""
    final = report.attempts[-1] if report.attempts else None
    if final is None and report.result is not None:
        final = agent_retry.AgentAttempt.from_result(0, report.result)
    lines = [
        "[FALHA NA EXECUÇÃO DO AGENTE] nenhuma tentativa concluiu com sucesso "
        f"({len(report.attempts)} tentativa(s), {report.total_duration_s}s no total)."
    ]
    if final is not None:
        stderr = final.stderr_tail.strip()
        lines += [
            f"  harness: {final.harness}",
            f"  modelo: {_format_optional(final.model, 'padrão')}",
            f"  error_kind: {_format_optional(final.error_kind)}",
            f"  exit_code: {_format_optional(final.exit_code, 'n/d')}",
            f"  duração: {final.duration_s}s",
            "  stderr (final):",
            *[f"    {line}" for line in (stderr.splitlines() if stderr else ["(vazio)"])],
            f"  saída do agente: {_format_optional(final.output_head.strip(), '(vazia)')}",
        ]
    if len(report.attempts) > 1:
        lines.append("  histórico:")
        lines += [
            f"    {a.number}. {a.harness}/{_format_optional(a.model, 'padrão')} error_kind={_format_optional(a.error_kind)} "
            f"exit_code={_format_optional(a.exit_code, 'n/d')} duração={a.duration_s}s"
            for a in report.attempts
        ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DarkFac Canonical Ticket Runner (Skill 19-run-ticket)")
    parser.add_argument("ticket_id", nargs="?", default=None, help="Ticket ID to run (e.g. USR-59)")
    parser.add_argument(
        "--harness", default=None,
        help="Explicitly requested harness (must support write mode: claude, codex). Never falls back to another harness",
    )
    parser.add_argument("--force", action="store_true", help="Explicit override to allow running on a critical quota harness")
    parser.add_argument("--prompt", default="", help="User natural language prompt (checked for explicit override)")
    parser.add_argument("--create", action="store_true", help="Create a new ticket before running")
    parser.add_argument("--title", default="", help="Title for newly created ticket")
    parser.add_argument("--problem", default="", help="Problem statement for newly created ticket")
    parser.add_argument("--criteria", nargs="*", default=[], help="Acceptance criteria for newly created ticket")
    parser.add_argument("--project", default="darkfac", help="Project identifier (default: darkfac)")
    parser.add_argument("--dry-run", action="store_true", help="Inspect quotas and resolve route without modifying code")
    parser.add_argument("--skip-validation", action="store_true", help="Skip running runner.py --quick after development")
    parser.add_argument("--no-commit", action="store_true", help="Do not automatically commit after validation pass")
    parser.add_argument("--no-push", action="store_true", help="Do not automatically push to remote after validation pass")
    parser.add_argument("--json", action="store_true", help="Output raw JSON result")
    parser.add_argument(
        "--queue-only",
        action="store_true",
        help="With --create: register the ticket in the development queue (isolated worktree, PR, merge) without running it",
    )
    parser.add_argument(
        "--allow-dirty-shared-checkout",
        action="store_true",
        help="Run even when the shared checkout has uncommitted changes of another ticket (they are never included)",
    )
    return parser


def _build_ticket(args: argparse.Namespace, store: DemandsStore) -> UserTicket:
    """Build the next sequential planned ticket from CLI arguments."""
    return UserTicket(
        id=store.next_ticket_id(args.project),
        project_id=args.project,
        title=args.title,
        problem_statement=args.problem,
        core_journey=[args.problem] if args.problem else [],
        acceptance_criteria=list(args.criteria),
        status=DeliveryStatus.PLANNED,
    )


def _queue_ticket_isolated(args: argparse.Namespace) -> Optional[UserTicket]:
    """Register a planned ticket on origin/main from a throwaway worktree (never the shared checkout)."""
    from core.git.autonomy import GitAutonomyManager
    from core.git.ticket_workspace import WorkspaceError, create_workspace

    try:
        workspace = create_workspace("queue", cwd=PROJECT_ROOT, unique_branch=True)
    except WorkspaceError as exc:
        logger.error("Queue registration: could not create the worktree: %s", exc)
        return None
    store = DemandsStore(workspace.path / ".factory" / "demands" / "demands.json")
    ticket = _build_ticket(args, store)
    store.save_ticket(ticket)
    report = GitAutonomyManager(PROJECT_ROOT).deliver_branch(ticket.id, ticket.title, cwd=workspace.path)
    if not report.ok:
        logger.error("Queue registration of %s failed (%s): %s", ticket.id, report.action, report.message)
    return ticket if report.ok else None


# Runtime state that git reports as modified in the shared checkout without being another
# ticket's work (tracked-but-ignored files, see USR-102): never a reason to refuse a run.
VOLATILE_STATE_PREFIXES: tuple[str, ...] = (
    ".factory/telegram/",
    ".factory/reports/",
    ".factory/usage/",
    ".factory/test_logs/",
    ".factory/tmp/",
    ".factory/pids/",
)


LEDGER_RELATIVE = ".factory/demands/demands.json"


class SharedCheckoutDirty(RuntimeError):
    """The shared checkout holds uncommitted changes that belong to somebody else."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        shown = "\n".join(f"    {p}" for p in paths[:15])
        more = f"\n    ... (+{len(paths) - 15})" if len(paths) > 15 else ""
        super().__init__(
            "O checkout compartilhado tem alterações não commitadas de outro ticket/sessão:\n"
            f"{shown}{more}\n"
            "Cada ticket roda numa worktree própria e nada disso seria incluído no seu commit, mas "
            "rodar a partir de um checkout sujo esconde o trabalho de outra sessão. Commite ou descarte "
            "essas alterações, rode a partir da sua própria worktree, ou use --allow-dirty-shared-checkout "
            "para prosseguir assumindo o risco."
        )


def _prepare_workspace(args: argparse.Namespace, ticket: UserTicket) -> TicketWorkspace:
    """Refuse a dirty shared checkout, then create the ticket's own worktree from origin/main (USR-69).

    The ticket row is copied into the worktree's ledger when origin/main does not have it yet
    (a ticket created by this very run), so the implementation commit carries it.
    """
    from core.git.ticket_workspace import create_workspace, shared_checkout_dirty_paths

    if not args.allow_dirty_shared_checkout:
        dirty = shared_checkout_dirty_paths(PROJECT_ROOT, ignore_prefixes=VOLATILE_STATE_PREFIXES)
        if dirty:
            raise SharedCheckoutDirty(dirty)
    workspace = create_workspace(ticket.id, cwd=PROJECT_ROOT)
    ledger = DemandsStore(workspace.path / ".factory" / "demands" / "demands.json")
    if ledger.get_ticket(ticket.id) is None:
        ledger.save_ticket(ticket)
    return workspace


def _discard_untouched_workspace(workspace: TicketWorkspace) -> bool:
    """Remove a worktree/branch the agent never changed (a failed run leaves nothing behind)."""
    from core.git.autonomy import GitAutonomyManager, _run_git
    from core.git.ticket_workspace import cleanup

    mgr = GitAutonomyManager(PROJECT_ROOT)
    head = _run_git(["rev-parse", "HEAD"], cwd=workspace.path).stdout.strip()
    if not head or head != workspace.base_sha:
        return False
    # The ledger row of a ticket created by this very run is the only change the harness itself makes.
    if [p for p in mgr.changed_paths(workspace.path) if p != LEDGER_RELATIVE]:
        return False
    if not cleanup(workspace.path, workspace.main_root).removed:
        return False
    _run_git(["branch", "-D", workspace.branch], cwd=workspace.main_root)
    return True


def _report_workspace_fate(workspace: TicketWorkspace, as_json: bool) -> None:
    """After a failed agent run: drop the worktree if untouched, otherwise say where the work is."""
    try:
        discarded = _discard_untouched_workspace(workspace)
    except Exception as exc:  # cleanup must never mask the agent failure
        logger.warning("Could not inspect/discard %s: %s", workspace.path, exc)
        discarded = False
    if as_json:
        return
    if discarded:
        print(f"[i] Worktree sem alterações removida: {workspace.path}", file=sys.stderr)
    else:
        print(f"[i] Worktree preservada para diagnóstico: {workspace.path}", file=sys.stderr)


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Fail before any side effect (quota probe, ticket creation, agent call) when the requested harness
    # cannot do what development needs: capabilities are declared, never assumed.
    if args.harness and not (args.create and args.queue_only) and not supports(args.harness, DEVELOPMENT_MODE):
        requested = args.harness.lower().strip()
        declared = ", ".join(sorted(HARNESS_CAPABILITIES.get(requested, frozenset()))) or "nenhuma (harness desconhecido)"
        writers = ", ".join(sorted(h for h, modes in HARNESS_CAPABILITIES.items() if DEVELOPMENT_MODE in modes))
        print(
            f"[ERRO] O harness '{requested}' não suporta o modo '{DEVELOPMENT_MODE}' exigido pelo desenvolvimento "
            f"(capacidades declaradas: {declared}).\n"
            f"Harnesses com escrita: {writers}. Nenhum agente foi consumido.",
            file=sys.stderr,
        )
        return 2

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
        if args.queue_only:
            queued = _queue_ticket_isolated(args)
            if queued is None:
                print("[ERRO] Não foi possível registrar o ticket na fila (fetch/worktree/entrega falhou).", file=sys.stderr)
                return 1
            print(f"[+] Ticket {queued.id} registrado na fila de desenvolvimento: '{queued.title}'")
            return 0
        # The row is NOT written to the shared checkout's ledger: it is added to the ticket's own
        # worktree ledger below and travels in the implementation commit (USR-69/USR-75).
        ticket = _build_ticket(args, store)
        if not args.json:
            print(f"\n[+] Novo ticket criado: {ticket.id} - '{ticket.title}'")
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
            rec_route = pick("development", caps, mode=DEVELOPMENT_MODE)
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
        # Automatic router resolution via Dynamic Headroom, among harnesses that can write
        route = pick("development", caps, mode=DEVELOPMENT_MODE)
        if route is None:
            print(
                "\n[ERRO] Nenhuma rota disponível: todas as contas de assinatura estão <= 15% e OpenRouter sem saldo confirmado.",
                file=sys.stderr,
            )
            return 2
        selected_harness, selected_model = route
        if not args.json:
            print(
                f"[+] Roteador selecionou automaticamente: {selected_harness.upper()} "
                f"(Modelo: {selected_model or 'default'}, modo: {DEVELOPMENT_MODE})"
            )

    if args.dry_run:
        result_payload = {
            "ticket_id": ticket.id,
            "title": ticket.title,
            "selected_harness": selected_harness,
            "selected_model": selected_model,
            "mode": DEVELOPMENT_MODE,
            "override_granted": override_granted,
            "dry_run": True,
        }
        if args.json:
            print(json.dumps(result_payload, indent=2))
        else:
            print(
                f"\n[DRY RUN] Execução concluída sem alterações. "
                f"Rota validada: {selected_harness} (modo: {DEVELOPMENT_MODE})"
            )
        return 0

    # 3. Own worktree (USR-69): the agent, the gate and the commit never touch the shared checkout
    from core.git.ticket_workspace import WorkspaceError

    try:
        workspace = _prepare_workspace(args, ticket)
    except SharedCheckoutDirty as exc:
        print(f"[ERRO] {exc}", file=sys.stderr)
        return 3
    except WorkspaceError as exc:
        print(f"[ERRO] Não foi possível criar a worktree do ticket {ticket.id}: {exc}", file=sys.stderr)
        return 1
    if not args.json:
        print(
            f"[+] Worktree própria: {workspace.path} "
            f"(branch {workspace.branch}, base {workspace.base_ref}@{workspace.base_sha[:12]})"
        )

    # 4. Execution Phase
    dev_prompt = (
        f"Voce e o agente de desenvolvimento autonomo da DarkFac.\n"
        f"Implemente o seguinte ticket:\n"
        f"ID: {ticket.id}\n"
        f"Titulo: {ticket.title}\n"
        f"Problema: {ticket.problem_statement}\n"
        f"Criterios de Aceite:\n" + "\n".join(f"- {c}" for c in ticket.acceptance_criteria) + "\n\n"
        f"Escreva/ajuste testes unitarios e o codigo funcional.\n"
        f"Voce trabalha numa worktree propria deste ticket (branch {workspace.branch}); "
        f"nao leia nem altere arquivos fora dela e nao faca commit, push nem merge: a fabrica entrega.\n"
    )

    if not args.json:
        print(f"\n--> Iniciando desenvolvimento via {selected_harness.upper()}...")

    def _build_request(harness: str, model: Optional[str]) -> AgentRequest:
        return AgentRequest(
            prompt=dev_prompt, cwd=workspace.path, mode=DEVELOPMENT_MODE, harness=harness, model=model, timeout_s=1800,
        )

    def _report_progress(message: str) -> None:
        if not args.json:
            print(f"[!] {message}", file=sys.stderr)

    # Same failure policy as the production line (core.line.agent_retry): transient failures repeat with
    # backoff, then the harness is excluded and the next healthy one is tried. An explicit --harness is
    # honoured: it may retry but never falls through to another harness.
    report = agent_retry.run_with_retry(
        _build_request,
        (selected_harness, selected_model),
        host_caps=caps,
        stage="development",
        mode=DEVELOPMENT_MODE,
        config=load_routing_config(),
        run_func=run_agent,
        pick_func=pick,
        sleep_fn=time.sleep,
        pinned=bool(args.harness),
        on_event=_report_progress,
    )
    if not report.ok or report.result is None:
        failure = format_agent_failure(report)
        print(failure, file=sys.stderr)
        _report_workspace_fate(workspace, args.json)
        if args.json:
            print(json.dumps({
                "ok": False,
                "ticket_id": ticket.id,
                "attempts": [a.model_dump() for a in report.attempts],
            }, indent=2, ensure_ascii=False))
        return 1

    agent_result = report.result
    if report.route is not None:
        selected_harness, selected_model = report.route  # the harness that actually did the work

    if not args.json:
        print(f"[+] Desenvolvimento concluído com sucesso pelo {selected_harness.upper()}.")

    from core.git.autonomy import GitAutonomyManager

    git_mgr = GitAutonomyManager(PROJECT_ROOT)

    # 4b. Commit the agent's work on the ticket branch: the official gate refuses a dirty
    # candidate worktree, so the gate must validate a committed SHA (the worktree's, never the shared checkout).
    if not args.no_commit and git_mgr.is_dirty(cwd=workspace.path):
        git_mgr.commit_ticket(
            ticket_id=ticket.id,
            title=ticket.title,
            scope="core" if not ticket.tags else ticket.tags[0].replace("user-", ""),
            cwd=workspace.path,
        )

    # 5. Official Validation Gate, run inside the ticket worktree
    if not args.skip_validation:
        if not args.json:
            print("\n--> Executando portão oficial da fábrica (python core/harness/runner.py --quick)...")
        gate_res = subprocess.run(
            [sys.executable, str(workspace.path / "core" / "harness" / "runner.py"), "--quick"],
            cwd=str(workspace.path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if gate_res.returncode != 0:
            print(f"[ERRO NO PORTÃO OFICIAL]:\n{gate_res.stdout}\n{gate_res.stderr}", file=sys.stderr)
            print(f"[i] Worktree preservada para diagnóstico: {workspace.path}", file=sys.stderr)
            return gate_res.returncode

        if not args.json:
            print("[+] Portão oficial aprovado com sucesso: [HARNESS_PASS]!")

    # 6. Autonomous Git Lifecycle Completion (USR-57): ledger closed in the same commit (USR-94), PR, merge, cleanup
    completion_report = None
    if not args.no_commit:
        completion_report = git_mgr.complete_ticket(
            ticket_id=ticket.id,
            cwd=workspace.path,
            auto_push=not args.no_push,
            auto_commit=True,
        )
        if not args.json:
            if completion_report.ok:
                print(
                    f"[+] Autonomia Git: Ticket {ticket.id} concluído, comitado "
                    f"({completion_report.commit_sha}) e sincronizado com sucesso."
                )
            else:
                print(f"[!] Aviso Autonomia Git: {completion_report.message}", file=sys.stderr)

    delivered = completion_report is None or completion_report.ok
    if not delivered:
        print(
            f"[ERRO] A entrega do ticket {ticket.id} não foi concluída; worktree preservada: {workspace.path}",
            file=sys.stderr,
        )
    elif workspace.path.exists() and not args.json:
        print(f"[i] Worktree mantida em {workspace.path} (sem entrega automática nesta execução).")

    if args.json:
        payload = {
            "ok": delivered,
            "ticket_id": ticket.id,
            "harness": selected_harness,
            "model": selected_model,
            "duration_s": agent_result.duration_s,
            "workspace": str(workspace.path),
            "branch": workspace.branch,
        }
        if completion_report:
            payload["commit_sha"] = completion_report.commit_sha
            if completion_report.sync_result:
                payload["sync_action"] = completion_report.sync_result.action
        print(json.dumps(payload, indent=2))
    elif delivered:
        print(f"\n[SUCESSO] Ticket {ticket.id} concluído e validado pelo portão oficial.")

    return 0 if delivered else 1


if __name__ == "__main__":
    sys.exit(main())
