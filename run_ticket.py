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
from core.line import agent_retry, cancellation, line_lock, local_cancel
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
from core.line import gate_ram, grokbox_wake, stage_planning, stage_review, ticket_engine, ticket_stages
from core.line.stages import Stage, StageArtifactMissing, require_artifact
from core.line.diagnostics import failed_test_ids, redacted_head
from core.line.local_progress import ENV_RUN_ID, ENV_WARNED, ProgressPublisher, open_progress
from core.workflow import execution_registry
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
# 600s (not 300s): even the capped, changed-files-only target set observed ~106s on an idle worker,
# but this machine's xdist is capped to 1 worker under memory pressure (`_worker_capacity`), and a
# concurrent session on the shared host (USR-188: the same contention this ticket's fix addresses for
# Codex) can slow a serial run 3x+; 300s turned a passing pre-check into a false-positive timeout.
FOCAL_TEST_TIMEOUT_S = 600.0
# Cap on test files the "mentions a changed module" sweep in `_focal_test_targets` may add beyond the
# changed test files themselves (USR-188). A foundational module (e.g. `routing.py`, `run_ticket.py`)
# is genuinely imported by dozens of test files; running all of them serially-equivalent (this machine's
# memory caps xdist to 1 worker, see `_worker_capacity`) blew well past FOCAL_TEST_TIMEOUT_S. Past the
# cap, the sweep is dropped entirely rather than trimmed arbitrarily — the pre-check stays fast (just
# the changed test files) and the official gate remains the final, complete word either way.
FOCAL_SWEEP_MAX_EXTRA_FILES = 15
# FOCAL_TEST_TIMEOUT_S above was calibrated against the changed-files-only set (~106s observed on an
# idle worker); it was never budgeted for a full sweep of up to FOCAL_SWEEP_MAX_EXTRA_FILES extra files.
# USR-249 (USR-245's own delivery): a legitimate 16-file sweep (15 extra, right at the cap) exceeded
# 600s serialized under this host's 1-worker memory cap, even though every individual test passed --
# the flat floor undercounted a sweep at the boundary it itself allows. The budget now scales with how
# many files were actually swept, keeping the flat floor for small target sets (<=8 files, matching the
# original calibration) and bounded by a ceiling well under GATE_TIMEOUT_S so a maximal sweep still
# fails fast relative to the full gate.
FOCAL_TEST_TIMEOUT_PER_TARGET_S = 75.0
FOCAL_TEST_TIMEOUT_CEILING_S = 1800.0
# Harnesses the in-place fix agent (USR-245) may be routed to; same set `_run()` offers development.
_DEV_HOST_CAPS: tuple[str, ...] = ("harness:claude", "harness:codex", "harness:grok", "harness:antigravity")
# Upper bound of one in-place fix-agent reinvocation (USR-245's `_invoke_fix_agent`); reported alongside
# every reinvocation in `attempts_log` (USR-249) the same way `GATE_TIMEOUT_S`/`FOCAL_TEST_TIMEOUT_S` are.
FIX_AGENT_TIMEOUT_S = 1800.0
# USR-375 S4: PLAN/REVIEW without an eligible route WAIT (route_wait semantics): the run ends with the
# fleet's requeue code (fleet_dispatcher.INFRA_EXIT_CODES), the ticket stays `planned` and is retried
# after the cooldown; never a hard failure.
EXIT_ROUTE_WAIT = 75
ROUTE_WAIT_MARKER = "harness_infra_error: route_wait"
# USR-375 S4: in-process attempts of PLAN and REVIEW for a transient `retry` outcome (agent hiccup).
PLAN_ATTEMPTS = 2
REVIEW_ATTEMPTS = 4


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
    parser.add_argument(
        "--non-goal",
        dest="non_goals",
        action="append",
        default=[],
        help="Explicit scope limit for newly created ticket (repeatable). Never invented: omit when unknown.",
    )
    parser.add_argument("--project", default="darkfac", help="Project identifier (default: darkfac)")
    parser.add_argument("--dry-run", action="store_true", help="Inspect quotas and resolve route without modifying code")
    parser.add_argument("--skip-validation", action="store_true", help="Skip running runner.py --quick after development")
    parser.add_argument("--no-commit", action="store_true", help="Do not automatically commit after validation pass")
    parser.add_argument("--no-push", action="store_true", help="Do not automatically push to remote after validation pass")
    parser.add_argument("--json", action="store_true", help="Output raw JSON result")
    parser.add_argument(
        "--queue-only",
        action="store_true",
        help="With --create: register the ticket in the development queue (control store, no PR) without running it",
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
    parser.add_argument(
        "--fleet",
        action="store_true",
        default=False,
        help="Habilitar coordenacao na frota e reserva duravel de ciclo no host (USR-284)",
    )
    parser.add_argument(
        "--host-id",
        type=str,
        default=None,
        help="Identificador do host na frota (grokbox, notebook, desktop)",
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
        non_goals=[g.strip() for g in (getattr(args, "non_goals", None) or []) if g and g.strip()],
        acceptance_criteria=list(args.criteria),
        status=DeliveryStatus.PLANNED,
    )


def _queue_ticket_isolated(args: argparse.Namespace) -> Optional[UserTicket]:
    """Register a planned ticket in the control store (USR-343): no worktree, no PR, no merge."""
    from core.demands.queue_store import register_ticket

    ticket = _build_ticket(args, DemandsStore(PROJECT_ROOT / ".factory" / "demands" / "demands.json"))
    try:
        return register_ticket(ticket)
    except Exception as exc:  # noqa: BLE001 - reported to the caller as a failed registration
        logger.error("Queue registration of %s failed in the control store: %s", ticket.id, exc)
        return None


# Runtime state that git reports as modified in the shared checkout without being another
# ticket's work (tracked-but-ignored files, see USR-102): never a reason to refuse a run.
VOLATILE_STATE_PREFIXES: tuple[str, ...] = (
    ".factory/telegram/",
    ".factory/reports/",
    ".factory/usage/",
    ".factory/test_logs/",
    ".factory/tmp/",
    ".factory/pids/",
    ".factory/line_lock/",
    ".factory/local_cancel/",
    ".factory/executions/active/",
    ".factory/hub_availability.json",
)

# Evidencias de runtime das OA (capturas/logs/scripts gerados), nunca versionadas (USR-352).
_OA_EVIDENCE_RE = re.compile(r"^\.factory/owner_actions/OA-[^/]+\.(?:png|jsonl|ps1)$")


def is_runtime_artifact(path: str) -> bool:
    """True para arquivo gerado pela propria fabrica que nao conta como trabalho de outro ticket."""
    return path.startswith(VOLATILE_STATE_PREFIXES) or bool(_OA_EVIDENCE_RE.match(path))


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

    if ticket.project_id != "darkfac":
        from core.projects.registry import get_project_registry
        from core.projects.ticket_execution import project_checkout, setup_project

        project = get_project_registry().get_project(ticket.project_id)
        if project is None:
            raise RuntimeError(f"Projeto '{ticket.project_id}' não existe em .factory/projects.json")
        if not project.commands.validate_cmds:
            raise RuntimeError(
                f"Projeto {project.id} não declara commands.validate. Execute o Project Adoption Gateway "
                "(skill 07) no onboarding e declare comandos de validação antes de executar tickets."
            )
        project_root = project_checkout(project)
        workspace = create_workspace(ticket.id, cwd=project_root, base=project.default_branch)
        setup = setup_project(project, workspace.path)
        if not setup.ok:
            from core.git.ticket_workspace import cleanup

            cleanup(workspace.path, workspace.main_root)
            raise RuntimeError(f"commands.setup do projeto {project.id} falhou:\n{setup.combined_output}")
        # External repositories do not carry DarkFac's central demand ledger.
        return workspace

    if not args.allow_dirty_shared_checkout:
        dirty = [
            p
            for p in shared_checkout_dirty_paths(PROJECT_ROOT, ignore_prefixes=VOLATILE_STATE_PREFIXES)
            if not is_runtime_artifact(p)
        ]
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

    mgr = GitAutonomyManager(workspace.main_root)
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


def rebase_on_project_base(cwd: Path, base: str) -> tuple[bool, list[str], str]:
    """Synchronize a client ticket branch with the project's registered default branch."""
    from core.git.autonomy import _run_git

    _run_git(["fetch", "origin", base], cwd=cwd)
    remote_ref = f"origin/{base}"
    if _run_git(["rev-parse", "--verify", remote_ref], cwd=cwd).returncode != 0:
        return False, [], f"base registrada não encontrada: {remote_ref}"
    if _run_git(["merge-base", "--is-ancestor", remote_ref, "HEAD"], cwd=cwd).returncode == 0:
        return True, [], "already up to date"
    rebase = _run_git(["rebase", remote_ref], cwd=cwd)
    if rebase.returncode == 0:
        return True, [], "rebased successfully"
    diff_u = _run_git(["diff", "--name-only", "--diff-filter=U"], cwd=cwd)
    conflicts = [f.strip() for f in diff_u.stdout.splitlines() if f.strip()]
    _run_git(["rebase", "--abort"], cwd=cwd)
    return False, conflicts, rebase.stderr.strip() or rebase.stdout.strip()


def repair_owner_action_duplicates_before_gate(git_mgr: Any, ticket_id: str, cwd: Path) -> list[str]:
    """Renumber OA-NNN collisions with origin/main right after the rebase, before the gate (USR-255).

    A rebase can merge two branches that each picked the next free OA-NNN (neither had seen the
    other's commit) without a textual conflict, leaving the same id twice in
    ``owner_actions.json``. Left alone, that is only caught once the official gate's
    ``tests/test_owner_actions_core.py::test_seeded_backlog_is_valid_and_complete`` fails with
    "item OA-NNN duplicado ignorado" -- well after the rebase already succeeded (~25min lost on
    USR-249's delivery on 2026-10-14). Returns the ``"OLD -> NEW"`` lines, or ``[]`` when nothing
    collided; never raises.
    """
    from core.owner_actions.dedupe import OWNER_ACTIONS_RELATIVE, renumber_duplicate_owner_actions

    try:
        changes = renumber_duplicate_owner_actions(cwd, remote_ref="origin/main")
    except Exception as exc:  # a repair attempt must never block the delivery
        logger.warning("owner_actions: dedupe pre-portao falhou para %s: %s", ticket_id, exc)
        return []
    if not changes:
        return []
    logger.info("owner_actions: renumerado pre-portao para %s: %s", ticket_id, "; ".join(changes))
    message = (
        f"fix(owner-actions): renumera OA duplicado apos sync com origin/main [{ticket_id}]\n\n"
        + "\n".join(changes)
        + f"\n\nDarkFac-Ticket: {ticket_id}\nDarkFac-Autonomy: zero-human-touch\n"
    )
    try:
        git_mgr.commit_ticket(
            ticket_id, "renumera OA duplicado apos sync", cwd=cwd,
            paths=[OWNER_ACTIONS_RELATIVE], custom_message=message,
        )
    except Exception as exc:
        logger.warning("owner_actions: commit da renumeracao pre-portao falhou para %s: %s", ticket_id, exc)
    return changes


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
    actually imports or references a changed (non-test) module: a module a ticket edits can break a
    test it never touches (the USR-201 rescue: 3 tests broke that the agent never looked at).
    Best-effort by design — a module whose name is too generic to search for, or a test with no textual
    reference, is simply not found; the full official gate downstream is still the final word.

    The reference must look like an import or an attribute/call on the module (``import stem``,
    ``stem import ...``, ``stem.attr``, ``stem(...)``), not just the bare word anywhere in the file:
    a stem like ``routing`` or ``run_ticket`` is also plain prose/an unrelated identifier, and a naive
    whole-file word search swept in dozens of unrelated test files (USR-188: 61 targets for a 2-module
    change, serialized well past ``FOCAL_TEST_TIMEOUT_S``).
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
            stem_patterns = [
                rf"\bimport\s+{re.escape(s)}\b|\b{re.escape(s)}\s+import\b|\b{re.escape(s)}\.\w|\b{re.escape(s)}\("
                for s in dict.fromkeys(module_stems)
            ]
            pattern = re.compile("(?:" + "|".join(stem_patterns) + ")")
            swept: set[str] = set()
            for test_file in tests_dir.rglob("test_*.py"):
                try:
                    text = test_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if pattern.search(text):
                    swept.add(str(test_file.relative_to(cwd)).replace("\\", "/"))
            if len(swept - targets) <= FOCAL_SWEEP_MAX_EXTRA_FILES:
                targets |= swept
    return sorted(targets)


def _run_focal_tests(cwd: Path, targets: list[str]) -> Optional[Any]:
    """Run pytest scoped to `targets`; `None` when there is nothing to focus on (USR-245).

    A ``DistilledTestReport`` (core.harness.test_subagent) on success or failure; never raises — a
    broken pre-check must never block the real gate from having the final word.

    Runs under pytest-xdist (``-n auto --dist worksteal``), same as the official gate's parallel step
    (`harness.config.json`), excluding ``-m serial`` tests the same way: those assume exclusive access
    (shared state, fixed ports, the suite lock) and the official gate re-checks them anyway, so the
    pre-check only needs the parallel-safe majority. Without this, `TestSubagentEngine.execute_local`
    runs plain single-process pytest, and even a handful of target files can exceed
    ``FOCAL_TEST_TIMEOUT_S`` serially (USR-188). The actual timeout budget scales with ``len(targets)``
    (``_focal_test_timeout_s``, USR-249) rather than staying flat at ``FOCAL_TEST_TIMEOUT_S``.
    """
    if not targets:
        return None
    if gate_ram.remote_workers_configured():
        logger.info(
            "[FOCAL] pre-teste local pulado: workers remotos configurados; "
            "disponibilidade e validacao integral ficam para o portao oficial"
        )
        return None

    ram_wait = gate_ram.wait_for_gate_ram(announce=logger.info)
    if not ram_wait.ok:
        logger.warning(
            "[FOCAL] pre-teste local pulado: RAM insuficiente apos %.0f s de espera "
            "(%.1f GiB livres, %s workers possiveis); o portao oficial decidira se pode executar",
            ram_wait.waited_s,
            ram_wait.reading.available_gib,
            ram_wait.reading.workers,
        )
        return None

    from core.harness.test_subagent import TestExecutionInstruction, TestSubagentEngine

    engine = TestSubagentEngine(project_root=cwd)
    extra_args = ["-n", "auto", "--dist", "worksteal", "-m", "not serial", *targets]
    instruction = TestExecutionInstruction(
        target=None,
        extra_args=extra_args,
        timeout_seconds=int(_focal_test_timeout_s(len(targets))),
        worker_mode="local",
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
    route = pick("development", list(_DEV_HOST_CAPS), mode=DEVELOPMENT_MODE, project_id=ticket.project_id)
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
        timeout_s=int(FIX_AGENT_TIMEOUT_S),
    )
    try:
        return run_agent(request)
    except Exception as exc:  # noqa: BLE001 - a crashed correction attempt must not crash the delivery
        logger.warning("Fix agent invocation failed for %s: %s", ticket.id, exc)
        return None


# Substrings in the official gate's combined stdout/stderr that mean a failure was a timeout or an
# infrastructure hiccup, not something a reinvoked agent could fix by editing code (USR-249): a harness
# step hitting its own internal budget (`core/harness/runner.py::run_step`, `[STEP_FAIL] ... (timeout
# exceeded)`), a remote test worker lost mid-run whose local fallback then runs out of road the same
# way (`core/harness/remote_dispatch.py`, USR-178: `[REMOTE_JOB_LOST]`/`[REMOTE_FALLBACK]`), and the
# machine-wide suite lock (`core/harness/suite_lock.py`, USR-249) spending the whole step budget queued
# behind a sibling session's run on the same host instead of ever reaching collection -- reproduced live
# while diagnosing USR-249's own delivery: a concurrent worktree held the lock for the full window,
# leaving `discovered_count=0` with no code-level failure to fix.
_GATE_TIMEOUT_MARKERS = ("(timeout exceeded)", "[SUITE_LOCK] timed out")
# Remote-worker loss markers are infra only when the output carries no real pytest failure: if the
# worker dropped and the local fallback then ran and reprovou a real test, that is fixable by the agent.
# `[SUITE_LOCK] acquired slot`/`waiting` also appear in normal runs and are NOT infra signals.
_GATE_REMOTE_INFRA_MARKERS = ("[REMOTE_JOB_LOST]", "[REMOTE_FALLBACK]")
_PYTEST_REAL_FAILURE_RE = re.compile(r"^(?:FAILED|ERROR) |\b\d+ (?:failed|errors?)\b", re.MULTILINE)
# `TestSubagentEngine.execute_local` (core/harness/test_subagent.py) sets this exit code only from
# `subprocess.TimeoutExpired`; any other nonzero code is an actual pytest failure the agent can act on.
_FOCAL_TIMEOUT_EXIT_CODE = 124


def _gate_failure_is_timeout(gate_res: Any) -> bool:
    """True when an official-gate failure was a timeout or infra issue, not a real test failure.

    Covers the outer `_run_bounded` deadline (`timed_out`) as well as the text markers above, which
    show up with the gate's own internal step budget still returning 0/1/2 (never 124) to this process.
    """
    if getattr(gate_res, "timed_out", False):
        return True
    combined = f"{gate_res.stdout}\n{gate_res.stderr}"
    if any(marker in combined for marker in _GATE_TIMEOUT_MARKERS):
        return True
    if any(marker in combined for marker in _GATE_REMOTE_INFRA_MARKERS):
        return _PYTEST_REAL_FAILURE_RE.search(combined) is None
    return False


# USR-308: two more gate failures a reinvoked agent cannot (or must not) fix.
GATE_CAUSE_INFRA_NO_TESTS = "gate_infra_no_tests"
GATE_CAUSE_BASE_RED = "base_red_suspected"
# USR-354: remote slot whose worker stayed busy, with a local RAM that cannot run 2+ xdist workers.
GATE_CAUSE_REMOTE_UNAVAILABLE = "gate_remote_unavailable"
_GATE_REMOTE_REQUIRED_MARKER = "--remote-required:"
_HARNESS_RESULT_PREFIX = "[HARNESS_RESULT]"
_FAILED_FILE_RE = re.compile(r"^(?:FAILED|ERROR)[ \t]+(\S+?\.py)(?:::|\s|$)", re.MULTILINE)
_FAILED_LINE_RE = re.compile(r"^(?:FAILED|ERROR)[ \t]+\S+", re.MULTILINE)
# A diff touching any of these can change test behaviour without being imported by name.
_GLOBAL_TEST_CONFIG_NAMES = frozenset(
    {"conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "harness.config.json"}
)


def _parse_harness_result(output: str) -> Optional[dict[str, Any]]:
    """The `[HARNESS_RESULT] {json}` payload printed by the official gate, or None."""
    for line in reversed((output or "").splitlines()):
        stripped = line.strip()
        if stripped.startswith(_HARNESS_RESULT_PREFIX):
            try:
                payload = json.loads(stripped[len(_HARNESS_RESULT_PREFIX):].strip())
            except ValueError:
                return None
            return payload if isinstance(payload, dict) else None
    return None


def _gate_execution_worker(output: str) -> str | None:
    """Return the runner-reported host for the gate, if it ran on a remote worker."""
    payload = _parse_harness_result(output)
    executed_on = payload.get("executed_on") if payload is not None else None
    if not isinstance(executed_on, dict):
        return None
    host = executed_on.get("host")
    return host.strip()[:160] if isinstance(host, str) and host.strip() else None


def _gate_failure_is_infra_no_tests(output: str) -> bool:
    """True when the gate ran zero tests because a step was killed (e.g. SIGTERM, exit -15), USR-308.

    `discovered_count == 0` plus a negative exit code (signal death), or a tests step that exited 0
    without discovering anything, means the candidate was never exercised: not a code defect. A positive
    exit code (syntax/type error) with zero tests is a real failure and stays fixable by the agent.
    """
    payload = _parse_harness_result(output)
    if payload is None or payload.get("discovered_count") != 0:
        return False
    exit_codes = payload.get("exit_codes")
    if not isinstance(exit_codes, dict):
        return False
    codes = {k: v for k, v in exit_codes.items() if isinstance(v, int) and not isinstance(v, bool)}
    if any(code < 0 for code in codes.values()):
        return True
    return any("test" in name.lower() and code == 0 for name, code in codes.items())


def _gate_failure_is_base_red(output: str, diff_paths: list[str], cwd: Path) -> bool:
    """True when every failed test file is untouched by the candidate diff, USR-308 (cheap + conservative).

    Requires parseable `FAILED path::test` lines (all of them), no failed file in the diff, no diff file
    that is global test config, and no failed file referencing (import/attr/call) a module the diff
    touches. Anything undeterminable returns False so the current reinvoke behaviour stays.
    """
    if not diff_paths:
        return False
    lines = _FAILED_LINE_RE.findall(output or "")
    files = _FAILED_FILE_RE.findall(output or "")
    if not lines or len(files) != len(lines):
        return False
    failed_files = {f.replace("\\", "/") for f in files}
    diff_norm = [p.replace("\\", "/") for p in diff_paths]
    if failed_files & set(diff_norm):
        return False
    if any(Path(p).name in _GLOBAL_TEST_CONFIG_NAMES for p in diff_norm):
        return False
    stems = [Path(p).stem for p in diff_norm if p.endswith(".py") and Path(p).stem not in ("", "__init__")]
    pattern = None
    if stems:
        pattern = re.compile("(?:" + "|".join(
            rf"\bimport\s+{re.escape(s)}\b|\b{re.escape(s)}\s+import\b|\b{re.escape(s)}\.\w|\b{re.escape(s)}\("
            for s in dict.fromkeys(stems)
        ) + ")")
    for failed in failed_files:
        path = cwd / failed
        if not path.is_file():
            return False
        if pattern is not None:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
            if pattern.search(text):
                return False
    return True


def _focal_failure_is_timeout(focal_report: Any) -> bool:
    """True when the focal pre-check failure was a timeout, not a real test failure."""
    return focal_report.exit_code == _FOCAL_TIMEOUT_EXIT_CODE


def _focal_test_timeout_s(target_count: int) -> float:
    """Timeout budget for the focal pre-check given how many files it will actually run (USR-249).

    `FOCAL_TEST_TIMEOUT_S` alone was calibrated for the changed-files-only set; a sweep up to
    `FOCAL_SWEEP_MAX_EXTRA_FILES` extra files serialized onto one xdist worker needs more wall-clock
    than that flat floor, so the budget grows with `target_count` -- bounded by
    `FOCAL_TEST_TIMEOUT_CEILING_S` so even a maximal sweep stays far cheaper than `GATE_TIMEOUT_S`.
    """
    scaled = target_count * FOCAL_TEST_TIMEOUT_PER_TARGET_S
    return min(FOCAL_TEST_TIMEOUT_CEILING_S, max(FOCAL_TEST_TIMEOUT_S, scaled))


# Output of the official gate (`core/harness/runner.py`) when it refuses a dirty candidate worktree. It is
# a loop-infrastructure error (the previous fix-agent edit was never committed), never something another
# agent reinvocation can fix (USR-249).
_GATE_DIRTY_WORKTREE_MARKER = "Candidate worktree is dirty"


def _commit_fix_agent_changes(target_worktree: Path, ticket: UserTicket, stage: str) -> bool:
    """Commit whatever the fix agent left uncommitted in the ticket worktree; True if it changed anything.

    The official gate refuses a dirty candidate, and the fix prompt tells the agent not to commit, so the
    loop itself commits the adjustment (reusing `GitAutonomyManager.commit_ticket`) before the next
    focal/gate round (USR-249).
    """
    from core.git.autonomy import GitAutonomyManager

    from core.git.ticket_workspace import main_checkout_root
    try:
        git_root = main_checkout_root(target_worktree)
    except Exception:
        git_root = PROJECT_ROOT
    git_mgr = GitAutonomyManager(git_root)
    if not git_mgr.is_dirty(cwd=target_worktree):
        return False
    git_mgr.commit_ticket(
        ticket_id=ticket.id,
        title=ticket.title,
        cwd=target_worktree,
        custom_message=(
            f"fix(autocorrecao): ajuste do agente apos falha em {stage} [{ticket.id}]\n\n"
            f"DarkFac-Ticket: {ticket.id}\n"
            f"DarkFac-Autonomy: zero-human-touch\n"
        ),
    )
    return True


def _commit_fix_agent_changes_safely(
    target_worktree: Path, ticket: UserTicket, stage: str
) -> tuple[bool, Optional[str]]:
    """`_commit_fix_agent_changes` that turns a commit failure (secret scan, staged state...) into an
    error message instead of a traceback, so the loop can end with `fix_commit_failed` (USR-249)."""
    try:
        return _commit_fix_agent_changes(target_worktree, ticket, stage), None
    except Exception as exc:  # noqa: BLE001 - commit_ticket raises RuntimeError; any failure ends the loop cleanly
        logger.warning("Could not commit the fix agent's changes for %s: %s", ticket.id, exc)
        return False, str(exc)


def _render_self_correction_log(attempts_log: list[dict[str, Any]]) -> str:
    """Human-readable summary of every agent reinvocation and every timeout hit during the delivery
    gate loop, so a rescue session knows what happened without opening logs (USR-249)."""
    lines = ["[AUTOCORREÇÃO] Resumo de tentativas:"]
    for entry in attempts_log:
        if entry["event"] == "reinvocation":
            lines.append(
                f"  {entry['attempt']}. reinvocação do agente apos falha em '{entry['stage']}' (causa: {entry['cause']}), "
                f"levou {entry['duration_s']:.1f}s (limite {entry['limit_s']:.0f}s), "
                f"alterou arquivos: {'sim' if entry.get('changed') else 'nao'}"
            )
        elif entry["event"] == "infra_error":
            detail = f": {entry['error']}" if entry.get("error") else ""
            lines.append(
                f"  {entry['attempt']}. ERRO DE INFRAESTRUTURA ({entry['cause']}) em '{entry['stage']}'{detail} -- agente NAO reinvocado"
            )
        elif entry["event"] == "gate_remote_retry":
            lines.append(
                f"  {entry['attempt']}. portao repetido em outro host ({entry['cause']}, "
                f"retry {entry['retry']}, backoff {entry['backoff_s']:.0f}s)"
            )
        else:
            lines.append(
                f"  {entry['attempt']}. TIMEOUT ({entry['cause']}) em '{entry['stage']}' apos "
                f"{entry['duration_s']:.1f}s (limite {entry['limit_s']:.0f}s) -- agente NAO reinvocado"
            )
    return "\n".join(lines)


def _tail_lines(text: str | None, count: int = 20) -> str:
    """Last `count` lines of `text`, redacted of secrets (USR-257)."""
    lines = (text or "").splitlines()[-count:]
    return redacted_head(chr(10).join(lines), 4000) if lines else "(vazio)"


def _report_delivery_child_failure(
    proc: Any,
    completion_info: dict[str, Any],
    stdout_was_json: bool,
    ticket_id: str,
    worktree: Path,
    stream: Any = None,
) -> None:
    """Human-mode console report for a failed delivery child: the child ran with --json so its own
    human messages were suppressed; without this the console only shows the resume hint (USR-257)."""
    out = stream if stream is not None else sys.stderr
    exit_code = getattr(proc, "returncode", None)
    if stdout_was_json:
        parts = [f"causa: {completion_info.get('cause') or 'desconhecida'}"]
        parts.append(f"erro: {completion_info.get('error') or '(sem detalhe)'}")
        if completion_info.get("exit_code") is not None:
            parts.append(f"codigo de saida do filho: {completion_info['exit_code']}")
        print("[ERRO NA ENTREGA] " + "; ".join(parts), file=out)
        failed_tests = completion_info.get("failed_tests")
        if isinstance(failed_tests, list) and failed_tests:
            print("Testes reprovados: " + ", ".join(str(test_id) for test_id in failed_tests), file=out)
        gate_log_tail = completion_info.get("gate_log_tail")
        if isinstance(gate_log_tail, str) and gate_log_tail:
            print("Cauda do log do portao:", file=out)
            print(gate_log_tail, file=out)
        log = completion_info.get("self_correction_log") or completion_info.get("attempts_log")
        if log and isinstance(log, list):
            try:
                print(_render_self_correction_log(log), file=out)
            except Exception:
                print(f"[AUTOCORREÇÃO] {len(log)} tentativa(s) registrada(s)", file=out)
        resume = completion_info.get("resume_command")
    else:
        duration = getattr(proc, "duration_s", 0.0) or 0.0
        flags = []
        if getattr(proc, "timed_out", False):
            flags.append("timeout")
        if getattr(proc, "cancelled", False):
            flags.append("cancelado")
        extra = f" ({', '.join(flags)})" if flags else ""
        print(
            f"[ERRO NA ENTREGA] processo filho terminou sem JSON valido{extra}: "
            f"codigo de saida {exit_code}; duracao {duration:.1f}s",
            file=out,
        )
        print("--- ultimas linhas do stdout do filho ---", file=out)
        print(_tail_lines(getattr(proc, "stdout", "")), file=out)
        print("--- ultimas linhas do stderr do filho ---", file=out)
        print(_tail_lines(getattr(proc, "stderr", "")), file=out)
        resume = None
    if not resume:
        resume = f"python {PROJECT_ROOT / 'run_ticket.py'} --resume-delivery {ticket_id} --worktree {worktree}"
    print("[i] Comando para retomar entrega:", file=out)
    print(f"  {resume}", file=out)


def _gate_should_require_remote() -> bool:
    """USR-354: True when the gate is configured for remote workers but the local host cannot take
    2+ xdist workers; then a busy worker must requeue (``--remote-required``) instead of running a
    1-worker local gate that blows through GATE_TIMEOUT_S."""
    if not gate_ram.remote_workers_configured():
        return False
    try:
        return gate_ram.measure_workers().workers < gate_ram.MIN_GATE_WORKERS
    except Exception:
        return True


def _wait_gate_ram_if_local(as_json: bool) -> "gate_ram.RamWaitResult | None":
    """USR-262: only when the gate would run locally (no idle remote worker); None = nothing to wait for."""
    if gate_ram.remote_worker_available() or _gate_should_require_remote():
        return None
    return gate_ram.wait_for_gate_ram(announce=None if not as_json else (lambda _msg: None))


def _wait_gate_grokbox_if_needed(as_json: bool) -> None:
    """USR-310: if the grokbox was down at pre-flight, give it a window to come back
    online before the official gate runs. A no-op when it was already up; any probe
    failure here only logs -- the existing worker fallback still decides the gate.
    Skipped under pytest: same rationale as the preflight guard above -- this call
    never originates the "down" state itself, it only ever consumes one a real
    preflight wrote, so skipping it here is just belt-and-suspenders against stray
    state from a previous real run on the same checkout."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        grokbox_wake.wait_if_down(announce=None if as_json else (lambda msg: print(msg, file=sys.stderr)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Grokbox wake wait failed: %s", exc)


GATE_REMOTE_RETRIES = 2
GATE_REMOTE_BACKOFF_S = (15.0, 45.0)
_GATE_WORKER_URL_RE = re.compile(r"https?://[\w.\-]+:\d+")


def _gate_remote_unavailable(gate_cmd: list[str], output: str) -> bool:
    return "--remote-required" in gate_cmd and _GATE_REMOTE_REQUIRED_MARKER in output


def _rotate_gate_workers(failed_output: str) -> str | None:
    """USR-372: DARKFAC_TEST_WORKERS for the next attempt, without the host(s) that just failed.

    Returns the previous env value (or None) so the caller can restore it; the new value only
    ever excludes hosts named in the failure output, falling back to the default pool."""
    from core.harness import remote_dispatch as rd

    failed = set(_GATE_WORKER_URL_RE.findall(failed_output))
    current = os.environ.get(rd.ENV_TEST_WORKERS) or rd.DEFAULT_WORKERS
    pool = [u.strip() for u in current.split(",") if u.strip()]
    for u in rd.DEFAULT_WORKERS.split(","):
        if u not in pool:
            pool.append(u)
    kept = [u for u in pool if u not in failed] or pool
    previous = os.environ.get(rd.ENV_TEST_WORKERS)
    os.environ[rd.ENV_TEST_WORKERS] = ",".join(kept)
    return previous


def _retry_gate_remote_unavailable(
    gate_cmd: list[str], gate_res: Any, target_worktree: Path, attempts_log: list[dict[str, Any]],
    attempt: int, as_json: bool, cancelled: Any,
) -> Any:
    """USR-372: re-run the gate on another worker (bounded retries, backoff) while the cause is
    gate_remote_unavailable; returns the last gate result."""
    from core.harness import remote_dispatch as rd

    for retry in range(GATE_REMOTE_RETRIES):
        out = f"{gate_res.stdout or ''}\n{gate_res.stderr or ''}"
        if not _gate_remote_unavailable(gate_cmd, out) or cancelled():
            break
        delay = GATE_REMOTE_BACKOFF_S[min(retry, len(GATE_REMOTE_BACKOFF_S) - 1)]
        attempts_log.append({
            "event": "gate_remote_retry", "attempt": attempt + 1, "stage": "gate",
            "cause": GATE_CAUSE_REMOTE_UNAVAILABLE, "retry": retry + 1, "backoff_s": delay,
        })
        if not as_json:
            print(f"[WORKER REMOTO INDISPONIVEL] nova tentativa {retry + 1}/{GATE_REMOTE_RETRIES} "
                  f"em outro host apos {delay:.0f}s (USR-372).", file=sys.stderr)
        time.sleep(delay)
        previous = _rotate_gate_workers(out)
        try:
            gate_res = _run_bounded(gate_cmd, cwd=target_worktree, timeout_s=GATE_TIMEOUT_S)
        finally:
            if previous is None:
                os.environ.pop(rd.ENV_TEST_WORKERS, None)
            else:
                os.environ[rd.ENV_TEST_WORKERS] = previous
    return gate_res


def _gate_focal_step(
    st: ticket_stages.GateState,
    target_worktree: Path,
    ticket: UserTicket,
    as_json: bool,
    progress: ProgressPublisher,
    attempts_log: list[dict[str, Any]],
) -> str:
    """Focal pre-check of one attempt. STEP_PROCEED = focal passed (or no targets); else loop control."""
    attempt = st.attempt
    changed = _implementation_diff_paths(target_worktree)
    focal_targets = _focal_test_targets(target_worktree, changed)
    focal_report = _run_focal_tests(target_worktree, focal_targets) if focal_targets else None

    if focal_report is None or focal_report.success:
        return ticket_stages.STEP_PROCEED

    st.last_output = focal_report.agent_feedback
    st.last_exit_code = focal_report.exit_code or 1
    if not as_json:
        print(
            f"[ERRO NOS TESTES FOCAIS]: {focal_report.concise_summary}\n{focal_report.agent_feedback}",
            file=sys.stderr,
        )

    if _focal_failure_is_timeout(focal_report):
        focal_timeout_limit = _focal_test_timeout_s(len(focal_targets))
        st.last_cause = "focal_timeout"
        attempts_log.append({
            "event": "timeout",
            "attempt": attempt + 1,
            "stage": "focal",
            "cause": st.last_cause,
            "duration_s": round(focal_report.duration_seconds, 1),
            "limit_s": focal_timeout_limit,
        })
        if not as_json:
            print(
                f"[TIMEOUT NO PRE-TESTE FOCAL]: {focal_report.duration_seconds:.1f}s "
                f"(limite {focal_timeout_limit:.0f}s). Nao reinvocando o agente: timeout de "
                f"infraestrutura nao e algo que uma correcao de codigo resolva (USR-249).",
                file=sys.stderr,
            )
        return ticket_stages.STEP_BREAK

    st.last_cause = "focal_tests_failed"
    if attempt >= GATE_FIX_ATTEMPTS:
        return ticket_stages.STEP_BREAK
    progress.phase(
        "gate", "failed",
        f"testes focais reprovados (tentativa {attempt + 1}/{GATE_FIX_ATTEMPTS + 1}): "
        f"{focal_report.concise_summary}",
        cause="focal_tests_failed",
    )
    if not as_json:
        print(f"\n--> Reinvocando o agente na mesma worktree para corrigir ({', '.join(focal_targets)})...")
    fix_result = _invoke_fix_agent(
        target_worktree, ticket, "testes focais dos arquivos alterados", focal_report.agent_feedback
    )
    return _gate_after_fix(st, target_worktree, ticket, "focal", fix_result, attempts_log)


def _gate_after_fix(
    st: ticket_stages.GateState,
    target_worktree: Path,
    ticket: UserTicket,
    stage: str,
    fix_result: Any,
    attempts_log: list[dict[str, Any]],
) -> str:
    """Commits the fix agent's changes and logs the reinvocation; decides how the loop proceeds."""
    attempt = st.attempt
    fix_changed, commit_error = _commit_fix_agent_changes_safely(target_worktree, ticket, stage)
    if commit_error is not None:
        st.last_cause = "fix_commit_failed"
        st.last_output = commit_error
        attempts_log.append(ticket_stages.infra_error_entry(attempt, stage, st.last_cause, error=commit_error))
        return ticket_stages.STEP_BREAK
    attempts_log.append(
        ticket_stages.reinvocation_entry(attempt, stage, st.last_cause, fix_result, FIX_AGENT_TIMEOUT_S, fix_changed)
    )
    if _cancelled():
        return ticket_stages.STEP_CANCEL
    if not fix_changed:
        st.last_cause = "fix_agent_no_changes"
        return ticket_stages.STEP_BREAK
    return ticket_stages.STEP_CONTINUE


def _gate_build_command(target_worktree: Path) -> list[str]:
    gate_cmd = [sys.executable, str(target_worktree / "core" / "harness" / "runner.py"), "--quick"]
    if line_lock.is_fleet_mode() and os.environ.get("DARKFAC_FLEET_GATE_REMOTE") == "1":
        # USR-366: o despachante da frota fixa o worker remoto do host (DARKFAC_TEST_WORKERS).
        gate_cmd.append("--remote-required")
    elif line_lock.is_fleet_mode():
        gate_cmd.append("--local")
    elif _gate_should_require_remote():
        gate_cmd.append("--remote-required")
    return gate_cmd


def _gate_run_official(
    st: ticket_stages.GateState,
    target_worktree: Path,
    progress: ProgressPublisher,
    attempts_log: list[dict[str, Any]],
    as_json: bool,
    resume_cmd: str,
) -> tuple[str, Any, Any]:
    """Waits for grokbox/RAM, then runs the official gate. Returns (step, gate_cmd, gate_res)."""
    attempt = st.attempt
    # USR-310: grokbox estava fora no pre-flight -- da uma janela para ele acordar antes do portao.
    _wait_gate_grokbox_if_needed(as_json)

    # USR-262: gate local com 1 worker passa de GATE_TIMEOUT; espera RAM para 2+ workers ou falha explicito.
    ram_wait = _wait_gate_ram_if_local(as_json)
    if ram_wait is not None and not ram_wait.ok:
        st.last_cause = gate_ram.CAUSE_INSUFFICIENT_RAM
        st.last_exit_code = 1
        st.last_output = (
            f"{st.last_cause}: RAM livre {ram_wait.reading.available_gib:.1f} GiB, "
            f"{ram_wait.reading.workers} workers possiveis (minimo {gate_ram.MIN_GATE_WORKERS}) "
            f"apos {ram_wait.waited_s:.0f} s de espera; portao NAO executado. Retome com: {resume_cmd}"
        )
        attempts_log.append(ticket_stages.infra_error_entry(attempt, "gate", st.last_cause, error=st.last_output))
        if not as_json:
            print(f"[RAM INSUFICIENTE PARA O PORTAO]: {st.last_output}", file=sys.stderr)
        return ticket_stages.STEP_BREAK, None, None

    progress.phase("gate", "running", "portao oficial (core/harness/runner.py --quick)")
    if not as_json:
        print("\n--> Executando portão oficial da fábrica (python core/harness/runner.py --quick)...")
    # Bounded runner: a cancellation (USR-166) kills the gate's whole process tree.
    gate_cmd = _gate_build_command(target_worktree)
    gate_res = _run_bounded(
        gate_cmd,
        cwd=target_worktree,
        timeout_s=GATE_TIMEOUT_S,
    )
    if getattr(gate_res, "cancelled", False) is True or _cancelled():
        return ticket_stages.STEP_CANCEL, gate_cmd, gate_res
    gate_res = _retry_gate_remote_unavailable(
        gate_cmd, gate_res, target_worktree, attempts_log, attempt, as_json, _cancelled
    )
    if getattr(gate_res, "cancelled", False) is True or _cancelled():
        return ticket_stages.STEP_CANCEL, gate_cmd, gate_res
    return ticket_stages.STEP_PROCEED, gate_cmd, gate_res


def _gate_classify_failure(
    st: ticket_stages.GateState,
    gate_cmd: list[str],
    gate_res: Any,
    target_worktree: Path,
    attempts_log: list[dict[str, Any]],
    as_json: bool,
    resume_cmd: str,
) -> bool:
    """Records the failed gate output and detects infra failures. True = stop the loop (no reinvocation)."""
    attempt = st.attempt
    st.last_output = f"{gate_res.stdout}\n{gate_res.stderr}"
    st.last_exit_code = gate_res.returncode
    # Keep the latest official gate diagnostics in the final payload, including when the
    # in-place fixer makes no changes or exits with an infrastructure error.
    st.failed_tests = failed_test_ids(st.last_output)
    st.gate_log_tail = _tail_lines(st.last_output, count=40)
    if not as_json:
        print(f"[ERRO NO PORTÃO OFICIAL]:\n{gate_res.stdout}\n{gate_res.stderr}", file=sys.stderr)

    def _infra(cause: str, message: str) -> bool:
        st.last_cause = cause
        attempts_log.append(ticket_stages.infra_error_entry(attempt, "gate", cause))
        if not as_json:
            print(message, file=sys.stderr)
        return True

    if "--remote-required" in gate_cmd and _GATE_REMOTE_REQUIRED_MARKER in st.last_output:
        return _infra(
            GATE_CAUSE_REMOTE_UNAVAILABLE,
            "[WORKER REMOTO INDISPONIVEL]: RAM local nao comporta 2+ workers; portao local "
            f"evitado. Reenfileire/retome com: {resume_cmd} (USR-354).",
        )
    if _GATE_DIRTY_WORKTREE_MARKER in st.last_output:
        return _infra(
            "gate_dirty_worktree",
            "[WORKTREE SUJA NO PORTÃO OFICIAL]: o portao recusou o candidato sujo. Nao reinvocando "
            "o agente: e erro de infraestrutura do laco de autocorrecao (USR-249).",
        )
    if _gate_failure_is_timeout(gate_res):
        st.last_cause = "gate_timeout"
        attempts_log.append({
            "event": "timeout",
            "attempt": attempt + 1,
            "stage": "gate",
            "cause": st.last_cause,
            "duration_s": round(gate_res.duration_s, 1),
            "limit_s": GATE_TIMEOUT_S,
        })
        if not as_json:
            print(
                f"[TIMEOUT/INFRA NO PORTÃO OFICIAL]: {gate_res.duration_s:.1f}s "
                f"(limite {GATE_TIMEOUT_S:.0f}s). Nao reinvocando o agente: timeout ou falha de "
                f"infraestrutura nao e algo que uma correcao de codigo resolva (USR-249).",
                file=sys.stderr,
            )
        return True
    if _gate_failure_is_infra_no_tests(st.last_output):
        return _infra(
            GATE_CAUSE_INFRA_NO_TESTS,
            "[INFRA NO PORTÃO OFICIAL]: zero testes descobertos e um passo morto por sinal "
            "(exit code negativo, ex.: SIGTERM). Nao reinvocando o agente: o candidato nao foi "
            "exercitado; e falha de infraestrutura (USR-308).",
        )
    if _gate_failure_is_base_red(st.last_output, _implementation_diff_paths(target_worktree), target_worktree):
        return _infra(
            GATE_CAUSE_BASE_RED,
            "[BASE VERMELHA SUSPEITA]: todos os testes reprovados estao em arquivos que o diff do "
            "ticket nao toca nem referencia (base_red_suspected). Nao reinvocando o agente: "
            "corrija a main e retome a entrega com o comando abaixo (USR-308).",
        )
    return False


def _gate_reinvoke_for_gate(
    st: ticket_stages.GateState,
    gate_res: Any,
    target_worktree: Path,
    ticket: UserTicket,
    progress: ProgressPublisher,
    attempts_log: list[dict[str, Any]],
    as_json: bool,
) -> str:
    st.last_cause = "gate_failed"
    if st.attempt >= GATE_FIX_ATTEMPTS:
        return ticket_stages.STEP_BREAK
    attempt = st.attempt
    progress.phase(
        "gate", "failed",
        f"portao oficial falhou (tentativa {attempt + 1}/{GATE_FIX_ATTEMPTS + 1}, codigo {gate_res.returncode}): "
        f"{redacted_head((gate_res.stdout or gate_res.stderr or '')[-600:], 300)}",
        cause="gate_failed",
        worker=st.last_gate_worker,
    )
    if not as_json:
        print("\n--> Reinvocando o agente na mesma worktree com o log destilado do portao...")
    fix_result = _invoke_fix_agent(
        target_worktree, ticket, "portao oficial (python core/harness/runner.py --quick)", distill_log(st.last_output)
    )
    return _gate_after_fix(st, target_worktree, ticket, "gate", fix_result, attempts_log)


def _gate_official_step(
    st: ticket_stages.GateState,
    target_worktree: Path,
    ticket: UserTicket,
    resume_cmd: str,
    skip_validation: bool,
    as_json: bool,
    progress: ProgressPublisher,
    attempts_log: list[dict[str, Any]],
) -> str:
    """Official gate of one attempt: STEP_PASS when approved/skipped, else loop control."""
    if skip_validation:
        progress.phase("gate", "skipped", "validacao pulada (--skip-validation)")
        return ticket_stages.STEP_PASS

    step, gate_cmd, gate_res = _gate_run_official(st, target_worktree, progress, attempts_log, as_json, resume_cmd)
    if step != ticket_stages.STEP_PROCEED:
        return step

    gate_output = f"{gate_res.stdout or ''}\n{gate_res.stderr or ''}"
    st.last_gate_worker = _gate_execution_worker(gate_output)

    if gate_res.returncode == 0:
        progress.phase("gate", "succeeded", "portao oficial aprovado", worker=st.last_gate_worker)
        if not as_json:
            print("[+] Portão oficial aprovado com sucesso: [HARNESS_PASS]!")
        return ticket_stages.STEP_PASS

    if _gate_classify_failure(st, gate_cmd, gate_res, target_worktree, attempts_log, as_json, resume_cmd):
        return ticket_stages.STEP_BREAK
    return _gate_reinvoke_for_gate(st, gate_res, target_worktree, ticket, progress, attempts_log, as_json)


def _gate_exhausted_result(
    st: ticket_stages.GateState,
    target_worktree: Path,
    current_branch: str,
    current_sha: str,
    ticket: UserTicket,
    resume_cmd: str,
    as_json: bool,
    return_result: bool,
    progress: ProgressPublisher,
    attempts_log: list[dict[str, Any]],
) -> Any:
    print(f"[i] Worktree preservada para diagnóstico: {target_worktree}", file=sys.stderr)
    print(f"[i] Comando para retomar entrega após correção:\n  {resume_cmd}", file=sys.stderr)
    if attempts_log and not as_json:
        print(_render_self_correction_log(attempts_log), file=sys.stderr)
    attempts_tried = st.attempt + 1
    progress.phase(
        "gate", "failed",
        f"{st.last_cause} apos {attempts_tried} tentativa(s): {redacted_head(st.last_output[-600:], 300)}",
        cause=st.last_cause,
        worker=st.last_gate_worker,
    )
    gate_payload = {
        "exit_code": st.last_exit_code,
        "ok": False,
        "cause": st.last_cause,
        "ticket_id": ticket.id,
        "branch": current_branch,
        "current_sha": current_sha,
        "worktree": str(target_worktree),
        "resume_command": resume_cmd,
        "self_correction_log": attempts_log,
        "failed_tests": st.failed_tests,
        "gate_log_tail": st.gate_log_tail,
    }
    if as_json and not return_result:
        print(json.dumps(gate_payload, indent=2, ensure_ascii=False))
    return gate_payload if return_result else gate_payload["exit_code"]


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
    attempts_log: list[dict[str, Any]],
) -> Any:
    """Focal tests + the official gate, self-correcting up to GATE_FIX_ATTEMPTS times (USR-245).

    A headless agent that ends its turn deferring verification to a background task ("vou esperar o
    pytest em segundo plano") never gets to see the gate fail before the ticket is handed back to the
    operator (the USR-201 rescue). The focal pre-check runs regardless of `--skip-validation` (it is
    the new, fast, mandatory check); `--skip-validation` only still skips the expensive official gate.

    A timeout or infra hiccup (`_gate_failure_is_timeout`/`_focal_failure_is_timeout`, USR-249) never
    reinvokes the agent: nothing in the diff caused it, so retrying just burns another
    ``FOCAL_TEST_TIMEOUT_S``/``GATE_TIMEOUT_S`` for nothing (the USR-239 incident: ~28 extra minutes
    across 2 blind reinvocations after a 3000s gate timeout). `attempts_log` is appended in place with
    one entry per reinvocation and per timeout (cause, duration, limit applied) so the final report can
    list them without opening logs.

    Returns `None` on success (the caller proceeds to commit/push), or the usual failure payload/exit
    code once every attempt is exhausted or a timeout/infra failure is hit.
    """
    if ticket.project_id != "darkfac":
        # Client delivery always fails closed without its declared project gate (skip_validation ignored).
        return ticket_stages.external_project_gate(
            ticket, target_worktree, skip_validation, as_json, progress, GATE_TIMEOUT_S
        )[1]

    st = ticket_stages.GateState()
    for attempt in range(GATE_FIX_ATTEMPTS + 1):
        st.attempt = attempt
        if _cancelled():
            return cancelled_payload()

        step = _gate_focal_step(st, target_worktree, ticket, as_json, progress, attempts_log)
        if step == ticket_stages.STEP_CANCEL:
            return cancelled_payload()
        if step == ticket_stages.STEP_BREAK:
            break
        if step == ticket_stages.STEP_CONTINUE:
            continue

        if _cancelled():
            return cancelled_payload()

        step = _gate_official_step(
            st, target_worktree, ticket, resume_cmd, skip_validation, as_json, progress, attempts_log
        )
        if step == ticket_stages.STEP_PASS:
            return None
        if step == ticket_stages.STEP_CANCEL:
            return cancelled_payload()
        if step == ticket_stages.STEP_BREAK:
            break

    return _gate_exhausted_result(
        st, target_worktree, current_branch, current_sha, ticket, resume_cmd, as_json, return_result,
        progress, attempts_log,
    )


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


def _ticket_from_origin_main(ticket_id: str, worktree: Path) -> Optional[UserTicket]:
    """USR-314: look the ticket up in the ledger of ``origin/main`` (registered after the worktree was cut).

    The later rebase onto ``origin/main`` brings the ledger entry into the worktree, so finding it here
    is enough to proceed. Returns None when the fetch/show fails or the ticket is absent.
    """
    import tempfile

    git = ["git", "-C", str(worktree)]
    try:
        fetched = subprocess.run(git + ["fetch", "origin", "-q"], capture_output=True, timeout=120, check=False)
        if fetched.returncode != 0:
            logger.warning("Fetch de origin falhou (offline?); usando origin/main local para %s", ticket_id)
        shown = subprocess.run(
            git + ["show", "origin/main:.factory/demands/demands.json"],
            capture_output=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Lookup do ticket %s em origin/main falhou: %s", ticket_id, exc)
        return None
    if shown.returncode != 0:
        return None
    with tempfile.TemporaryDirectory(prefix="darkfac-origin-ledger-") as tmp:
        ledger = Path(tmp) / "demands.json"
        ledger.write_bytes(shown.stdout)
        return DemandsStore(ledger).get_ticket(ticket_id)


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

    from core.git.ticket_workspace import main_checkout_root
    try:
        git_root = main_checkout_root(target_worktree)
    except Exception:
        git_root = PROJECT_ROOT
    git_mgr = GitAutonomyManager(git_root)
    store = DemandsStore(target_worktree / ".factory" / "demands" / "demands.json")
    ticket = store.get_ticket(ticket_id)
    if ticket is None:
        shared_store = DemandsStore(PROJECT_ROOT / ".factory" / "demands" / "demands.json")
        ticket = shared_store.get_ticket(ticket_id)
    if ticket is None:
        ticket = _ticket_from_origin_main(ticket_id, target_worktree)
    if ticket is None:
        print(
            f"[ERRO] Ticket {ticket_id} não encontrado no ledger de demandas. Locais procurados: "
            f"(1) ledger da worktree ({target_worktree / '.factory' / 'demands' / 'demands.json'}); "
            f"(2) checkout compartilhado ({PROJECT_ROOT / '.factory' / 'demands' / 'demands.json'}); "
            f"(3) origin/main (.factory/demands/demands.json, após git fetch).",
            file=sys.stderr,
        )
        if return_result:
            return {"exit_code": 1, "ok": False, "error": f"Ticket {ticket_id} not found in ledger"}
        return 1

    session_probe_path = os.environ.get("DARKFAC_CODEX_SESSION_PATH", "").strip()
    if session_probe_path:  # USR-79: aviso somente leitura de chamada de ferramenta orfa
        from core.line.codex_session_probe import warn_if_unanswered

        warn_if_unanswered(session_probe_path, logger)

    # A resume typed by an operator (no launcher in this process, no parent launcher): the only case in
    # which a worktree cut before USR-375 S4 (no stage artifacts at all) gets its plan/build backfilled.
    standalone = not holder and not os.environ.get(local_cancel.ENV_PARENT)
    if holder:
        progress = holder[0]
    else:
        progress = open_progress(ticket.id, ticket.project_id, ticket.title)
        holder.append(progress)
        execution_registry.attach_progress(progress, ticket.id, title=ticket.title)  # USR-208
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
            standalone=standalone,
        )
    except cancellation.RunCancelledError:
        return cancelled_payload()


def _deliver_sync(
    git_mgr: Any,
    ticket: UserTicket,
    target_worktree: Path,
    current_branch: str,
    resume_cmd: str,
    no_commit: bool,
    as_json: bool,
    return_result: bool,
    progress: ProgressPublisher,
    cancelled_payload: Any,
    external_project: bool,
    base_branch: str,
) -> tuple[bool, Any]:
    """Commit pending changes and rebase on the base branch (steps 1-2 of the delivery).

    Returns (True, result) when the delivery must stop here, else (False, current_sha).
    """
    progress.phase("gate", "running", f"sincronizando com origin/{base_branch} antes do portao")
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
        return True, cancelled_payload()
    if not as_json:
        print(f"--> Sincronizando e rebaseando branch em origin/{base_branch}...")
    if external_project:
        ok, conflict_files, msg = rebase_on_project_base(target_worktree, base_branch)
    else:
        ok, conflict_files, msg = rebase_on_origin_main(target_worktree)
    if not ok:
        return True, ticket_stages.rebase_conflict_result(
            ticket, target_worktree, current_branch, current_sha, resume_cmd, base_branch,
            conflict_files, msg, progress, as_json, return_result,
        )

    current_sha = git_mgr.get_current_sha(target_worktree)

    owner_action_changes = (
        repair_owner_action_duplicates_before_gate(git_mgr, ticket.id, target_worktree)
        if not external_project else []
    )
    if owner_action_changes:
        if not as_json:
            print(f"[+] OA renumerada apos sync com origin/main: {'; '.join(owner_action_changes)}")
        current_sha = git_mgr.get_current_sha(target_worktree)
    return False, current_sha


def _deliver_external_project(
    git_mgr: Any,
    ticket: UserTicket,
    target_worktree: Path,
    no_push: bool,
    progress: ProgressPublisher,
    project: Any,
) -> Any:
    """Branch delivery (PR/merge/deploy/smoke) of a ticket from an external project."""
    if git_mgr.is_dirty(cwd=target_worktree):
        git_mgr.commit_ticket(
            ticket_id=ticket.id, title=ticket.title, cwd=target_worktree,
            custom_message=f"feat: {ticket.title} [{ticket.id}]",
        )
    if no_push:
        return None
    delivery = git_mgr.deliver_branch(
        ticket.id, ticket.title, cwd=target_worktree, base=project.default_branch,
        merge=not project.requires_commercial_acceptance, kind="implementation",
        project_id=ticket.project_id,
    )
    if delivery.ok and delivery.merge_sha and project.deploy is not None:
        progress.phase("deploy", "running", f"deploy pós-merge do projeto {project.id}")
        release_error = _release_external_project(
            project, ticket, delivery, progress, git_mgr.root
        )
        if release_error:
            progress.phase("deploy", "failed", release_error, cause="project_release_failed")
            delivery.ok = False
            delivery.action = "error"
            delivery.message = release_error
        else:
            progress.phase("deploy", "succeeded", f"deploy e smoke do projeto {project.id} aprovados")
    elif delivery.ok and delivery.merge_sha and project.smoke:
        smoke_error = _smoke_external_project(project)
        if smoke_error:
            progress.phase("deploy", "failed", smoke_error, cause="project_smoke_failed")
            delivery.ok = False
            delivery.action = "error"
            delivery.message = smoke_error
    return delivery


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
    standalone: bool = False,
) -> Any:
    """VALIDATE, REVIEW, MERGE and DEPLOY_SMOKE of one worktree (USR-375 S4).

    Each stage writes its artifact and `require_artifact` validates it before the next stage; the
    cancel request is checked before every step.
    """
    rws = ticket_engine.run_workspace(ticket, current_branch)

    def refused(stage: Stage, reason: str) -> Any:
        return _delivery_stage_refused(
            ticket, stage, reason, target_worktree, current_branch, resume_cmd, progress, as_json, return_result
        )

    try:
        _require_plan_and_build(ticket, rws, standalone)
    except StageArtifactMissing as exc:
        return refused(exc.stage, exc.reason)
    ticket_engine.clear_delivery(rws)

    # 1. Commit uncommitted changes in worktree if any (required before rebase)
    if _cancelled():
        return cancelled_payload()
    external_project = ticket.project_id != "darkfac"
    project = None
    if external_project:
        from core.projects.registry import get_project_registry
        project = get_project_registry().get_project(ticket.project_id)
    base_branch = project.default_branch if project is not None else "main"
    finished, synced = _deliver_sync(
        git_mgr, ticket, target_worktree, current_branch, resume_cmd, no_commit, as_json,
        return_result, progress, cancelled_payload, external_project, base_branch,
    )
    if finished:
        return synced
    current_sha = synced

    # 3. Focal tests + Official Validation Gate, with in-place self-correction (USR-245)
    if _cancelled():
        return cancelled_payload()
    attempts_log: list[dict[str, Any]] = []
    gate_outcome = _run_gate_with_self_correction(
        target_worktree, current_branch, current_sha, ticket, resume_cmd,
        skip_validation, as_json, return_result, progress, cancelled_payload,
        attempts_log,
    )
    if gate_outcome is not None:
        return gate_outcome
    if attempts_log and not as_json:
        print(_render_self_correction_log(attempts_log))
    ticket_engine.write_validation(
        rws, True, "skip_validation" if skip_validation else "core/harness/runner.py --quick",
        f"{len(attempts_log)} autocorrecao(oes)",
    )

    # REVIEW by another model family than the implementer; a change request goes back to the agent.
    if _cancelled():
        return cancelled_payload()
    review_outcome = _stage_review(
        ticket, target_worktree, current_branch, current_sha, resume_cmd, skip_validation, as_json,
        return_result, progress, cancelled_payload, rws,
    )
    if review_outcome is not None:
        return review_outcome
    try:
        require_artifact(Stage.VALIDATE, rws)
        require_artifact(Stage.REVIEW, rws)
    except StageArtifactMissing as exc:
        return refused(exc.stage, exc.reason)

    # 4. Autonomous Git Lifecycle Completion (PR, merge, cleanup)
    if _cancelled():
        return cancelled_payload()
    completion_report = None
    if external_project and project is not None and not no_commit:
        completion_report = _deliver_external_project(
            git_mgr, ticket, target_worktree, no_push, progress, project
        )
    elif not no_commit:
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
        return ticket_stages.delivery_failure_result(
            ticket, completion_report, external_project, target_worktree, current_branch, current_sha,
            resume_cmd, progress, as_json, return_result,
        )

    awaiting_deploy = False
    if completion_report is not None and not no_push:
        pr_url, merge_sha = ticket_engine.merge_receipt_from(completion_report, current_branch, current_sha)
        try:
            ticket_engine.write_merge(rws, pr_url, merge_sha)
            require_artifact(Stage.MERGE, rws)
            _stage_deploy_smoke(rws, ticket, project, merge_sha, external_project, progress)
            awaiting_deploy = bool(getattr(require_artifact(Stage.DEPLOY_SMOKE, rws), "pending", False))
        except (StageArtifactMissing, ValueError) as exc:
            stage = exc.stage if isinstance(exc, StageArtifactMissing) else Stage.MERGE
            return refused(stage, getattr(exc, "reason", str(exc)))
    else:
        progress.phase("deploy", "skipped", "merge e deploy desligados por --no-commit/--no-push")
    try:
        git_mgr.sweep_stale(cwd=git_mgr.root)
    except Exception as exc:
        logger.warning("Sweep after delivery warning: %s", exc)

    return ticket_stages.delivery_success_result(
        ticket, completion_report, target_worktree, current_branch, current_sha, attempts_log,
        as_json, return_result, awaiting_deploy=awaiting_deploy,
    )


def _require_plan_and_build(ticket: UserTicket, rws: Any, standalone: bool) -> None:
    """Delivery never starts without PLAN and BUILD artifacts (raises `StageArtifactMissing`).

    Exception: an operator's standalone `--resume-delivery` of a worktree cut before USR-375 S4 (no
    artifact at all) records a minimal plan/build so the old worktree can still go through VALIDATE,
    REVIEW and MERGE; the launcher and the fleet always produce both stages themselves.
    """
    if standalone and not ticket_engine.has_any(rws):
        logger.warning("resume-delivery de %s sem artefatos de estagio: plano/build minimos registrados", ticket.id)
        ticket_engine.write_demand(rws, ticket)
        ticket_engine.write_minimal_plan(rws, ticket)
        ticket_engine.write_build(rws, None, [ticket.id])
    require_artifact(Stage.PLAN, rws)
    require_artifact(Stage.BUILD, rws)


def _delivery_stage_refused(
    ticket: UserTicket,
    stage: Stage,
    reason: str,
    target_worktree: Path,
    current_branch: str,
    resume_cmd: str,
    progress: ProgressPublisher,
    as_json: bool,
    return_result: bool,
) -> Any:
    """A delivery stage without its artifact: nothing further runs and the ticket is not delivered."""
    cause = f"stage_artifact_missing:{stage.value}"
    message = f"estagio {stage.value} sem artefato valido: {reason}"
    progress.phase("merge" if stage.value in ("MERGE", "DEPLOY_SMOKE") else "gate", "failed", message, cause=cause)
    progress.set_outcome(message, cause)
    print(f"[ERRO] Ticket {ticket.id}: {message}. Entrega interrompida.", file=sys.stderr)
    print(f"[i] Worktree preservada: {target_worktree}\n[i] Para retomar: {resume_cmd}", file=sys.stderr)
    payload = {
        "exit_code": 1, "ok": False, "cause": cause, "error": reason, "ticket_id": ticket.id,
        "branch": current_branch, "worktree": str(target_worktree), "resume_command": resume_cmd,
    }
    if as_json and not return_result:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    return payload if return_result else 1


def _delivery_route_wait(
    ticket: UserTicket,
    cause: str,
    target_worktree: Path,
    current_branch: str,
    resume_cmd: str,
    progress: ProgressPublisher,
    as_json: bool,
    return_result: bool,
) -> Any:
    """No other-family reviewer now: keep the worktree, requeue (fleet requeue code), no failure."""
    message = f"estagio REVIEW aguardando revisor de outra familia: {cause}"
    progress.phase("gate", "skipped", message, cause="route_wait")
    progress.set_outcome(message, "route_wait")
    print(f"[AGUARDANDO] Ticket {ticket.id}: {message} ({ROUTE_WAIT_MARKER}).", file=sys.stderr)
    print(f"[i] Worktree preservada: {target_worktree}\n[i] Para retomar: {resume_cmd}", file=sys.stderr)
    payload = {
        "exit_code": EXIT_ROUTE_WAIT, "ok": False, "cause": "route_wait", "error": cause, "ticket_id": ticket.id,
        "branch": current_branch, "worktree": str(target_worktree), "resume_command": resume_cmd,
    }
    if as_json and not return_result:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    return payload if return_result else EXIT_ROUTE_WAIT


def _review_candidate(ticket: UserTicket, target_worktree: Path, rws: Any) -> Any:
    """One `stage_review.ReviewStage` round over the ticket diff; writes `review_verdict.json` on approve."""
    return stage_review.ReviewStage().run(
        _resolve_project(ticket), ticket.id, ws=rws, repo_dir=target_worktree, publish=False
    )


def _latest_review_feedback(rws: Any) -> str:
    from core.line import workspace as line_workspace

    reviews = sorted(line_workspace.context_dir(rws).glob("review-*.md"))
    return reviews[-1].read_text(encoding="utf-8", errors="replace") if reviews else ""


def _stage_review(
    ticket: UserTicket,
    target_worktree: Path,
    current_branch: str,
    current_sha: str,
    resume_cmd: str,
    skip_validation: bool,
    as_json: bool,
    return_result: bool,
    progress: ProgressPublisher,
    cancelled_payload: Any,
    rws: Any,
) -> Any:
    """REVIEW stage: None once the verdict is `approve`; otherwise the refusal/failure payload.

    `changes_required` reinvokes the development agent with the review, commits its fix and re-runs
    the gate before the next round (rounds are capped by `run_caps.review_rounds` in the stage).
    """
    cause = "review_not_attempted"
    for _attempt in range(REVIEW_ATTEMPTS):
        if _cancelled():
            return cancelled_payload()
        progress.phase("gate", "running", "estagio REVIEW: revisao por outra familia de modelo")
        if not as_json:
            print("--> Estagio REVIEW: revisao independente por outra familia de modelo...")
        try:
            result = _review_candidate(ticket, target_worktree, rws)
        except Exception as exc:  # noqa: BLE001 - a crashed review is a refused stage, never an approval
            logger.warning("Review of %s crashed: %s", ticket.id, exc)
            cause = f"review_error:{type(exc).__name__}: {exc}"
            break
        if result.outcome == "success":
            progress.phase("gate", "succeeded", "revisao independente aprovada")
            return None
        cause = f"review_{result.outcome}:{result.cause_code or ''}"
        if _is_route_wait(result):
            return _delivery_route_wait(
                ticket, cause, target_worktree, current_branch, resume_cmd, progress, as_json, return_result
            )
        if result.outcome != "retry" or "not_before" in (result.cause_code or ""):
            break
        if (result.cause_code or "").startswith("retry:development"):
            feedback = _latest_review_feedback(rws)
            _invoke_fix_agent(target_worktree, ticket, "revisao independente", feedback)
            _changed, commit_error = _commit_fix_agent_changes_safely(target_worktree, ticket, "revisao")
            if commit_error:
                cause = f"fix_commit_failed: {commit_error}"
                break
            gate_outcome = _run_gate_with_self_correction(
                target_worktree, current_branch, current_sha, ticket, resume_cmd,
                skip_validation, as_json, return_result, progress, cancelled_payload, [],
            )
            if gate_outcome is not None:
                return gate_outcome
    return _delivery_stage_refused(
        ticket, Stage.REVIEW, cause, target_worktree, current_branch, resume_cmd, progress, as_json, return_result
    )


def _stage_deploy_smoke(
    rws: Any, ticket: UserTicket, project: Any, merge_sha: str, external_project: bool, progress: ProgressPublisher
) -> None:
    """DEPLOY_SMOKE artifact. An external project's deploy/smoke already ran in the delivery (a failure
    never reaches here); the factory's own deploy is the fleet's single post-merge redeploy."""
    if external_project and project is not None:
        deployed = project.deploy is not None
        detail = "deploy e smoke do projeto aprovados" if deployed else "sem deploy declarado; smoke HTTP aprovado"
        ticket_engine.write_deploy(rws, merge_sha, True, deployed, detail)
        return
    # Not run here: smoke_ok None = deploy pending (merged-awaiting-deploy), never a claimed smoke.
    ticket_engine.write_deploy(
        rws, merge_sha, None, False,
        "deploy pos-merge pendente: unico pelo fleet_dispatcher/scripts/dokploy_redeploy.py (fora do run_ticket)",
    )
    progress.phase(
        "deploy", "skipped", "deploy pos-merge nao faz parte do run_ticket (scripts/dokploy_redeploy.py)"
    )


def _describe_workspace(workspace: TicketWorkspace) -> str:
    """One-line description for the live board; progress text must never raise into the launcher."""
    try:
        return f"{workspace.path.name} ({workspace.branch}@{workspace.base_sha[:12]})"
    except Exception:  # noqa: BLE001
        return "worktree criada"


def _release_external_project(
    project: Any, ticket: UserTicket, report: Any, progress: ProgressPublisher, project_root: Path
) -> Optional[str]:
    """Run the project's configured post-merge deploy/smoke flow, if one is declared."""
    if project.deploy is None or not getattr(report, "merge_sha", None):
        return None
    from core.line.stage_release import ReleaseStageHandler
    from core.workflow.control_contracts import Claim, JobKey, StageContext
    from datetime import datetime, timezone

    release_project = project.model_copy(update={"path": str(project_root)})
    handler = ReleaseStageHandler(release_project)
    run_id = progress.run_id or f"run-ticket-{ticket.id}"
    claim = Claim(
        job_key=JobKey(run_id=run_id, ticket_id=ticket.id, plan_version="run-ticket", stage="release", iteration=0),
        lease_id=f"run-ticket-{ticket.id}", owner="run_ticket", fencing_token=1,
        expires_at=datetime.now(timezone.utc).isoformat(),
    )
    context = StageContext(
        claim=claim, plan_ref=ticket.id, plan_digest=report.merge_sha, config_version="run-ticket",
        environment_ref="production", identity="run_ticket", route_ref="run_ticket", memory_version="0",
        input_refs=[report.pr_url or "", report.merge_sha],
    )
    result = handler.handle(context)
    if result.outcome != "success":
        return f"release do projeto {project.id} terminou em {result.outcome}: {result.cause_code or ''}"
    return None


def _smoke_external_project(project: Any) -> Optional[str]:
    """Run registered HTTP smoke checks for a project without a deployment adapter."""
    if project.deploy is not None or not project.smoke:
        return None
    from core.line.stage_release import _run_smoke_http

    failures: list[str] = []
    for check in project.smoke:
        ok, detail = _run_smoke_http(check, retries=1, delay_s=0, sleep=lambda _seconds: None)
        if not ok:
            failures.append(detail)
    return "\n".join(failures) if failures else None


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


def _needs_line_lock(args: argparse.Namespace) -> bool:
    """False for invocations that never touch pytest/the Desktop worker/the agent (USR-250).

    ``--dry-run`` only inspects quotas/routing, ``--create --queue-only`` only registers a ticket row
    (its own throwaway worktree, no agent, no gate), and no ticket at all just prints the quota report;
    none of these is the contention this guards against.
    """
    if args.dry_run:
        return False
    if args.create and args.queue_only:
        return False
    if not args.resume_delivery and not args.ticket_id and not args.create:
        return False
    return True


def _resume_delivery_target(args: argparse.Namespace) -> Optional[str]:
    if not args.resume_delivery:
        return None
    return (
        args.resume_delivery
        if isinstance(args.resume_delivery, str) and args.resume_delivery.strip()
        else args.ticket_id
    )


def _stage_line_lock(args: argparse.Namespace, stack: contextlib.ExitStack, resume_target: Optional[str]) -> Optional[int]:
    """Line lock (USR-250): one run_ticket at a time per shared checkout. Returns an exit code on refusal."""
    # 000. Line lock (USR-250): one run_ticket (this main flow, or a standalone --resume-delivery) at a
    # time per shared checkout. Acquired before anything else -- quota probe, ticket creation, worktree --
    # so a second launch is refused on the spot instead of discovering the collision after paying for it.
    # The delivery subprocess this same run later spawns for itself inherits the lock (ENV_INHERITED,
    # set on its environment below) instead of trying to acquire it a second time.
    if not _needs_line_lock(args):
        return None
    lock_label = resume_target or args.ticket_id or (f"novo:{args.title}" if args.create else "desconhecido")
    fleet_mode = getattr(args, "fleet", False) or line_lock.is_fleet_mode()
    host_id = getattr(args, "host_id", None) or line_lock.resolve_host_id()
    project_id = getattr(args, "project", "darkfac") or "darkfac"
    if fleet_mode:
        os.environ["DARKFAC_FLEET_MODE"] = "1"
    try:
        stack.enter_context(
            line_lock.guard(
                lock_label,
                fleet_mode=fleet_mode,
                host_id=host_id,
                project_id=project_id,
                # USR-330: a manual start reserves ticket/project in the canonical store (the same
                # unique-active-ticket reservation the line's admission takes) before any effect.
                require_reservation=bool(resume_target or args.ticket_id),
            )
        )
    except line_lock.LineLockHeld as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2
    except line_lock.ReservationConflictError as exc:
        print(f"\n[RESERVA DE FROTA] {exc}\n", file=sys.stderr)
        return 2
    return None


def _stage_check_harness(args: argparse.Namespace) -> Optional[int]:
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
    return None


def _stage_intake(args: argparse.Namespace) -> tuple[Optional[UserTicket], int]:
    """Ticket resolution or creation. Returns (ticket, 0) to continue, or (None, exit_code) to stop."""
    store = DemandsStore(PROJECT_ROOT / ".factory" / "demands" / "demands.json")
    if args.create:
        if not args.title:
            print("[ERRO] --title é obrigatório para criar um novo ticket.", file=sys.stderr)
            return None, 1
        if args.queue_only:
            queued = _queue_ticket_isolated(args)
            if queued is None:
                print("[ERRO] Não foi possível registrar o ticket na fila (control store indisponível).", file=sys.stderr)
                return None, 1
            print(f"[+] Ticket {queued.id} registrado na fila de desenvolvimento: '{queued.title}'")
            return None, 0
        # The row is NOT written to the shared checkout's ledger: it is added to the ticket's own
        # worktree ledger below and travels in the implementation commit (USR-69/USR-75).
        ticket = _build_ticket(args, store)
        if not args.json:
            print(f"\n[+] Novo ticket criado: {ticket.id} - '{ticket.title}'")
        return ticket, 0
    if args.ticket_id:
        ticket = store.get_ticket(args.ticket_id)
        if ticket is None:  # USR-335: checkout compartilhado atrasado; procurar no ledger de origin/main
            ticket = _ticket_from_origin_main(args.ticket_id, PROJECT_ROOT)
        if ticket is None:
            print(f"[ERRO] Ticket {args.ticket_id} não encontrado no backlog.", file=sys.stderr)
            return None, 1
        if not args.json:
            print(f"\n[+] Ticket carregado: {ticket.id} - '{ticket.title}'")
        return ticket, 0
    # No ticket specified; display quota report and exit
    if not args.json:
        print("\nNenhum ticket especificado. Use: python run_ticket.py <TICKET_ID> ou python run_ticket.py --create --title '...'")
    return None, 0


_DEV_CAPS = ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"]


def _stage_route(
    args: argparse.Namespace,
    ticket: UserTicket,
    quotas: dict[str, Any],
    override_granted: bool,
    progress: ProgressPublisher,
    operating_harness: Optional[str],
) -> Any:
    """Routing decision. Returns (harness, model, reason) or an int exit code."""
    caps = _DEV_CAPS
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
            rec_route = pick("development", caps, mode=DEVELOPMENT_MODE, project_id=ticket.project_id)
            if rec_route:
                print(f"--> Recomendação do Roteador: {rec_route[0].upper()}", file=sys.stderr)
            return 2

        if not args.json:
            if harness_info and harness_info["is_critical"] and override_granted:
                print(f"[AVISO] Override explícito do usuário ativo: executando em {target_harness} com quota crítica ou desconhecida.")
            else:
                print(f"[+] Harness explicitamente selecionado: {target_harness}")
        return target_harness, None, "explicit_harness"

    # Automatic router resolution via Dynamic Headroom, among harnesses that can write
    if operating_harness and not args.json:
        print(f"[+] Harness de operacao detectado: {operating_harness}")
    route = pick(
        "development", caps, mode=DEVELOPMENT_MODE, operating_harness=operating_harness,
        project_id=ticket.project_id,
    )
    if route is None:
        progress.phase("preflight", "failed", "nenhuma rota disponivel (cotas <= 15%)", cause="no_route")
        print(
            "\n[ERRO] Nenhuma rota disponível: todas as contas de assinatura estão <= 15% e OpenRouter sem saldo confirmado.",
            file=sys.stderr,
        )
        return 2
    selected_harness, selected_model = route
    preferred_operating = operating_harness is not None and selected_harness == operating_harness
    if not args.json:
        print(
            f"[+] Roteador selecionou automaticamente: {selected_harness.upper()} "
            f"(Modelo: {selected_model or 'default'}, modo: {DEVELOPMENT_MODE})"
            + (" (preferencia: harness de operacao)" if preferred_operating else "")
        )
    return selected_harness, selected_model, ("operating_harness_preferred" if preferred_operating else "headroom")


def _stage_workspace(
    args: argparse.Namespace, ticket: UserTicket, progress: ProgressPublisher
) -> tuple[Optional[TicketWorkspace], int]:
    """Own worktree (USR-69): the agent, the gate and the commit never touch the shared checkout."""
    from core.git.ticket_workspace import WorkspaceError

    progress.phase("workspace", "running", "criando worktree propria a partir de origin/main")
    try:
        workspace = _prepare_workspace(args, ticket)
    except SharedCheckoutDirty as exc:
        progress.phase("workspace", "failed", "checkout compartilhado sujo", cause="shared_checkout_dirty")
        print(f"[ERRO] {exc}", file=sys.stderr)
        return None, 3
    except (WorkspaceError, RuntimeError) as exc:
        progress.phase("workspace", "failed", str(exc), cause="workspace_error")
        print(f"[ERRO] Não foi possível criar a worktree do ticket {ticket.id}: {exc}", file=sys.stderr)
        return None, 1
    progress.phase("workspace", "succeeded", _describe_workspace(workspace))
    if not args.json:
        print(
            f"[+] Worktree própria: {workspace.path} "
            f"(branch {workspace.branch}, base {workspace.base_ref}@{workspace.base_sha[:12]})"
        )
    return workspace, 0


def _run_dev_agent(
    args: argparse.Namespace,
    ticket: UserTicket,
    workspace: TicketWorkspace,
    quotas: dict[str, Any],
    selected_harness: Optional[str],
    selected_model: Optional[str],
    progress: ProgressPublisher,
    extra_prompt: str = "",
) -> Any:
    """Runs the development agent with the line's retry policy; returns the retry report.

    `extra_prompt` scopes the run to one sub-ticket of a multi-ticket plan (USR-375 S4)."""
    dev_prompt = ticket_stages.build_dev_prompt(ticket, workspace) + extra_prompt

    def _build_request(harness: str, model: Optional[str]) -> AgentRequest:
        continuation = ticket_stages.continuation_prompt(workspace, LEDGER_RELATIVE)
        cli_model, cli_effort = harness_cli_pin(
            harness, model, stage="development", config=load_routing_config()
        )
        return AgentRequest(
            prompt=headless_development_preamble(
                harness, quotas.get(harness, {}).get("headroom")
            ) + dev_prompt + continuation,
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
    return agent_retry.run_with_retry(
        _build_request,
        (selected_harness, selected_model),
        host_caps=_DEV_CAPS,
        stage="development",
        mode=DEVELOPMENT_MODE,
        config=load_routing_config(),
        run_func=run_agent,
        pick_func=pick,
        sleep_fn=time.sleep,
        pinned=bool(args.harness),
        on_event=_report_progress,
    )


def _stage_agent_failure(
    args: argparse.Namespace,
    ticket: UserTicket,
    workspace: TicketWorkspace,
    report: Any,
    selected_harness: Optional[str],
    before_headroom: Any,
    ticket_size: str,
    progress: ProgressPublisher,
) -> int:
    failure = format_agent_failure(report)
    timed_out = (
        report.result is not None and report.result.error_kind == "timeout"
    )
    failure_cause = "agent_timeout" if timed_out else "agent_failed"
    if timed_out:
        # USR-256: no ledger field exists for a failure reason; the cause goes to the progress
        # board and the resume command to stderr (the ticket stays `planned`).
        failure += (
            f"\n[i] Causa: agent_timeout. Worktree preservada: {workspace.path}\n"
            f"[i] Para retomar: python C:\\dev\\DarkFac\\run_ticket.py --resume-delivery "
            f"{ticket.id} --worktree {workspace.path}"
        )
    progress.phase("agent", "failed", failure, cause=failure_cause)
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


def _stage_agent_early_exit(
    args: argparse.Namespace,
    ticket: UserTicket,
    workspace: TicketWorkspace,
    report: Any,
    selected_harness: Optional[str],
    before_headroom: Any,
    ticket_size: str,
    progress: ProgressPublisher,
) -> Optional[int]:
    """Cancellation, protected-path block and agent failure after the agent returned; None to proceed."""
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
    advance_after_timeout = (
        not report.ok and report.result is not None and report.advance_to_validation
    )
    if advance_after_timeout:
        # USR-256: timeout with work already in the worktree is not a failure; validate what exists.
        progress.phase(
            "agent", "running",
            "agente estourou o tempo com alteracoes na worktree; seguindo para validacao",
            cause="agent_timeout_with_changes",
        )
        print(
            "[i] Agente estourou o tempo com alterações na worktree; seguindo para validação",
            file=sys.stderr,
        )
    if (not report.ok or report.result is None) and not advance_after_timeout:
        return _stage_agent_failure(
            args, ticket, workspace, report, selected_harness, before_headroom, ticket_size, progress
        )
    return None


def _stage_agent_no_changes(
    args: argparse.Namespace,
    ticket: UserTicket,
    workspace: TicketWorkspace,
    agent_result: Any,
    selected_harness: Optional[str],
    ticket_size: str,
    cost_record: Any,
    progress: ProgressPublisher,
) -> int:
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


def _stage_deliver(
    args: argparse.Namespace,
    ticket: UserTicket,
    workspace: TicketWorkspace,
    progress: ProgressPublisher,
    exec_tracker: Any,
) -> tuple[int, dict[str, Any]]:
    """Delivery phase (4b, 5, 6) in a fresh, isolated Python process (USR-116), or in-process under tests.

    Spawning a fresh process ensures all modules (DemandsStore, UserTicket, GitAutonomyManager)
    are loaded from current disk state, preventing in-memory schema drift or ValueError
    caused by background commits to the shared checkout during long agent runs.
    """
    run_script = PROJECT_ROOT / "run_ticket.py"
    use_subprocess = run_script.is_file() and not os.environ.get("PYTEST_CURRENT_TEST")

    if not use_subprocess:
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
        return exit_code, (res if isinstance(res, dict) else {"ok": exit_code == 0})

    if not args.json:
        print("\n--> Disparando fase de entrega em processo novo isolado (USR-116)...")
    delivery_cmd = ticket_stages.delivery_command(run_script, ticket.id, workspace.path, args)
    child_env = ticket_stages.delivery_child_env(progress, exec_tracker)
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
    completion_info, stdout_was_json = ticket_stages.parse_delivery_output(delivery_proc, exit_code)
    if not args.json and not (exit_code == 0 and completion_info.get("ok", False)) \
            and exit_code != EXIT_CANCELLED and completion_info.get("cause") != "cancelled":
        _report_delivery_child_failure(
            delivery_proc, completion_info, stdout_was_json, ticket.id, workspace.path
        )
    return exit_code, completion_info


def _stage_preflight(
    args: argparse.Namespace,
    ticket: UserTicket,
    quotas: dict[str, Any],
    override_granted: bool,
    progress: ProgressPublisher,
) -> Any:
    """Grokbox probe, size estimation, routing and dry run. Returns a route dict, or an int exit code."""
    # 1a. Grokbox health probe + wake email (USR-310): a dead probe or a failed email
    # only logs -- it never blocks or changes this run's outcome. Skipped under pytest
    # (same guard as the delivery subprocess bypass below): the probe/email are already
    # offline-safe on their own, but leaving "down" state in the real .factory of the
    # checkout the suite runs from would make an unrelated test's gate wait for real.
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        try:
            grokbox_wake.preflight_check()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Grokbox preflight check failed: %s", exc)

    # 1b. Ticket size and quota risk estimation (USR-113)
    ticket_size, size_reasons = estimate_ticket_size(ticket)
    if ticket_size == "large" and not args.json:
        ticket_stages.print_large_ticket_warning(ticket, size_reasons)

    # 2. Routing Decision
    # The harness the owner operates the factory through is the preferred developer while its quota is
    # above the critical floor (USR-109); None when no signal exists (pump, worker, cloud).
    operating_harness = detect_operating_harness()
    routed = _stage_route(args, ticket, quotas, override_granted, progress, operating_harness)
    if isinstance(routed, int):
        return routed
    selected_harness, selected_model, route_reason = routed

    if args.dry_run:
        progress.discard()
        result_payload = ticket_stages.dry_run_payload(
            ticket, ticket_size, size_reasons, selected_harness, selected_model, DEVELOPMENT_MODE,
            override_granted, operating_harness, route_reason,
        )
        if args.json:
            print(json.dumps(result_payload, indent=2))
        else:
            print(
                f"\n[DRY RUN] Execução concluída sem alterações. "
                f"Rota validada: {selected_harness} (modo: {DEVELOPMENT_MODE})"
            )
        return 0
    return {
        "harness": selected_harness, "model": selected_model, "reason": route_reason,
        "ticket_size": ticket_size,
    }


def _resolve_project(ticket: UserTicket) -> Any:
    from core.projects.registry import get_project_registry

    project = get_project_registry().get_project(ticket.project_id)
    if project is None:
        raise RuntimeError(f"projeto {ticket.project_id} nao registrado")
    return project


def _plan_with_agent(ticket: UserTicket, workspace: TicketWorkspace, rws: Any) -> Optional[str]:
    """PLAN through `stage_planning.run_planning` (high-tier model only); None on success, else the cause."""
    cause = "plan_not_attempted"
    for _attempt in range(PLAN_ATTEMPTS):
        try:
            result = stage_planning.run_planning(
                _resolve_project(ticket), ticket.id, ws=rws, repo_dir=workspace.path, publish=False,
                host_caps=_DEV_HOST_CAPS, demand_text=ticket.problem_statement,
            )
        except Exception as exc:  # noqa: BLE001 - a crashed planner is a refused stage, not a traceback
            return f"plan_error:{type(exc).__name__}: {exc}"
        if result.outcome == "success":
            return None
        cause = f"plan_{result.outcome}:{result.cause_code or ''}"
        if _is_route_wait(result):
            return cause
        if result.outcome != "retry" or "not_before" in (result.cause_code or ""):
            break
    return cause


def _is_route_wait(result: Any) -> bool:
    """`RouteWaiter.no_route_result` outcome (retry with not_before, or parked) for "no eligible model"."""
    from core.line.route_wait import NO_ROUTE_CAUSE

    return NO_ROUTE_CAUSE in str(getattr(result, "cause_code", "") or "")


def _stage_wait(
    args: argparse.Namespace,
    ticket: UserTicket,
    workspace: Optional[TicketWorkspace],
    progress: ProgressPublisher,
    stage: Stage,
    cause: str,
) -> int:
    """No route for a stage right now: requeue later (ticket stays planned), never a failure."""
    message = f"estagio {stage.value} aguardando rota elegivel: {cause}"
    progress.phase("agent", "skipped", message, cause="route_wait")
    progress.set_outcome(message, "route_wait")
    print(f"[AGUARDANDO] {message}. Ticket segue planned e volta a fila depois ({ROUTE_WAIT_MARKER}).",
          file=sys.stderr)
    if workspace is not None:
        _report_workspace_fate(workspace, args.json)
    if args.json:
        print(json.dumps({
            "ok": False, "cause": "route_wait", "stage": stage.value, "error": cause, "ticket_id": ticket.id,
        }, indent=2, ensure_ascii=False))
    return EXIT_ROUTE_WAIT


def _stage_refused(
    args: argparse.Namespace,
    ticket: UserTicket,
    workspace: TicketWorkspace,
    progress: ProgressPublisher,
    stage: Stage,
    cause: str,
) -> int:
    """A launcher stage that did not leave its artifact stops the ticket before the next stage."""
    message = f"estagio {stage.value} nao produziu o artefato: {cause}"
    progress.phase("agent", "failed", message, cause=f"stage_{stage.value.lower()}_refused")
    progress.set_outcome(message, f"stage_{stage.value.lower()}_refused")
    print(f"[ERRO] {message}. O ticket NAO segue para o desenvolvimento nem para o merge.", file=sys.stderr)
    _report_workspace_fate(workspace, args.json)
    if args.json:
        print(json.dumps({
            "ok": False, "cause": f"stage_artifact_missing:{stage.value}", "error": cause,
            "ticket_id": ticket.id, "workspace": str(workspace.path),
        }, indent=2, ensure_ascii=False))
    return 1


def _stage_plan(
    args: argparse.Namespace,
    ticket: UserTicket,
    workspace: TicketWorkspace,
    route: dict[str, Any],
    progress: ProgressPublisher,
) -> Optional[int]:
    """INTAKE, GRILL (skipped for a small fix) and PLAN, each validated before the next; None to proceed."""
    rws = ticket_engine.run_workspace(ticket, workspace.branch, workspace.base_sha)
    ticket_engine.reset(rws)
    stage = Stage.INTAKE
    try:
        ticket_engine.write_demand(rws, ticket)
        require_artifact(Stage.INTAKE, rws)
        stage = Stage.PLAN
        if route.get("ticket_size") == "small":
            # A small fix skips only GRILL; it still leaves a (minimal) plan artifact.
            progress.phase("agent", "running", "estagio PLAN: plano minimo de correcao pequena (sem grill)")
            ticket_engine.write_minimal_plan(rws, ticket)
        else:
            stage = Stage.GRILL
            ticket_engine.write_grill(rws, ticket)
            require_artifact(Stage.GRILL, rws)
            stage = Stage.PLAN
            progress.phase("agent", "running", "estagio PLAN: planejamento por modelo de nivel alto")
            if not args.json:
                print("\n--> Estagio PLAN: planejamento (SPEC.md + tickets.json)...")
            cause = _plan_with_agent(ticket, workspace, rws)
            if cause is not None and "no_route" in cause:
                return _stage_wait(args, ticket, workspace, progress, Stage.PLAN, cause)
            if cause is not None:
                return _stage_refused(args, ticket, workspace, progress, Stage.PLAN, cause)
        planned = ticket_engine.plan_tickets(rws)
    except StageArtifactMissing as exc:
        return _stage_refused(args, ticket, workspace, progress, exc.stage, exc.reason)
    except (OSError, ValueError) as exc:
        return _stage_refused(args, ticket, workspace, progress, stage, f"{type(exc).__name__}: {exc}")
    if not args.json:
        print(f"[+] Plano validado: {len(planned)} sub-ticket(s).")
    return None


def _stage_build(
    args: argparse.Namespace,
    ticket: UserTicket,
    workspace: TicketWorkspace,
    quotas: dict[str, Any],
    route: dict[str, Any],
    progress: ProgressPublisher,
) -> Any:
    """Development agent run. Returns an int exit code, or a dict with the agent outcome."""
    selected_harness = route["harness"]
    selected_model = route["model"]
    ticket_size = route["ticket_size"]
    # 4. Execution Phase
    mcp_healthy, mcp_reasons = check_optional_mcp_servers()
    if not mcp_healthy and not args.json:
        print(f"[!] Integração opcional MCP indisponível ({'; '.join(mcp_reasons)}). Continuando em modo degradado.")
    if not args.json:
        print(f"\n--> Iniciando desenvolvimento via {selected_harness.upper()}...")

    before_headroom = quotas.get(selected_harness, {}).get("headroom")
    rws = ticket_engine.run_workspace(ticket, workspace.branch, workspace.base_sha)
    plan_items = ticket_engine.plan_tickets(rws)
    # A multi-ticket plan runs its sub-tickets sequentially inside this same ticket (USR-375 S4).
    sequential = len(plan_items) > 1
    tickets_done: list[str] = []
    total_duration = 0.0
    implementer = selected_harness
    for index, item in enumerate(plan_items, start=1):
        if sequential:
            if not args.json:
                print(f"--> Sub-ticket {index}/{len(plan_items)} do plano: {item.id} {item.title}")
            extra = ticket_engine.sub_ticket_prompt(item, index, len(plan_items))
            report = _run_dev_agent(
                args, ticket, workspace, quotas, selected_harness, selected_model, progress, extra_prompt=extra
            )
        else:
            report = _run_dev_agent(args, ticket, workspace, quotas, selected_harness, selected_model, progress)
        early = _stage_agent_early_exit(
            args, ticket, workspace, report, selected_harness, before_headroom, ticket_size, progress
        )
        if early is not None:
            return early
        total_duration += float(report.total_duration_s or 0.0)
        tickets_done.append(item.id)
        if report.route is not None:
            implementer = report.route[0]

    agent_result = report.result
    if report.route is not None:
        selected_harness, selected_model = report.route  # the harness that actually did the work
    if agent_result.model:
        selected_model = agent_result.model  # the model the CLI reported, not only the cascade alias

    quotas_after = inspect_quotas()
    after_headroom = quotas_after.get(selected_harness, {}).get("headroom")
    cost_record = record_ticket_quota_cost(
        ticket_id=ticket.id,
        harness=selected_harness,
        before_headroom=before_headroom,
        after_headroom=after_headroom,
        duration_s=total_duration if sequential else report.total_duration_s,
    )
    if not args.json:
        print(f"\n{format_ticket_quota_summary(cost_record)}")

    if _cancelled():  # cancelled right as the agent finished: do not even look at its output
        return _abort_cancelled(progress, ticket.id, args.json, workspace=workspace)

    from core.git.autonomy import GitAutonomyManager

    git_mgr = GitAutonomyManager(workspace.main_root)
    implementation_paths = [
        path for path in git_mgr.changed_paths(workspace.path) if path != LEDGER_RELATIVE
    ]
    if not implementation_paths:
        return _stage_agent_no_changes(
            args, ticket, workspace, agent_result, selected_harness, ticket_size, cost_record, progress
        )

    # BUILD artifact: the implementer family REVIEW must differ from (stage_review reads `harness`).
    ticket_engine.write_build(rws, implementer, tickets_done)
    require_artifact(Stage.BUILD, rws)
    agent_summary = redacted_head(agent_result.text, 800)
    progress.phase(
        "agent", "succeeded", f"{len(implementation_paths)} arquivo(s) alterado(s)", harness=selected_harness
    )
    if not args.json:
        print(f"[+] Desenvolvimento concluído pelo {selected_harness.upper()}.")
        print(f"[i] Resumo do agente: {agent_summary or '(vazio)'}")
    return {
        "agent_result": agent_result, "harness": selected_harness, "model": selected_model,
        "effort": agent_result.effort, "cost_record": cost_record, "agent_summary": agent_summary,
    }


def _run(argv: Optional[list[str]], box: list[ProgressPublisher], stack: contextlib.ExitStack) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # 00. Cancel mode (USR-166): only writes the control file the running launcher watches
    if args.cancel:
        return _request_cancel(args)

    resume_target = _resume_delivery_target(args)
    locked = _stage_line_lock(args, stack, resume_target)
    if locked is not None:
        return locked

    # 0. Resume delivery mode (USR-116): rebase, validate, and deliver an existing worktree in a fresh process
    if args.resume_delivery:
        target_ticket_id = resume_target
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

    refused = _stage_check_harness(args)
    if refused is not None:
        return refused

    quotas = inspect_quotas()
    if not args.json:
        print(format_quota_report(quotas))

    override_granted = check_explicit_override(args.prompt, args.force)

    # 1. Ticket resolution or creation
    ticket, intake_exit = _stage_intake(args)
    if ticket is None:
        return intake_exit

    # Live line board (USR-140): best-effort phase events. Held until the run is known not to be a dry
    # run; a publishing failure can never block or change the outcome of this launcher.
    progress = open_progress(ticket.id, ticket.project_id, ticket.title)
    progress.hold()
    box.append(progress)
    progress.phase("preflight", "running", "cota, tamanho do ticket e roteamento")

    route = _stage_preflight(args, ticket, quotas, override_granted, progress)
    if isinstance(route, int):
        return route

    progress.phase(
        "preflight",
        "succeeded",
        f"rota {route['harness']} (modelo {route['model'] or 'default'}, motivo {route['reason']})",
        harness=route["harness"],
    )
    progress.release()
    # USR-208: every ticket and phase is an execution with its own id in Hub Live (best effort, never raises).
    exec_tracker = execution_registry.attach_progress(
        progress, ticket.id, harness=route["harness"], model=route["model"], title=ticket.title
    )

    # Cancellation scope (USR-166): the same token/guards the line uses (USR-152), fed by the per-ticket
    # control file and termination signals. `_run_bounded` kills the agent's process tree when it fires.
    stack.enter_context(
        local_cancel.watch(
            ticket.id, progress.run_id, fresh=progress.owns_run, not_before=local_cancel.LAUNCHED_AT
        )
    )
    if _cancelled():
        return _abort_cancelled(progress, ticket.id, args.json)

    # 3. Own worktree (USR-69)
    workspace, ws_exit = _stage_workspace(args, ticket, progress)
    if workspace is None:
        return ws_exit
    if _cancelled():
        return _abort_cancelled(progress, ticket.id, args.json, workspace=workspace)

    # INTAKE -> GRILL -> PLAN (USR-375 S4): BUILD never starts without a validated plan artifact.
    plan_refused = _stage_plan(args, ticket, workspace, route, progress)
    if plan_refused is not None:
        return plan_refused
    if _cancelled():
        return _abort_cancelled(progress, ticket.id, args.json, workspace=workspace)

    built = _stage_build(args, ticket, workspace, quotas, route, progress)
    if isinstance(built, int):
        return built

    exit_code, completion_info = _stage_deliver(args, ticket, workspace, progress, exec_tracker)

    delivery_done = exit_code == 0 and completion_info.get("ok", False)
    if (
        exit_code == EXIT_CANCELLED
        or completion_info.get("cause") == "cancelled"
        or (_cancelled() and not delivery_done)  # e.g. the delivery process was killed with this launcher
    ):
        return _abort_cancelled(progress, ticket.id, args.json, workspace=workspace)

    delivered = exit_code == 0 and completion_info.get("ok", False)
    if delivered and completion_info.get("status") == "merged_awaiting_deploy":
        progress.set_outcome("mergeado, aguardando deploy pos-merge", "merged_awaiting_deploy")
    if exit_code == EXIT_ROUTE_WAIT:
        pass  # requeue: the outcome was already recorded by the waiting stage
    elif not delivered:
        failure_text = completion_info.get("error") or completion_info.get("cause") or f"codigo {exit_code}"
        progress.set_outcome(f"entrega falhou: {failure_text}", str(completion_info.get("cause") or "delivery_failed"))

    if args.json:
        payload = ticket_stages.final_json_payload(
            ticket, route["ticket_size"], built["cost_record"], built["harness"], built["model"],
            built["effort"], built["agent_result"], workspace, built["agent_summary"], delivered,
            completion_info,
        )
        print(json.dumps(payload, indent=2, ensure_ascii=False))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
