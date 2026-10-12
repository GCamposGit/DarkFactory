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
import contextlib
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
from core.line import agent_retry, cancellation, local_cancel
from core.line.agent_cli import (
    HARNESS_CAPABILITIES,
    AgentRequest,
    AgentResult,
    check_optional_mcp_servers,
    _run_bounded,
    headless_development_preamble,
    redact_secrets,
    run_agent,
    supports,
)
from core.line.diagnostics import redacted_head
from core.line.local_progress import ENV_RUN_ID, ENV_WARNED, ProgressPublisher, open_progress
from core.line.operating_harness import detect_operating_harness
from core.line.routing import (
    _HARNESS_TO_PROVIDER,
    _default_quota_headroom,
    harness_cli_pin,
    load_routing_config,
    pick,
)
from core.line.stage_build import distill_log
from core.usage.history import history_path, read_history
from core.usage.ticket_cost import format_ticket_quota_summary, record_ticket_quota_cost
from core.roadmap.models import DeliveryStatus

if TYPE_CHECKING:
    from core.git.ticket_workspace import TicketWorkspace

logger = logging.getLogger("darkfac.run_ticket")

# The launcher asks the agent to edit files in the checkout, so the route must declare `write`.
DEVELOPMENT_MODE = "write"

# Exit code of a run cancelled by the owner (USR-166): the conventional 128 + SIGINT.
EXIT_CANCELLED = 130
# Upper bound of the official gate; it also lets a cancellation kill the gate's process tree.
GATE_TIMEOUT_S = 7200.0
# Upper bound of the delivery subprocess (gate, PR, CI wait, merge): same as the board's "presumed dead".
DELIVERY_TIMEOUT_S = 6 * 3600.0
# How many times the delivery phase reinvokes the agent in-place after a focal-test or gate failure,
# before giving the ticket back with a resume command (USR-245). 2 reinvocations = 3 total attempts.
GATE_FIX_ATTEMPTS = 2
# Upper bound of the focal-test pre-check (fast, scoped to the changed files' tests), well under
# GATE_TIMEOUT_S since it exists to catch an unverified agent turn before paying for the full gate.
FOCAL_TEST_TIMEOUT_S = 300.0
# Harnesses the in-place fix agent (USR-245) may be routed to; same set `_run()` offers development.
_DEV_HOST_CAPS: tuple[str, ...] = ("harness:claude", "harness:codex", "harness:grok", "harness:antigravity")


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
        help=(
            "Explicitly requested harness (must support write mode: claude, codex). Never falls back to "
            "another harness, except on a harness_infra_error (USR-188: the harness's own terminal/sandbox "
            "tooling failed to start the call)"
        ),
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
        "--cancel",
        nargs="?",
        const=True,
        default=None,
        metavar="TICKET_ID",
        help="Cancelar o run_ticket em andamento deste ticket (grava o arquivo de controle; USR-166)",
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


def _resume_preserved_workspace(ticket: UserTicket, *, as_json: bool) -> Optional[TicketWorkspace]:
    """Worktree an interrupted earlier attempt of this ticket left behind, or None (USR-225).

    A run that timed out keeps its worktree (and the agent's partial work); a second `run_ticket <ID>`
    must continue there instead of starting from scratch in a new worktree. The most recent preserved
    worktree with work is reused; the others are only listed. Lookup failures never block a fresh start.
    """
    from core.git.ticket_workspace import find_preserved_workspaces

    try:
        lookup = find_preserved_workspaces(ticket.id, cwd=PROJECT_ROOT, ignore_paths=(LEDGER_RELATIVE,))
    except Exception as exc:  # noqa: BLE001 - a lookup problem falls back to a new worktree
        logger.warning("Could not look for a preserved worktree of %s: %s", ticket.id, exc)
        return None
    for note in lookup.notes:
        logger.info("Preserved-worktree candidate skipped: %s", note)
    chosen = lookup.chosen
    if chosen is None:
        return None
    logger.info("Reusing preserved worktree %s for %s", chosen.workspace.path, ticket.id)
    if not as_json:
        print(
            f"[+] Reaproveitando a worktree preservada de uma tentativa anterior: {chosen.workspace.path} "
            f"(branch {chosen.workspace.branch}; {len(chosen.changed_paths)} arquivo(s) alterado(s), "
            f"{chosen.commits_ahead} commit(s) fora da base)",
            file=sys.stderr,
        )
        for other in lookup.others:
            print(
                f"[i] Outra worktree preservada do mesmo ticket (nao usada, nada foi apagado): {other.workspace.path} "
                f"({len(other.changed_paths)} arquivo(s), {other.commits_ahead} commit(s))",
                file=sys.stderr,
            )
    return chosen.workspace


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
    workspace = _resume_preserved_workspace(ticket, as_json=bool(getattr(args, "json", False)))
    if workspace is None:
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


_TEST_FILE_RE = re.compile(r"(^|/)tests?/.*test_[^/]+\.py$|(^|/)test_[^/]+\.py$")


def _implementation_diff_paths(cwd: Path) -> list[str]:
    """Repo-relative paths this ticket touched: the committed diff since the rebased base, plus any
    still-uncommitted edit (e.g. one just made by an in-place self-correction agent call, USR-245)."""
    from core.git.autonomy import GitAutonomyManager, _run_git

    base_ref = "origin/main"
    if _run_git(["rev-parse", "--verify", base_ref], cwd=cwd).returncode != 0:
        base_ref = "main"
    committed = _run_git(["diff", "--name-only", f"{base_ref}...HEAD"], cwd=cwd)
    committed_paths = (
        [p.strip() for p in committed.stdout.splitlines() if p.strip()] if committed.returncode == 0 else []
    )
    dirty_paths = GitAutonomyManager(PROJECT_ROOT).changed_paths(cwd)
    merged = list(dict.fromkeys([*committed_paths, *dirty_paths]))
    return [p for p in merged if p != LEDGER_RELATIVE]


def _focal_test_targets(cwd: Path, changed_paths: list[str]) -> list[str]:
    """Test files to run as a fast pre-check before the official gate (USR-245).

    Includes every changed test file verbatim, plus any OTHER test file under ``tests/`` whose source
    mentions a changed (non-test) module's filename: a module a ticket edits can break a test it never
    touches (the USR-201 rescue: 3 tests broke that the agent never looked at). Best-effort by design —
    a module whose name is too generic to search for, or a test with no textual reference, is simply not
    found; the full official gate downstream is still the final word.
    """
    targets: set[str] = set()
    module_stems: list[str] = []
    for raw in changed_paths:
        if not raw.endswith(".py"):
            continue
        normalized = raw.replace("\\", "/")
        if _TEST_FILE_RE.search(normalized):
            if (cwd / normalized).is_file():
                targets.add(normalized)
            continue
        stem = Path(normalized).stem
        if stem and stem != "__init__":
            module_stems.append(stem)

    if module_stems:
        tests_dir = cwd / "tests"
        if tests_dir.is_dir():
            pattern = re.compile(r"\b(" + "|".join(re.escape(s) for s in dict.fromkeys(module_stems)) + r")\b")
            for test_file in tests_dir.rglob("test_*.py"):
                try:
                    text = test_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if pattern.search(text):
                    targets.add(str(test_file.relative_to(cwd)).replace("\\", "/"))
    return sorted(targets)


def _run_focal_tests(cwd: Path, targets: list[str]) -> Optional[Any]:
    """Run pytest scoped to `targets`; `None` when there is nothing to focus on (USR-245).

    A ``DistilledTestReport`` (core.harness.test_subagent) on success or failure; never raises — a
    broken pre-check must never block the real gate from having the final word.
    """
    if not targets:
        return None
    from core.harness.test_subagent import TestExecutionInstruction, TestSubagentEngine

    engine = TestSubagentEngine(project_root=cwd)
    instruction = TestExecutionInstruction(
        target=None, extra_args=list(targets), timeout_seconds=int(FOCAL_TEST_TIMEOUT_S), worker_mode="local"
    )
    try:
        return engine.execute_local(instruction)
    except Exception as exc:  # noqa: BLE001 - a broken focal pre-check must never block the real gate
        logger.warning("Focal test pre-check could not run in %s: %s", cwd, exc)
        return None


def _build_gate_fix_prompt(ticket: UserTicket, stage_label: str, feedback: str) -> str:
    return (
        f"Voce e o agente de desenvolvimento autonomo da DarkFac corrigindo o ticket {ticket.id} "
        f"('{ticket.title}') depois que a fase de entrega reprovou nos {stage_label}.\n"
        f"NAO encerre o turno esperando uma tarefa em segundo plano (ex.: 'vou esperar o pytest terminar'): "
        f"a correcao so conta como concluida quando os testes abaixo passarem de fato, de forma sincrona, "
        f"antes de voce terminar o turno.\n\n"
        f"## Log destilado da falha\n{feedback}\n\n"
        f"Corrija o codigo e/ou os testes nesta mesma worktree (nao crie outra, nao faca checkout de outra "
        f"branch). Nao faca commit, push nem merge: a fabrica entrega.\n"
    )


def _invoke_fix_agent(
    target_worktree: Path, ticket: UserTicket, stage_label: str, feedback: str
) -> Optional[AgentResult]:
    """Reinvoke the development agent in-place with the distilled failure, for self-correction (USR-245).

    Best-effort: if no route is available or the call itself fails, the next focal/gate attempt simply
    fails again for the same reason, which the caller already treats as an (eventually final) failure.
    """
    route = pick("development", list(_DEV_HOST_CAPS), mode=DEVELOPMENT_MODE)
    if route is None:
        logger.warning("No route available to reinvoke the agent for %s after a delivery failure", ticket.id)
        return None
    harness, model = route
    cli_model, cli_effort = harness_cli_pin(harness, model, stage="development", config=load_routing_config())
    headroom = inspect_quotas().get(harness, {}).get("headroom")
    request = AgentRequest(
        prompt=headless_development_preamble(harness, headroom) + _build_gate_fix_prompt(ticket, stage_label, feedback),
        cwd=target_worktree,
        mode=DEVELOPMENT_MODE,
        harness=harness,
        model=cli_model,
        effort=cli_effort,
        timeout_s=1800,
    )
    try:
        return run_agent(request)
    except Exception as exc:  # noqa: BLE001 - a crashed correction attempt must not crash the delivery
        logger.warning("Fix agent invocation failed for %s: %s", ticket.id, exc)
        return None


def _run_gate_with_self_correction(
    target_worktree: Path,
    current_branch: str,
    current_sha: str,
    ticket: UserTicket,
    resume_cmd: str,
    skip_validation: bool,
    as_json: bool,
    return_result: bool,
    progress: ProgressPublisher,
    cancelled_payload: Any,
) -> Any:
    """Focal tests + the official gate, self-correcting up to GATE_FIX_ATTEMPTS times (USR-245).

    A headless agent that ends its turn deferring verification to a background task ("vou esperar o
    pytest em segundo plano") never gets to see the gate fail before the ticket is handed back to the
    operator (the USR-201 rescue). The focal pre-check runs regardless of `--skip-validation` (it is
    the new, fast, mandatory check); `--skip-validation` only still skips the expensive official gate.

    Returns `None` on success (the caller proceeds to commit/push), or the usual failure payload/exit
    code once every attempt is exhausted.
    """
    last_output = ""
    last_cause = "gate_failed"
    last_exit_code = 1

    for attempt in range(GATE_FIX_ATTEMPTS + 1):
        if _cancelled():
            return cancelled_payload()

        changed = _implementation_diff_paths(target_worktree)
        focal_targets = _focal_test_targets(target_worktree, changed)
        focal_report = _run_focal_tests(target_worktree, focal_targets) if focal_targets else None

        if focal_report is not None and not focal_report.success:
            last_output = focal_report.agent_feedback
            last_cause = "focal_tests_failed"
            last_exit_code = focal_report.exit_code or 1
            if not as_json:
                print(
                    f"[ERRO NOS TESTES FOCAIS]: {focal_report.concise_summary}\n{focal_report.agent_feedback}",
                    file=sys.stderr,
                )
            if attempt >= GATE_FIX_ATTEMPTS:
                break
            progress.phase(
                "gate", "failed",
                f"testes focais reprovados (tentativa {attempt + 1}/{GATE_FIX_ATTEMPTS + 1}): "
                f"{focal_report.concise_summary}",
                cause="focal_tests_failed",
            )
            if not as_json:
                print(f"\n--> Reinvocando o agente na mesma worktree para corrigir ({', '.join(focal_targets)})...")
            _invoke_fix_agent(target_worktree, ticket, "testes focais dos arquivos alterados", focal_report.agent_feedback)
            if _cancelled():
                return cancelled_payload()
            continue

        if _cancelled():
            return cancelled_payload()

        if skip_validation:
            progress.phase("gate", "skipped", "validacao pulada (--skip-validation)")
            return None

        progress.phase("gate", "running", "portao oficial (core/harness/runner.py --quick)")
        if not as_json:
            print("\n--> Executando portão oficial da fábrica (python core/harness/runner.py --quick)...")
        # Bounded runner: a cancellation (USR-166) kills the gate's whole process tree.
        gate_res = _run_bounded(
            [sys.executable, str(target_worktree / "core" / "harness" / "runner.py"), "--quick"],
            cwd=target_worktree,
            timeout_s=GATE_TIMEOUT_S,
        )
        if getattr(gate_res, "cancelled", False) is True or _cancelled():
            return cancelled_payload()

        if gate_res.returncode == 0:
            progress.phase("gate", "succeeded", "portao oficial aprovado")
            if not as_json:
                print("[+] Portão oficial aprovado com sucesso: [HARNESS_PASS]!")
            return None

        last_output = f"{gate_res.stdout}\n{gate_res.stderr}"
        last_cause = "gate_failed"
        last_exit_code = gate_res.returncode
        if not as_json:
            print(f"[ERRO NO PORTÃO OFICIAL]:\n{gate_res.stdout}\n{gate_res.stderr}", file=sys.stderr)
        if attempt >= GATE_FIX_ATTEMPTS:
            break
        progress.phase(
            "gate", "failed",
            f"portao oficial falhou (tentativa {attempt + 1}/{GATE_FIX_ATTEMPTS + 1}, codigo {gate_res.returncode}): "
            f"{redacted_head((gate_res.stdout or gate_res.stderr or '')[-600:], 300)}",
            cause="gate_failed",
        )
        if not as_json:
            print("\n--> Reinvocando o agente na mesma worktree com o log destilado do portao...")
        _invoke_fix_agent(target_worktree, ticket, "portao oficial (python core/harness/runner.py --quick)", distill_log(last_output))
        if _cancelled():
            return cancelled_payload()

    print(f"[i] Worktree preservada para diagnóstico: {target_worktree}", file=sys.stderr)
    print(f"[i] Comando para retomar entrega após correção:\n  {resume_cmd}", file=sys.stderr)
    progress.phase(
        "gate", "failed",
        f"{last_cause} apos {GATE_FIX_ATTEMPTS + 1} tentativa(s): {redacted_head(last_output[-600:], 300)}",
        cause=last_cause,
    )
    gate_payload = {
        "exit_code": last_exit_code,
        "ok": False,
        "cause": last_cause,
        "ticket_id": ticket.id,
        "branch": current_branch,
        "current_sha": current_sha,
        "worktree": str(target_worktree),
        "resume_command": resume_cmd,
    }
    if as_json and not return_result:
        print(json.dumps(gate_payload, indent=2, ensure_ascii=False))
    return gate_payload if return_result else gate_payload["exit_code"]


def _cancelled() -> bool:
    """True when the run in this process was cancelled (control file, signal or the line's token)."""
    token = cancellation.current_token()
    return token is not None and token.refresh()


def _cancel_message() -> str:
    token = cancellation.current_token()
    reason = token.reason if token is not None else cancellation.CANCELLED_CAUSE
    how = "sinal" if reason == local_cancel.SIGNAL_REASON else "arquivo de controle"
    return f"execucao cancelada pelo owner ({how})"


def _abort_cancelled(
    progress: ProgressPublisher,
    ticket_id: str,
    as_json: bool,
    *,
    workspace: Optional[TicketWorkspace] = None,
) -> int:
    """Close a cancelled launcher run (USR-166): live board `cancelada`, no delivery, exit 130."""
    message = _cancel_message()
    progress.cancel(message)
    print(
        f"[CANCELADO] Ticket {ticket_id}: {message}. "
        f"Nenhum commit, push ou PR foi feito apos o cancelamento.",
        file=sys.stderr,
    )
    if workspace is not None:
        _report_workspace_fate(workspace, as_json)
    if as_json:
        print(json.dumps({"ok": False, "cause": "cancelled", "ticket_id": ticket_id, "reason": message},
                         indent=2, ensure_ascii=False))
    return EXIT_CANCELLED


def _finish_protected_path(
    progress: ProgressPublisher,
    ticket: UserTicket,
    workspace: TicketWorkspace,
    report: agent_retry.RetryReport,
    *,
    as_json: bool,
    harness: str,
    before_headroom: Optional[float],
    ticket_size: str,
) -> int:
    """Stop a protected-path block on the first attempt and register the owner action (USR-205).

    The worktree is kept even when git reports no implementation diff: discarding it is what made
    the repeated cancellations look like a ticket that was never tried. The protected file is not
    edited; the owner action points at ``docs/proposals``.
    """
    from core.line.protected_edit import register_protected_path_owner_action

    result = report.result
    protected = (result.protected_path if result is not None and result.protected_path else "") or "caminho protegido"
    owner_action_id = ""
    try:
        action = register_protected_path_owner_action(
            ticket_id=ticket.id,
            protected_path=protected,
            workspace_path=workspace.path,
            checkout_root=workspace.main_root,
        )
        owner_action_id = action.id
    except Exception as exc:  # still a human block: do not retry the agent because the backlog write failed
        logger.warning("Could not register the protected-path owner action for %s: %s", ticket.id, exc)

    message = (
        f"[BLOQUEIO HUMANO] Ticket {ticket.id}: edicao em caminho protegido ({protected}) "
        f"cancelada na primeira tentativa (error_kind=protected_path). "
        f"Nenhum outro harness sera tentado com o mesmo escopo. "
        f"Entregue a proposta e o patch em docs/proposals (modelo "
        f"docs/proposals/USR-141-147-harness-resilience.md) e nao edite o arquivo protegido. "
        f"Acao do owner: {owner_action_id or 'nao registrada'}."
    )
    progress.phase("agent", "failed", message, cause="protected_path", harness=harness)
    print(message, file=sys.stderr)
    print(f"[i] Worktree preservada: {workspace.path}", file=sys.stderr)
    cost_record = record_ticket_quota_cost(
        ticket_id=ticket.id,
        harness=harness,
        before_headroom=before_headroom,
        after_headroom=inspect_quotas().get(harness, {}).get("headroom"),
        duration_s=report.total_duration_s,
    )
    if not as_json:
        print(f"\n{format_ticket_quota_summary(cost_record)}", file=sys.stderr)
    else:
        print(json.dumps({
            "ok": False,
            "cause": "protected_path",
            "ticket_id": ticket.id,
            "protected_path": protected,
            "owner_action": owner_action_id or None,
            "estimated_size": ticket_size,
            "quota_usage": cost_record,
            "workspace": str(workspace.path),
            "branch": workspace.branch,
            "attempts": [a.model_dump() for a in report.attempts],
        }, indent=2, ensure_ascii=False))
    return 1


def resume_delivery(
    ticket_id: str,
    worktree_path: Optional[Path | str] = None,
    skip_validation: bool = False,
    no_commit: bool = False,
    no_push: bool = False,
    as_json: bool = False,
    return_result: bool = False,
    progress: Optional[ProgressPublisher] = None,
) -> Any:
    """Execute the rebase, validation gate, and Git autonomy delivery phase for a ticket (USR-116).

    Runs in a fresh process with fresh module imports from disk.
    Preserves worktree and outputs exact diagnosis if any phase fails.

    Phase progress (USR-140) goes to the live line board: through ``progress`` when the caller passes
    its own publisher, otherwise through one opened here (joining the parent run when the launcher
    exported ``DARKFAC_LOCAL_RUN_ID``). A publisher opened here for a standalone resume closes its run.
    """
    holder: list[ProgressPublisher] = [progress] if progress is not None else []
    code = 1
    try:
        result = _resume_delivery(
            ticket_id, worktree_path, skip_validation, no_commit, no_push, as_json, return_result, holder
        )
        code = int(result.get("exit_code", 1)) if isinstance(result, dict) else int(result)
        return result
    finally:
        if progress is None and holder:
            if code == EXIT_CANCELLED:
                holder[0].cancel(_cancel_message())
            else:
                holder[0].finish(code == 0, "" if code == 0 else f"entrega terminou com codigo {code}")


def _resume_delivery(
    ticket_id: str,
    worktree_path: Optional[Path | str],
    skip_validation: bool,
    no_commit: bool,
    no_push: bool,
    as_json: bool,
    return_result: bool,
    holder: list[ProgressPublisher],
) -> Any:
    with contextlib.ExitStack() as stack:
        return _resume_delivery_scoped(
            ticket_id, worktree_path, skip_validation, no_commit, no_push, as_json, return_result, holder, stack
        )


def _resume_delivery_scoped(
    ticket_id: str,
    worktree_path: Optional[Path | str],
    skip_validation: bool,
    no_commit: bool,
    no_push: bool,
    as_json: bool,
    return_result: bool,
    holder: list[ProgressPublisher],
    stack: contextlib.ExitStack,
) -> Any:
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

    session_probe_path = os.environ.get("DARKFAC_CODEX_SESSION_PATH", "").strip()
    if session_probe_path:  # USR-79: aviso somente leitura de chamada de ferramenta orfa
        from core.line.codex_session_probe import warn_if_unanswered

        warn_if_unanswered(session_probe_path, logger)

    if holder:
        progress = holder[0]
    else:
        progress = open_progress(ticket.id, ticket.project_id, ticket.title)
        holder.append(progress)
    # USR-166: honour the ticket's cancel request (control file or signal). Reuses the launcher's scope
    # when the delivery runs in-process; a delivery subprocess joins the parent's run and never erases
    # the request it did not create.
    stack.enter_context(
        local_cancel.watch(
            ticket.id,
            progress.run_id,
            fresh=progress.owns_run and not os.environ.get(local_cancel.ENV_PARENT),
            not_before=local_cancel.LAUNCHED_AT,
        )
    )
    git_mgr.on_phase = lambda phase, status, message, cause: progress.phase(  # type: ignore[arg-type]
        phase, status, message, cause=cause
    )

    current_branch = git_mgr._current_branch(target_worktree)
    resume_cmd = (
        f"python C:\\dev\\DarkFac\\run_ticket.py --resume-delivery {ticket.id} "
        f"--worktree {target_worktree}"
    )

    if not as_json:
        print(f"\n[+] Retomando entrega do ticket {ticket.id}...")
        print(f"    Worktree: {target_worktree}")
        print(f"    Branch: {current_branch}")

    def cancelled_payload() -> Any:
        """Nothing is committed, pushed or merged after the cancellation (USR-166)."""
        message = _cancel_message()
        progress.cancel(message)
        print(f"[CANCELADO] Entrega do ticket {ticket.id} interrompida: {message}.", file=sys.stderr)
        print(f"[i] Worktree preservada: {target_worktree}", file=sys.stderr)
        payload = {
            "exit_code": EXIT_CANCELLED,
            "ok": False,
            "cause": "cancelled",
            "ticket_id": ticket.id,
            "branch": current_branch,
            "worktree": str(target_worktree),
            "resume_command": resume_cmd,
        }
        if as_json and not return_result:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        return payload if return_result else EXIT_CANCELLED

    try:
        return _deliver_worktree(
            git_mgr, ticket, target_worktree, current_branch, resume_cmd,
            skip_validation, no_commit, no_push, as_json, return_result, progress, cancelled_payload,
        )
    except cancellation.RunCancelledError:
        return cancelled_payload()


def _deliver_worktree(
    git_mgr: Any,
    ticket: UserTicket,
    target_worktree: Path,
    current_branch: str,
    resume_cmd: str,
    skip_validation: bool,
    no_commit: bool,
    no_push: bool,
    as_json: bool,
    return_result: bool,
    progress: ProgressPublisher,
    cancelled_payload: Any,
) -> Any:
    """Rebase, gate and delivery of one worktree, checking the cancel request before every step."""
    # 1. Commit uncommitted changes in worktree if any (required before rebase)
    if _cancelled():
        return cancelled_payload()
    progress.phase("gate", "running", "sincronizando com origin/main antes do portao")
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
    if _cancelled():
        return cancelled_payload()
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

        progress.phase(
            "gate",
            "failed",
            f"conflito no rebase em origin/main: {', '.join(conflict_files[:5]) or msg}",
            cause="rebase_conflict",
        )
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

    # 3. Focal tests + Official Validation Gate, with in-place self-correction (USR-245)
    if _cancelled():
        return cancelled_payload()
    gate_outcome = _run_gate_with_self_correction(
        target_worktree, current_branch, current_sha, ticket, resume_cmd,
        skip_validation, as_json, return_result, progress, cancelled_payload,
    )
    if gate_outcome is not None:
        return gate_outcome

    # 4. Autonomous Git Lifecycle Completion (PR, merge, cleanup)
    if _cancelled():
        return cancelled_payload()
    completion_report = None
    if not no_commit:
        completion_report = git_mgr.complete_ticket(
            ticket_id=ticket.id,
            cwd=target_worktree,
            auto_push=not no_push,
            auto_commit=True,
        )
        if _cancelled() and (completion_report is None or not completion_report.ok):
            return cancelled_payload()

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

        progress.set_outcome(error_msg, "delivery_failed")
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
        progress.phase(
            "deploy", "skipped", "deploy pos-merge nao faz parte do run_ticket (scripts/dokploy_redeploy.py)"
        )
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


def _describe_workspace(workspace: TicketWorkspace) -> str:
    """One-line description for the live board; progress text must never raise into the launcher."""
    try:
        return f"{workspace.path.name} ({workspace.branch}@{workspace.base_sha[:12]})"
    except Exception:  # noqa: BLE001
        return "worktree criada"


def main(argv: Optional[list[str]] = None) -> int:
    """Run the launcher and report its phases to the live line board (USR-140, best-effort)."""
    box: list[ProgressPublisher] = []
    code = 1
    try:
        code = _main(argv, box)
        return code
    finally:
        if box:
            box[0].release()
            if code == EXIT_CANCELLED:
                box[0].cancel(_cancel_message())
            else:
                if code != 0:
                    box[0].set_outcome(f"run_ticket terminou com codigo {code}", f"exit_{code}", only_if_unset=True)
                box[0].finish(code == 0)


def _main(argv: Optional[list[str]], box: list[ProgressPublisher]) -> int:
    """Parse the arguments and run; the cancellation scope of the run lives as long as this call."""
    with contextlib.ExitStack() as stack:
        return _run(argv, box, stack)


def _request_cancel(args: argparse.Namespace) -> int:
    """`--cancel TICKET_ID`: ask the run in progress for that ticket to stop (USR-166)."""
    target = args.cancel if isinstance(args.cancel, str) and args.cancel.strip() else args.ticket_id
    if not target:
        print("[ERRO] --cancel requer um TICKET_ID.", file=sys.stderr)
        return 1
    path = local_cancel.request_cancel(target.strip())
    print(
        f"[+] Cancelamento do ticket {target.strip()} solicitado ({path}). "
        f"O run_ticket em andamento encerra o agente (e a arvore de processos) em ate alguns segundos "
        f"e nao faz commit nem push depois disso."
    )
    return 0


def _run(argv: Optional[list[str]], box: list[ProgressPublisher], stack: contextlib.ExitStack) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # 00. Cancel mode (USR-166): only writes the control file the running launcher watches
    if args.cancel:
        return _request_cancel(args)

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

    # Live line board (USR-140): best-effort phase events. Held until the run is known not to be a dry
    # run; a publishing failure can never block or change the outcome of this launcher.
    progress = open_progress(ticket.id, ticket.project_id, ticket.title)
    progress.hold()
    box.append(progress)
    progress.phase("preflight", "running", "cota, tamanho do ticket e roteamento")

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
            progress.phase(
                "preflight", "failed", f"harness {target_harness} recusado: {quota_reason}", cause="quota_critical"
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
            progress.phase("preflight", "failed", "nenhuma rota disponivel (cotas <= 15%)", cause="no_route")
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
        progress.discard()
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

    progress.phase(
        "preflight",
        "succeeded",
        f"rota {selected_harness} (modelo {selected_model or 'default'}, motivo {route_reason})",
        harness=selected_harness,
    )
    progress.release()

    # Cancellation scope (USR-166): the same token/guards the line uses (USR-152), fed by the per-ticket
    # control file and termination signals. `_run_bounded` kills the agent's process tree when it fires.
    stack.enter_context(
        local_cancel.watch(
            ticket.id, progress.run_id, fresh=progress.owns_run, not_before=local_cancel.LAUNCHED_AT
        )
    )
    if _cancelled():
        return _abort_cancelled(progress, ticket.id, args.json)

    # 3. Own worktree (USR-69): the agent, the gate and the commit never touch the shared checkout
    from core.git.ticket_workspace import WorkspaceError

    progress.phase("workspace", "running", "criando worktree propria a partir de origin/main")
    try:
        workspace = _prepare_workspace(args, ticket)
    except SharedCheckoutDirty as exc:
        progress.phase("workspace", "failed", "checkout compartilhado sujo", cause="shared_checkout_dirty")
        print(f"[ERRO] {exc}", file=sys.stderr)
        return 3
    except WorkspaceError as exc:
        progress.phase("workspace", "failed", str(exc), cause="workspace_error")
        print(f"[ERRO] Não foi possível criar a worktree do ticket {ticket.id}: {exc}", file=sys.stderr)
        return 1
    progress.phase("workspace", "succeeded", _describe_workspace(workspace))
    if not args.json:
        print(
            f"[+] Worktree própria: {workspace.path} "
            f"(branch {workspace.branch}, base {workspace.base_ref}@{workspace.base_sha[:12]})"
        )
    if _cancelled():
        return _abort_cancelled(progress, ticket.id, args.json, workspace=workspace)

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
        # A preserved worktree (USR-225) may hold commits already; they count as work too.
        try:
            from core.git.autonomy import _run_git

            committed = _run_git(["diff", "--name-only", f"{workspace.base_sha}..HEAD"], cwd=workspace.path)
        except Exception as exc:  # noqa: BLE001 - the continuation hint is best effort
            logger.debug("Could not list committed changes in %s: %s", workspace.path, exc)
            committed = None
        if committed is not None and committed.returncode == 0:
            committed_paths = [p.strip() for p in committed.stdout.splitlines() if p.strip()]
            existing_changes = list(
                dict.fromkeys(existing_changes + [p for p in committed_paths if p != LEDGER_RELATIVE])
            )
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

        cli_model, cli_effort = harness_cli_pin(
            harness, model, stage="development", config=load_routing_config()
        )
        return AgentRequest(
            prompt=headless_development_preamble(
                harness, quotas.get(harness, {}).get("headroom")
            ) + dev_prompt + continuation_prompt,
            cwd=workspace.path,
            mode=DEVELOPMENT_MODE,
            harness=harness,
            model=cli_model,
            effort=cli_effort,
            timeout_s=1800,
        )

    def _report_progress(message: str) -> None:
        progress.phase("agent", "running", message)
        if not args.json:
            print(f"[!] {message}", file=sys.stderr)

    progress.phase("agent", "running", f"desenvolvimento via {selected_harness}", harness=selected_harness)

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
    cancelled_by_owner = (
        report.result is not None and report.result.error_kind == "cancelled"
    ) or _cancelled()
    if cancelled_by_owner and (not report.ok or report.result is None):
        # The agent was cancelled (its process tree is already dead): record the quota it burned and stop.
        cost_record = record_ticket_quota_cost(
            ticket_id=ticket.id,
            harness=selected_harness,
            before_headroom=before_headroom,
            after_headroom=inspect_quotas().get(selected_harness, {}).get("headroom"),
            duration_s=report.total_duration_s,
        )
        if not args.json:
            print(f"\n{format_ticket_quota_summary(cost_record)}", file=sys.stderr)
        return _abort_cancelled(progress, ticket.id, args.json, workspace=workspace)
    if report.result is not None and agent_retry.is_protected_path_block(report.result):
        return _finish_protected_path(
            progress,
            ticket,
            workspace,
            report,
            as_json=args.json,
            harness=report.result.harness or selected_harness or "unknown",
            before_headroom=before_headroom,
            ticket_size=ticket_size,
        )
    if not report.ok or report.result is None:
        failure = format_agent_failure(report)
        progress.phase("agent", "failed", failure, cause="agent_failed")
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
    if agent_result.model:
        selected_model = agent_result.model  # the model the CLI reported, not only the cascade alias
    selected_effort = agent_result.effort

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

    if _cancelled():  # cancelled right as the agent finished: do not even look at its output
        return _abort_cancelled(progress, ticket.id, args.json, workspace=workspace)

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
        progress.phase(
            "agent", "failed", "o agente terminou sem alterar nenhum arquivo de implementacao",
            cause="agent_no_changes", harness=selected_harness,
        )
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
    progress.phase(
        "agent", "succeeded", f"{len(implementation_paths)} arquivo(s) alterado(s)", harness=selected_harness
    )
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

        child_env = dict(os.environ)
        # The delivery subprocess joins this run for cancellation too: it must honour the request the
        # parent watches and never erase it as "stale" (it starts later than the request was written).
        child_env[local_cancel.ENV_PARENT] = progress.run_id
        if progress.enabled:
            # The delivery subprocess joins this run: same run id, so the board shows one run.
            child_env[ENV_RUN_ID] = progress.run_id
        if progress.warning:
            # USR-154: the user was already warned; the subprocess must not repeat it.
            child_env[ENV_WARNED] = "1"
        # Bounded runner (USR-166): a cancellation of this launcher also kills the delivery's process tree.
        delivery_proc = _run_bounded(
            delivery_cmd,
            cwd=PROJECT_ROOT,
            env=child_env,
            timeout_s=DELIVERY_TIMEOUT_S,
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
            progress=progress,
        )
        exit_code = res.get("exit_code", 0) if isinstance(res, dict) else int(res)
        completion_info = res if isinstance(res, dict) else {"ok": exit_code == 0}

    delivery_done = exit_code == 0 and completion_info.get("ok", False)
    if (
        exit_code == EXIT_CANCELLED
        or completion_info.get("cause") == "cancelled"
        or (_cancelled() and not delivery_done)  # e.g. the delivery process was killed with this launcher
    ):
        return _abort_cancelled(progress, ticket.id, args.json, workspace=workspace)

    delivered = exit_code == 0 and completion_info.get("ok", False)
    if not delivered:
        failure_text = completion_info.get("error") or completion_info.get("cause") or f"codigo {exit_code}"
        progress.set_outcome(f"entrega falhou: {failure_text}", str(completion_info.get("cause") or "delivery_failed"))

    if args.json:
        payload = {
            "ok": delivered,
            "ticket_id": ticket.id,
            "estimated_size": ticket_size,
            "quota_usage": cost_record,
            "harness": selected_harness,
            "model": selected_model,
            "effort": selected_effort,
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
