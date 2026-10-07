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
from core.line.agent_cli import (
    HARNESS_CAPABILITIES,
    AgentRequest,
    AgentResult,
    check_optional_mcp_servers,
    headless_development_preamble,
    redact_secrets,
    run_agent,
    supports,
)
from core.line.diagnostics import redacted_head
from core.line.operating_harness import detect_operating_harness
from core.line.routing import _HARNESS_TO_PROVIDER, _default_quota_headroom, load_routing_config, pick
from core.usage.history import history_path, read_history
from core.usage.ticket_cost import format_ticket_quota_summary, record_ticket_quota_cost
from core.roadmap.models import DeliveryStatus

if TYPE_CHECKING:
    from core.git.ticket_workspace import TicketWorkspace

logger = logging.getLogger("darkfac.run_ticket")

# The launcher asks the agent to edit files in the checkout, so the route must declare `write`.
DEVELOPMENT_MODE = "write"


def estimate_ticket_size(ticket: UserTicket) -> tuple[str, list[str]]:
    """Classify ticket as 'small', 'medium', or 'large' based on complexity, criteria, and text volume (USR-113)."""
    reasons: list[str] = []
    complexity = (ticket.estimated_complexity or "").lower().strip()
    if complexity in ("large", "huge", "xl"):
        reasons.append(f"complexidade estimada declarada como '{complexity}'")

    criteria_count = len(ticket.acceptance_criteria)
    if criteria_count >= 5:
        reasons.append(f"elevado número de critérios de aceite ({criteria_count} critérios >= 5)")

    problem_len = len(ticket.problem_statement or "")
    if problem_len >= 1000:
        reasons.append(f"descrição do problema extensa ({problem_len} caracteres >= 1000)")

    total_text = problem_len + sum(len(c) for c in ticket.acceptance_criteria)
    if total_text >= 1500 and not reasons:
        reasons.append(f"volume textual total extenso ({total_text} caracteres >= 1500)")

    for tag in ticket.tags:
        clean = tag.lower().strip()
        if clean in ("epic", "size:large", "complex"):
            reasons.append(f"tag de escopo amplo '{clean}'")

    if reasons:
        return "large", reasons
    return complexity or "medium", []


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
        rows = read_history(history_path(PROJECT_ROOT / ".factory" / "usage" / "providers", provider))
        latest = rows[-1] if rows else {}
        snapshot_file = PROJECT_ROOT / ".factory" / "usage" / "providers" / f"{provider}.json"
        if snapshot_file.is_file():
            try:
                snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                snapshot = {}
        else:
            snapshot = {}
        source = snapshot.get("adapter") or latest.get("adapter") or "sem fonte"
        checked_at = snapshot.get("checked_at") or latest.get("checked_at")
        try:
            from datetime import datetime, timezone
            age_seconds = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))).total_seconds())
            age = f"{age_seconds / 60:.0f} min" if age_seconds < 3600 else f"{age_seconds / 3600:.1f} h"
        except (TypeError, ValueError):
            age = "idade desconhecida"
        raw = snapshot.get("raw_fields") or latest.get("raw_fields") or {}
        from core.usage.history import sanitize_raw
        raw = sanitize_raw(provider, raw)
        flags = snapshot.get("plausibility_flags") or latest.get("flags") or []
        # Unknown remains ineligible for the launch guard, while the report
        # labels it separately from a measured critical percentage.
        is_critical = headroom is None or headroom <= 15.0
        quotas[harness] = {
            "provider": provider,
            "headroom": headroom,
            "is_critical": is_critical,
            "status": "SEM MEDIDOR / SUSPEITA" if headroom is None else ("CRÍTICO (<= 15%)" if is_critical else "SAUDÁVEL"),
            "source": source, "age": age, "raw_fields": raw, "flags": flags,
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
        raw = ", ".join(
            f"{key}={value}" + (" usado" if info.get("provider") == "xai" and key == "usagePercent" else "")
            for key, value in info.get("raw_fields", {}).items()
        ) or "sem leitura bruta"
        flags = "; ".join(info.get("flags", []))
        lines.append(f" - {harness:<12} ({info['provider']:<10}): {val_str:<10} [{info['status']}] "
                     f"[{info.get('source', 'sem fonte')}, {info.get('age', 'idade desconhecida')}, raw {raw}]"
                     + (f" ALERTA: {flags}" if flags else ""))
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
    parser.add_argument(
        "--resume-delivery",
        nargs="?",
        const=True,
        default=None,
        metavar="TICKET_ID",
        help="Reaplicar rebase em origin/main, portão oficial e entrega para uma worktree já desenvolvida (USR-116)",
    )
    parser.add_argument(
        "--worktree",
        default=None,
        help="Caminho da worktree para retomar entrega (padrão: busca automática da worktree ativa do ticket)",
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
    report = GitAutonomyManager(PROJECT_ROOT).deliver_branch(ticket.id, ticket.title, cwd=workspace.path, kind="queue")
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


def rebase_on_origin_main(cwd: Path) -> tuple[bool, list[str], str]:
    """Rebase current branch on origin/main with automatic conflict cleanup (USR-116).

    Returns (ok, conflict_files, message).
    If a conflict occurs, unmerged files are parsed and the rebase is cleanly aborted.
    """
    from core.git.autonomy import _run_git

    _run_git(["fetch", "origin", "main"], cwd=cwd)
    remote_ref = "origin/main"
    if _run_git(["rev-parse", "--verify", remote_ref], cwd=cwd).returncode != 0:
        if _run_git(["rev-parse", "--verify", "main"], cwd=cwd).returncode == 0:
            remote_ref = "main"
        else:
            return True, [], "no base branch found"

    is_anc = _run_git(["merge-base", "--is-ancestor", remote_ref, "HEAD"], cwd=cwd)
    if is_anc.returncode == 0:
        return True, [], "already up to date"

    rebase = _run_git(["rebase", remote_ref], cwd=cwd)
    if rebase.returncode == 0:
        return True, [], "rebased successfully"

    # Conflict occurred
    diff_u = _run_git(["diff", "--name-only", "--diff-filter=U"], cwd=cwd)
    conflict_files = [f.strip() for f in diff_u.stdout.splitlines() if f.strip()]
    _run_git(["rebase", "--abort"], cwd=cwd)
    return False, conflict_files, rebase.stderr.strip() or rebase.stdout.strip()


def resume_delivery(
    ticket_id: str,
    worktree_path: Optional[Path | str] = None,
    skip_validation: bool = False,
    no_commit: bool = False,
    no_push: bool = False,
    as_json: bool = False,
    return_result: bool = False,
) -> Any:
    """Execute the rebase, validation gate, and Git autonomy delivery phase for a ticket (USR-116).

    Runs in a fresh process with fresh module imports from disk.
    Preserves worktree and outputs exact diagnosis if any phase fails.
    """
    from core.git.autonomy import GitAutonomyManager
    from core.git.ticket_workspace import find_ticket_worktree

    target_worktree: Optional[Path] = None
    if worktree_path:
        target_worktree = Path(worktree_path).resolve()
        if not target_worktree.is_dir():
            print(f"[ERRO] Worktree especificada não existe: {target_worktree}", file=sys.stderr)
            if return_result:
                return {"exit_code": 1, "ok": False, "error": f"Worktree not found: {target_worktree}"}
            return 1
    else:
        target_worktree = find_ticket_worktree(ticket_id, PROJECT_ROOT)
        if not target_worktree:
            print(
                f"[ERRO] Nenhuma worktree encontrada para o ticket {ticket_id}. "
                f"Especifique o caminho via --worktree <caminho>.",
                file=sys.stderr,
            )
            if return_result:
                return {"exit_code": 1, "ok": False, "error": f"Worktree not found for {ticket_id}"}
            return 1

    git_mgr = GitAutonomyManager(PROJECT_ROOT)
    store = DemandsStore(target_worktree / ".factory" / "demands" / "demands.json")
    ticket = store.get_ticket(ticket_id)
    if ticket is None:
        shared_store = DemandsStore(PROJECT_ROOT / ".factory" / "demands" / "demands.json")
        ticket = shared_store.get_ticket(ticket_id)
    if ticket is None:
        print(f"[ERRO] Ticket {ticket_id} não encontrado no ledger de demandas.", file=sys.stderr)
        if return_result:
            return {"exit_code": 1, "ok": False, "error": f"Ticket {ticket_id} not found in ledger"}
        return 1

    current_branch = git_mgr._current_branch(target_worktree)
    resume_cmd = (
        f"python C:\\dev\\DarkFac\\run_ticket.py --resume-delivery {ticket.id} "
        f"--worktree {target_worktree}"
    )

    if not as_json:
        print(f"\n[+] Retomando entrega do ticket {ticket.id}...")
        print(f"    Worktree: {target_worktree}")
        print(f"    Branch: {current_branch}")

    # 1. Commit uncommitted changes in worktree if any (required before rebase)
    if not no_commit and git_mgr.is_dirty(cwd=target_worktree):
        if not as_json:
            print("[+] Comitando alterações pendentes antes do rebase...")
        git_mgr.commit_ticket(
            ticket_id=ticket.id,
            title=ticket.title,
            scope="core" if not ticket.tags else ticket.tags[0].replace("user-", ""),
            cwd=target_worktree,
        )

    current_sha = git_mgr.get_current_sha(target_worktree)

    # 2. Rebase on origin/main
    if not as_json:
        print("--> Sincronizando e rebaseando branch em origin/main...")
    ok, conflict_files, msg = rebase_on_origin_main(target_worktree)
    if not ok:
        print("\n" + "=" * 68, file=sys.stderr)
        print(
            f"[ERRO DE CONFLITO NO REBASE] Conflito ao rebasear a branch {current_branch} em origin/main.",
            file=sys.stderr,
        )
        if conflict_files:
            print("Arquivos conflitantes:", file=sys.stderr)
            for cf in conflict_files:
                print(f"  - {cf}", file=sys.stderr)
        else:
            print(f"Detalhes do erro: {msg}", file=sys.stderr)
        print(f"\nWorktree preservada para resolução manual em: {target_worktree}", file=sys.stderr)
        print("Para retomar a entrega após resolver o conflito, execute:", file=sys.stderr)
        print(f"  {resume_cmd}", file=sys.stderr)
        print("=" * 68 + "\n", file=sys.stderr)

        conflict_payload = {
            "exit_code": 2,
            "ok": False,
            "cause": "rebase_conflict",
            "ticket_id": ticket.id,
            "branch": current_branch,
            "current_sha": current_sha,
            "worktree": str(target_worktree),
            "conflict_files": conflict_files,
            "resume_command": resume_cmd,
        }
        if as_json and not return_result:
            print(json.dumps(conflict_payload, indent=2, ensure_ascii=False))
        return conflict_payload if return_result else 2

    current_sha = git_mgr.get_current_sha(target_worktree)

    # 3. Official Validation Gate
    if not skip_validation:
        if not as_json:
            print("\n--> Executando portão oficial da fábrica (python core/harness/runner.py --quick)...")
        gate_res = subprocess.run(
            [sys.executable, str(target_worktree / "core" / "harness" / "runner.py"), "--quick"],
            cwd=str(target_worktree),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if gate_res.returncode != 0:
            print(f"[ERRO NO PORTÃO OFICIAL]:\n{gate_res.stdout}\n{gate_res.stderr}", file=sys.stderr)
            print(f"[i] Worktree preservada para diagnóstico: {target_worktree}", file=sys.stderr)
            print(f"[i] Comando para retomar entrega após correção:\n  {resume_cmd}", file=sys.stderr)
            gate_payload = {
                "exit_code": gate_res.returncode,
                "ok": False,
                "cause": "gate_failed",
                "ticket_id": ticket.id,
                "branch": current_branch,
                "current_sha": current_sha,
                "worktree": str(target_worktree),
                "resume_command": resume_cmd,
            }
            if as_json and not return_result:
                print(json.dumps(gate_payload, indent=2, ensure_ascii=False))
            return gate_payload if return_result else gate_res.returncode

        if not as_json:
            print("[+] Portão oficial aprovado com sucesso: [HARNESS_PASS]!")

    # 4. Autonomous Git Lifecycle Completion (PR, merge, cleanup)
    completion_report = None
    if not no_commit:
        completion_report = git_mgr.complete_ticket(
            ticket_id=ticket.id,
            cwd=target_worktree,
            auto_push=not no_push,
            auto_commit=True,
        )

    delivered = completion_report is None or completion_report.ok
    current_sha = git_mgr.get_current_sha(target_worktree) if target_worktree.exists() else current_sha

    if not delivered:
        error_msg = completion_report.message if completion_report else "complete_ticket failed"
        print("\n" + "=" * 68, file=sys.stderr)
        print(f"[ERRO NA ENTREGA DO TICKET {ticket.id}]:", file=sys.stderr)
        print(f"  Motivo: {error_msg}", file=sys.stderr)
        print(f"  Branch: {current_branch}", file=sys.stderr)
        print(f"  SHA: {current_sha}", file=sys.stderr)
        print(f"  Worktree preservada: {target_worktree}", file=sys.stderr)
        print("\nPara retomar a entrega deste ticket após solucionar o problema, execute:", file=sys.stderr)
        print(f"  {resume_cmd}", file=sys.stderr)
        print("=" * 68 + "\n", file=sys.stderr)

        fail_payload = {
            "exit_code": 1,
            "ok": False,
            "cause": "delivery_failed",
            "ticket_id": ticket.id,
            "error": error_msg,
            "branch": current_branch,
            "current_sha": current_sha,
            "worktree": str(target_worktree),
            "resume_command": resume_cmd,
        }
        if as_json and not return_result:
            print(json.dumps(fail_payload, indent=2, ensure_ascii=False))
        return fail_payload if return_result else 1

    if delivered:
        try:
            git_mgr.sweep_stale(cwd=PROJECT_ROOT)
        except Exception as exc:
            logger.warning("Sweep after delivery warning: %s", exc)

    if not as_json:
        if completion_report:
            print(
                f"[+] Autonomia Git: Ticket {ticket.id} concluído, comitado "
                f"({completion_report.commit_sha}) e sincronizado com sucesso."
            )
        print(f"\n[SUCESSO] Ticket {ticket.id} entregue e validado com sucesso!")

    success_payload = {
        "exit_code": 0,
        "ok": True,
        "ticket_id": ticket.id,
        "branch": current_branch,
        "commit_sha": completion_report.commit_sha if completion_report else current_sha,
        "worktree": str(target_worktree),
    }
    if completion_report and completion_report.sync_result:
        success_payload["sync_action"] = completion_report.sync_result.action
    if as_json and not return_result:
        print(json.dumps(success_payload, indent=2, ensure_ascii=False))

    return success_payload if return_result else 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # 0. Resume delivery mode (USR-116): rebase, validate, and deliver an existing worktree in a fresh process
    if args.resume_delivery:
        target_ticket_id = (
            args.resume_delivery
            if isinstance(args.resume_delivery, str) and args.resume_delivery.strip()
            else args.ticket_id
        )
        if not target_ticket_id:
            print("[ERRO] --resume-delivery requer um TICKET_ID.", file=sys.stderr)
            return 1
        return resume_delivery(
            ticket_id=target_ticket_id,
            worktree_path=Path(args.worktree) if args.worktree else None,
            skip_validation=args.skip_validation,
            no_commit=args.no_commit,
            no_push=args.no_push,
            as_json=args.json,
        )


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

    # 1b. Ticket size and quota risk estimation (USR-113)
    ticket_size, size_reasons = estimate_ticket_size(ticket)
    if ticket_size == "large" and not args.json:
        box_lines = [
            "=" * 68,
            " [AVISO: TICKET ESTIMADO COMO GRANDE - RISCO DE COTA (USR-113)]",
            f" Ticket {ticket.id} possui características de alta complexidade/tamanho:",
            *[f"   - {r}" for r in size_reasons],
            " Risco: pode estourar o timeout do agente (1800s) e consumir cota elevada.",
            " Recomendação: considere quebrar este ticket em demandas menores.",
            "=" * 68,
        ]
        print("\n" + "\n".join(box_lines) + "\n", file=sys.stderr)

    # 2. Routing Decision
    caps = ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"]
    selected_harness: Optional[str] = None
    selected_model: Optional[str] = None
    # The harness the owner operates the factory through is the preferred developer while its quota is
    # above the critical floor (USR-109); None when no signal exists (pump, worker, cloud).
    operating_harness = detect_operating_harness()
    route_reason = "headroom"

    if args.harness:
        target_harness = args.harness.lower().strip()
        harness_info = quotas.get(target_harness)
        if harness_info and harness_info["is_critical"] and not override_granted:
            quota_reason = ("sem medidor verificável" if harness_info["headroom"] is None
                            else f"cota crítica ({harness_info['headroom']}% restante <= 15.0%)")
            print(
                f"\n[BLOQUEIO DE SEGURANÇA - FAIL-CLOSED]\n"
                f"O harness solicitado '{target_harness}' está {quota_reason}.\n"
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
        route_reason = "explicit_harness"
        if not args.json:
            if harness_info and harness_info["is_critical"] and override_granted:
                print(f"[AVISO] Override explícito do usuário ativo: executando em {target_harness} com quota crítica ou desconhecida.")
            else:
                print(f"[+] Harness explicitamente selecionado: {target_harness}")
    else:
        # Automatic router resolution via Dynamic Headroom, among harnesses that can write
        if operating_harness and not args.json:
            print(f"[+] Harness de operacao detectado: {operating_harness}")
        route = pick("development", caps, mode=DEVELOPMENT_MODE, operating_harness=operating_harness)
        if route is None:
            print(
                "\n[ERRO] Nenhuma rota disponível: todas as contas de assinatura estão <= 15% e OpenRouter sem saldo confirmado.",
                file=sys.stderr,
            )
            return 2
        selected_harness, selected_model = route
        preferred_operating = operating_harness is not None and selected_harness == operating_harness
        if preferred_operating:
            route_reason = "operating_harness_preferred"
        if not args.json:
            print(
                f"[+] Roteador selecionou automaticamente: {selected_harness.upper()} "
                f"(Modelo: {selected_model or 'default'}, modo: {DEVELOPMENT_MODE})"
                + (" (preferencia: harness de operacao)" if preferred_operating else "")
            )

    if args.dry_run:
        result_payload = {
            "ticket_id": ticket.id,
            "title": ticket.title,
            "estimated_size": ticket_size,
            "size_reasons": size_reasons,
            "selected_harness": selected_harness,
            "selected_model": selected_model,
            "mode": DEVELOPMENT_MODE,
            "override_granted": override_granted,
            "operating_harness": operating_harness,
            "route_reason": route_reason,
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
    mcp_healthy, mcp_reasons = check_optional_mcp_servers()
    if not mcp_healthy and not args.json:
        print(f"[!] Integração opcional MCP indisponível ({'; '.join(mcp_reasons)}). Continuando em modo degradado.")
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

    before_headroom = quotas.get(selected_harness, {}).get("headroom")

    def _build_request(harness: str, model: Optional[str]) -> AgentRequest:
        from core.git.autonomy import GitAutonomyManager
        git_mgr = GitAutonomyManager(PROJECT_ROOT)
        existing_changes = [
            p for p in git_mgr.changed_paths(workspace.path) if p != LEDGER_RELATIVE
        ]
        continuation_prompt = ""
        if existing_changes:
            changed_preview = ", ".join(existing_changes[:8])
            if len(existing_changes) > 8:
                changed_preview += f" (+{len(existing_changes) - 8} arquivos)"
            continuation_prompt = (
                f"\n\n[AVISO DE CONTINUAÇÃO - WORKTREE JÁ INICIADA (USR-113)]\n"
                f"Uma tentativa anterior foi interrompida (ex.: tempo limite atingido), mas o trabalho já iniciado foi PRESERVADO nesta worktree.\n"
                f"Arquivos já criados ou alterados: {changed_preview}.\n"
                f"NÃO recomece a implementação do zero nem descarte o que foi feito. Inspecione o estado atual dos arquivos na worktree, "
                f"continue o desenvolvimento a partir de onde parou, ajuste os testes unitários e garanta que todos os critérios de aceite passem.\n"
            )

        return AgentRequest(
            prompt=headless_development_preamble(
                harness, quotas.get(harness, {}).get("headroom")
            ) + dev_prompt + continuation_prompt,
            cwd=workspace.path, mode=DEVELOPMENT_MODE, harness=harness, model=model, timeout_s=1800,
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
        quotas_after = inspect_quotas()
        after_headroom = quotas_after.get(selected_harness, {}).get("headroom")
        cost_record = record_ticket_quota_cost(
            ticket_id=ticket.id,
            harness=selected_harness,
            before_headroom=before_headroom,
            after_headroom=after_headroom,
            duration_s=report.total_duration_s,
        )
        if not args.json:
            print(f"\n{format_ticket_quota_summary(cost_record)}", file=sys.stderr)
        _report_workspace_fate(workspace, args.json)
        if args.json:
            print(json.dumps({
                "ok": False,
                "ticket_id": ticket.id,
                "estimated_size": ticket_size,
                "quota_usage": cost_record,
                "attempts": [a.model_dump() for a in report.attempts],
            }, indent=2, ensure_ascii=False))
        return 1

    agent_result = report.result
    if report.route is not None:
        selected_harness, selected_model = report.route  # the harness that actually did the work

    quotas_after = inspect_quotas()
    after_headroom = quotas_after.get(selected_harness, {}).get("headroom")
    cost_record = record_ticket_quota_cost(
        ticket_id=ticket.id,
        harness=selected_harness,
        before_headroom=before_headroom,
        after_headroom=after_headroom,
        duration_s=report.total_duration_s,
    )
    if not args.json:
        print(f"\n{format_ticket_quota_summary(cost_record)}")

    from core.git.autonomy import GitAutonomyManager

    git_mgr = GitAutonomyManager(PROJECT_ROOT)
    implementation_paths = [
        path for path in git_mgr.changed_paths(workspace.path) if path != LEDGER_RELATIVE
    ]
    if not implementation_paths:
        reason = (
            "[ERRO] O agente terminou sem alterar nenhum arquivo de implementacao; "
            "o ticket NAO foi marcado como concluido."
        )
        agent_tail = redact_secrets(agent_result.text).strip()[-1500:]
        print(reason, file=sys.stderr)
        print(f"[i] Saida final do agente: {agent_tail or '(vazia)'}", file=sys.stderr)
        _report_workspace_fate(workspace, args.json)
        if args.json:
            print(json.dumps({
                "ok": False, "cause": "agent_no_changes", "ticket_id": ticket.id,
                "estimated_size": ticket_size,
                "quota_usage": cost_record,
                "workspace": str(workspace.path), "branch": workspace.branch,
                "agent_tail": agent_tail,
            }, indent=2, ensure_ascii=False))
        return 4

    agent_summary = redacted_head(agent_result.text, 800)
    if not args.json:
        print(f"[+] Desenvolvimento concluído pelo {selected_harness.upper()}.")
        print(f"[i] Resumo do agente: {agent_summary or '(vazio)'}")

    # 4b, 5, 6: Execute delivery phase in a fresh, isolated Python process (USR-116).
    # Spawning a fresh process ensures all modules (DemandsStore, UserTicket, GitAutonomyManager)
    # are loaded from current disk state, preventing in-memory schema drift or ValueError
    # caused by background commits to the shared checkout during long agent runs.
    run_script = PROJECT_ROOT / "run_ticket.py"
    use_subprocess = run_script.is_file() and not os.environ.get("PYTEST_CURRENT_TEST")

    completion_info: dict[str, Any] = {}
    exit_code = 0

    if use_subprocess:
        if not args.json:
            print("\n--> Disparando fase de entrega em processo novo isolado (USR-116)...")

        delivery_cmd = [
            sys.executable,
            str(run_script),
            "--resume-delivery", ticket.id,
            "--worktree", str(workspace.path),
            "--json",
        ]
        if args.skip_validation:
            delivery_cmd.append("--skip-validation")
        if args.no_commit:
            delivery_cmd.append("--no-commit")
        if args.no_push:
            delivery_cmd.append("--no-push")

        delivery_proc = subprocess.run(
            delivery_cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        exit_code = delivery_proc.returncode
        if not args.json and delivery_proc.stderr:
            print(delivery_proc.stderr, file=sys.stderr, end="")
        try:
            completion_info = json.loads(delivery_proc.stdout)
        except Exception:
            completion_info = {"ok": exit_code == 0, "error": delivery_proc.stderr or delivery_proc.stdout}
    else:
        # In-process execution (for tests or embedded environments)
        res = resume_delivery(
            ticket_id=ticket.id,
            worktree_path=workspace.path,
            skip_validation=args.skip_validation,
            no_commit=args.no_commit,
            no_push=args.no_push,
            as_json=args.json,
            return_result=True,
        )
        exit_code = res.get("exit_code", 0) if isinstance(res, dict) else int(res)
        completion_info = res if isinstance(res, dict) else {"ok": exit_code == 0}

    delivered = exit_code == 0 and completion_info.get("ok", False)

    if args.json:
        payload = {
            "ok": delivered,
            "ticket_id": ticket.id,
            "estimated_size": ticket_size,
            "quota_usage": cost_record,
            "harness": selected_harness,
            "model": selected_model,
            "duration_s": agent_result.duration_s,
            "workspace": str(workspace.path),
            "branch": workspace.branch,
            "agent_summary": agent_summary,
        }
        if "commit_sha" in completion_info:
            payload["commit_sha"] = completion_info["commit_sha"]
        if "sync_action" in completion_info:
            payload["sync_action"] = completion_info["sync_action"]
        if not delivered and "error" in completion_info:
            payload["error"] = completion_info["error"]
        print(json.dumps(payload, indent=2, ensure_ascii=False))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
