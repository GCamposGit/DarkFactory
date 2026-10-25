"""Stage artifacts of a `run_ticket` run (USR-375 S4).

`run_ticket.py` executes the canonical sequence of `core.line.stages` and every stage leaves its
artifact here before the next one starts. Ticket artifacts live OUTSIDE the ticket worktree, under
`state_root()/artifacts/ticket_runs/<ticket_id>/.darkfac/runs/<ticket_id>/` (gitignored), because the
worktree is removed by the merge and the post-merge artifacts (merge receipt, deploy report) must
outlive it. `RunWorkspace.path` points at that directory, so `stages.require_artifact` works unchanged.

Only helpers without launcher globals live here; the flow (agents, gate, merge) stays in `run_ticket.py`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Iterable, Optional

from core.line import workspace
from core.line.stage_planning import PlanningTicket
from core.line.stage_release import DEPLOY_REPORT_FILE, DeployReport
from core.line.stages import (
    BUILD_FILE,
    MERGE_FILE,
    RESEARCH_FILE,
    VALIDATION_FILE,
    VERDICT_FILE,
    BuildReport,
    MergeReceipt,
    Stage,
    ValidationReport,
    require_artifact,
)
from core.paths import state_root

DEMAND_FILE = "DEMAND.md"
GRILL_FILE = "GRILL.md"
SPEC_FILE = "SPEC.md"
TICKETS_FILE = "tickets.json"

# Produced by the delivery process; never trusted from an earlier attempt.
DELIVERY_FILES: tuple[str, ...] = (VALIDATION_FILE, MERGE_FILE, DEPLOY_REPORT_FILE)


def artifacts_root() -> Path:
    return state_root() / "artifacts" / "ticket_runs"


def run_workspace(ticket: Any, branch: str = "", base_sha: str = "") -> workspace.RunWorkspace:
    """Artifact workspace of `ticket` (not a git checkout: only its context dir is used)."""
    return workspace.RunWorkspace(
        project_id=getattr(ticket, "project_id", None) or "darkfac",
        run_id=ticket.id,
        branch=branch if isinstance(branch, str) and branch else f"ticket/{ticket.id.lower()}",
        path=artifacts_root() / ticket.id,
        base_sha=base_sha or "0" * 40,
    )


def reset(ws: workspace.RunWorkspace) -> None:
    """A fresh launcher run starts with no artifact: nothing from an earlier attempt counts."""
    directory = workspace.context_dir(ws)
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)


def clear_delivery(ws: workspace.RunWorkspace) -> None:
    for name in DELIVERY_FILES:
        (workspace.context_dir(ws) / name).unlink(missing_ok=True)


def has_any(ws: workspace.RunWorkspace) -> bool:
    directory = workspace.context_dir(ws)
    return directory.is_dir() and any(directory.iterdir())


def write_demand(ws: workspace.RunWorkspace, ticket: Any) -> None:
    lines = [f"# {ticket.id}: {ticket.title}", "", "## Problema", ticket.problem_statement or "(sem descricao)", ""]
    lines.append("## Criterios de aceite")
    lines.extend(f"- {c}" for c in ticket.acceptance_criteria or ["(nenhum registrado)"])
    workspace.write_context(ws, DEMAND_FILE, "\n".join(lines) + "\n")


def write_grill(ws: workspace.RunWorkspace, ticket: Any) -> None:
    """Decisions already taken at intake (G1 semantico, S6) that bound planning."""
    lines = [f"# Grill de {ticket.id}", "", "## Fora de escopo"]
    lines.extend(f"- {n}" for n in getattr(ticket, "non_goals", None) or ["(nenhum registrado)"])
    lines += ["", "## Jornada"]
    lines.extend(f"- {j}" for j in getattr(ticket, "core_journey", None) or ["(nao informada)"])
    lines += ["", "## Arquivos sugeridos"]
    lines.extend(f"- {f}" for f in getattr(ticket, "suggested_files", None) or ["(a definir no planejamento)"])
    workspace.write_context(ws, GRILL_FILE, "\n".join(lines) + "\n")


def write_minimal_plan(ws: workspace.RunWorkspace, ticket: Any) -> None:
    """Plan of a small fix: one ticket equal to the demand, no planning agent."""
    spec = f"# SPEC\n\n## Objetivo\n{ticket.title}: {ticket.problem_statement or ticket.title}\n"
    item = PlanningTicket(
        id=ticket.id, title=ticket.title, goal=ticket.problem_statement or "",
        files_hint=list(getattr(ticket, "suggested_files", None) or []),
        acceptance=list(ticket.acceptance_criteria or []),
    )
    workspace.write_context(ws, SPEC_FILE, spec)
    workspace.write_context(ws, TICKETS_FILE, json.dumps([item.model_dump()], indent=2, ensure_ascii=False))


def plan_tickets(ws: workspace.RunWorkspace) -> list[PlanningTicket]:
    plan = require_artifact(Stage.PLAN, ws)
    return list(plan.tickets)  # type: ignore[union-attr]


def sub_ticket_prompt(item: PlanningTicket, index: int, total: int) -> str:
    """Scope of one planned sub-ticket, appended to the development prompt of a large plan."""
    lines = [
        f"\n\n[PLANO {index}/{total}] Implemente agora SOMENTE o sub-ticket {item.id}: {item.title}.",
        f"Objetivo: {item.goal or item.title}",
    ]
    if item.files_hint:
        lines.append("Arquivos provaveis: " + ", ".join(item.files_hint))
    if item.acceptance:
        lines.append("Aceite:\n" + "\n".join(f"- {a}" for a in item.acceptance))
    if index > 1:
        lines.append("Os sub-tickets anteriores ja estao nesta worktree; preserve-os.")
    return "\n".join(lines) + "\n"


def write_research(ws: workspace.RunWorkspace, artifact: Any) -> None:
    workspace.write_context(ws, RESEARCH_FILE, artifact.model_dump_json(indent=2))


def write_build(ws: workspace.RunWorkspace, harness: Optional[str], tickets_done: Iterable[str]) -> None:
    report = BuildReport(harness=harness, tickets_done=list(tickets_done))
    workspace.write_context(ws, BUILD_FILE, report.model_dump_json(indent=2))


def write_validation(ws: workspace.RunWorkspace, passed: bool, gate: str, detail: str = "") -> None:
    report = ValidationReport(passed=passed, gate=gate, detail=detail)
    workspace.write_context(ws, VALIDATION_FILE, report.model_dump_json(indent=2))


def write_merge(ws: workspace.RunWorkspace, pr_url: str, merge_sha: str) -> None:
    receipt = MergeReceipt(pr_url=pr_url, merge_sha=merge_sha)
    workspace.write_context(ws, MERGE_FILE, receipt.model_dump_json(indent=2))


def write_deploy(
    ws: workspace.RunWorkspace, sha: str, smoke_ok: Optional[bool], deployed: bool, detail: str = ""
) -> None:
    report = DeployReport(sha=sha, smoke_ok=smoke_ok, deployed=deployed, detail=detail)
    workspace.write_context(ws, DEPLOY_REPORT_FILE, report.model_dump_json(indent=2))


def merge_receipt_from(report: Any, branch: str, fallback_sha: str) -> tuple[str, str]:
    """(pr_url, merge_sha) of a `TicketCompletionReport`, falling back to the branch and HEAD."""
    delivery = getattr(report, "delivery", None) or report  # a DeliveryReport carries them itself
    pr_url = getattr(delivery, "pr_url", None) or f"branch:{branch}"
    merge_sha = (
        getattr(delivery, "merge_sha", None) or getattr(report, "commit_sha", None) or fallback_sha or ""
    )
    return str(pr_url), str(merge_sha)


__all__ = [
    "DEMAND_FILE", "GRILL_FILE", "SPEC_FILE", "TICKETS_FILE", "VERDICT_FILE", "DELIVERY_FILES",
    "artifacts_root", "run_workspace", "reset", "clear_delivery", "has_any", "write_demand", "write_grill",
    "write_minimal_plan", "write_research", "plan_tickets", "sub_ticket_prompt", "write_build", "write_validation",
    "write_merge", "write_deploy", "merge_receipt_from",
]
