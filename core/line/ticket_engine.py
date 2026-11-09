"""Stage artifacts of a `run_ticket` run (USR-375 S4).

`run_ticket.py` executes the canonical sequence of `core.line.stages` and every stage leaves its
artifact here before the next one starts. Ticket artifacts live OUTSIDE the ticket worktree, under
`state_root()/artifacts/ticket_runs/<ticket_id>/.darkfac/runs/<ticket_id>/` (gitignored), because the
worktree is removed by the merge and the post-merge artifacts (merge receipt, deploy report) must
outlive it. `RunWorkspace.path` points at that directory, so `stages.require_artifact` works unchanged.

Only helpers without launcher globals live here; the flow (agents, gate, merge) stays in `run_ticket.py`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from pydantic import BaseModel, Field

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
    StageArtifactMissing,
    ValidationReport,
    require_artifact,
)
from core.paths import state_root

DEMAND_FILE = "DEMAND.md"
GRILL_FILE = "GRILL.md"
SPEC_FILE = "SPEC.md"
TICKETS_FILE = "tickets.json"
PLAN_ORIGIN_FILE = "plan_origin.json"

# Produced by the delivery process; never trusted from an earlier attempt.
DELIVERY_FILES: tuple[str, ...] = (VALIDATION_FILE, MERGE_FILE, DEPLOY_REPORT_FILE)


class PlanOrigin(BaseModel):
    """Provenance of a PLAN artifact (USR-447): which ticket it belongs to and how it was produced."""

    ticket_id: str = Field(min_length=1)
    origin: str = Field(min_length=1)
    branch: str = ""
    recorded_at: str = ""
    evidence: list[str] = Field(default_factory=list)


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


def clear_delivery(ws: workspace.RunWorkspace, *, keep_validation: bool = False) -> None:
    """Drop delivery artifacts from an earlier attempt.

    ``merge.json`` and the deploy report always go. ``validation.json`` stays only
    when the caller has decided it still covers the current candidate (USR-424).
    """
    for name in DELIVERY_FILES:
        if keep_validation and name == VALIDATION_FILE:
            continue
        (workspace.context_dir(ws) / name).unlink(missing_ok=True)


def review_approves(
    ws: workspace.RunWorkspace,
    candidate_digest: Optional[str],
    policy_digest: Optional[str],
) -> bool:
    """True when the last review round approves this candidate and policy."""
    from pydantic import ValidationError

    from core.line.stage_review import ReviewState, approval_binding_valid

    path = workspace.context_dir(ws) / "review_state.json"
    if not path.is_file():
        return False
    try:
        state = ReviewState.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValidationError):
        return False
    if not state.rounds:
        return False
    last = state.rounds[-1]
    if last.verdict != "approve" or last.forced:
        return False
    return approval_binding_valid(last, candidate_digest, policy_digest)


def reusable_validation(
    ws: workspace.RunWorkspace,
    candidate_digest: Optional[str],
    policy_digest: Optional[str],
) -> bool:
    """True when a passed ``validation.json`` still covers this candidate.

    A recorded digest must equal the current candidate. An empty digest is kept
    only when ``review_approves`` accepts that candidate.
    """
    from pydantic import ValidationError

    path = workspace.context_dir(ws) / VALIDATION_FILE
    if not path.is_file():
        return False
    try:
        report = ValidationReport.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValidationError):
        return False
    if not report.passed:
        return False
    stored = report.candidate_digest.strip()
    current = (candidate_digest or "").strip()
    if stored:
        return bool(current) and stored == current
    return review_approves(ws, current or None, policy_digest)


def keep_reusable_validation(
    ws: workspace.RunWorkspace,
    candidate_digest: Optional[str],
    policy_digest: Optional[str],
) -> bool:
    """Keep a reusable validation artifact; delete it when the candidate moved."""
    if reusable_validation(ws, candidate_digest, policy_digest):
        return True
    (workspace.context_dir(ws) / VALIDATION_FILE).unlink(missing_ok=True)
    return False


def ensure_approved_verdict(ws: workspace.RunWorkspace) -> None:
    """Write ``review_verdict.json`` when a bound approval has no contract artifact yet."""
    from pydantic import ValidationError

    from core.line.stage_review import VERDICT_FILE, ReviewVerdict

    path = workspace.context_dir(ws) / VERDICT_FILE
    if path.is_file():
        try:
            verdict = ReviewVerdict.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValidationError):
            verdict = None
        if verdict is not None and verdict.verdict == "approve" and not verdict.blocking:
            return
    workspace.write_context(ws, VERDICT_FILE, ReviewVerdict(verdict="approve").model_dump_json(indent=2))


def _plan_evidence(ws: workspace.RunWorkspace) -> list[str]:
    """sha256 of each PLAN file on disk, as `"<name>:sha256:<hex>"` (empty when neither exists)."""
    directory = workspace.context_dir(ws)
    evidence: list[str] = []
    for name in (SPEC_FILE, TICKETS_FILE):
        path = directory / name
        if path.is_file():
            evidence.append(f"{name}:sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}")
    return evidence


def write_plan_origin(ws: workspace.RunWorkspace, ticket_id: str, origin: str) -> None:
    """Record PLAN provenance; called every time a PLAN artifact is (re)written for `ticket_id`."""
    record = PlanOrigin(
        ticket_id=ticket_id,
        origin=origin,
        branch=ws.branch,
        recorded_at=datetime.now(timezone.utc).isoformat(),
        evidence=_plan_evidence(ws),
    )
    workspace.write_context(ws, PLAN_ORIGIN_FILE, record.model_dump_json(indent=2))


def ensure_plan_provenance(ws: workspace.RunWorkspace, ticket_id: str) -> None:
    """Validate (or migrate) `plan_origin.json` before a PLAN artifact is trusted (USR-447).

    - No record on disk (or an unreadable one): a truly legacy worktree. If a PLAN artifact already
      sits there (pre-USR-447, no provenance was ever written), it is bound to `ticket_id` once, with
      `origin="legacy_migrated"` and the evidence found on disk. Without a PLAN artifact either there
      is nothing yet to migrate: the caller backfills PLAN itself, which records its own origin.
    - A record with no `ticket_id` field (older minimal schema): the same legacy migration, bound by
      the per-ticket artifact directory it already lives in, not by content it never carried.
    - A record whose `ticket_id` equals `ticket_id`: already bound; left untouched.
    - A record whose `ticket_id` is a *different* ticket: refused. The binding is never rewritten
      merely to pass this check — a resume must not silently claim another ticket's plan.
    """
    directory = workspace.context_dir(ws)
    path = directory / PLAN_ORIGIN_FILE
    recorded_ticket_id: Optional[str] = None
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, dict):
            recorded_ticket_id = raw.get("ticket_id") or None
    if recorded_ticket_id is not None:
        if recorded_ticket_id != ticket_id:
            raise StageArtifactMissing(
                Stage.PLAN,
                f"plan_origin.json pertence ao ticket {recorded_ticket_id}, nao a {ticket_id}",
            )
        return
    if (directory / SPEC_FILE).is_file() or (directory / TICKETS_FILE).is_file():
        write_plan_origin(ws, ticket_id, "legacy_migrated")


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
    write_plan_origin(ws, ticket.id, "minimal_small_fix")


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


def write_validation(
    ws: workspace.RunWorkspace,
    passed: bool,
    gate: str,
    detail: str = "",
    candidate_digest: str = "",
) -> None:
    report = ValidationReport(
        passed=passed, gate=gate, detail=detail, candidate_digest=candidate_digest or ""
    )
    workspace.write_context(ws, VALIDATION_FILE, report.model_dump_json(indent=2))


def write_merge(ws: workspace.RunWorkspace, pr_url: str, merge_sha: str) -> None:
    validation_path = workspace.context_dir(ws) / VALIDATION_FILE
    try:
        digest = json.loads(validation_path.read_text(encoding="utf-8")).get("candidate_digest")
    except (OSError, ValueError, AttributeError):
        digest = None
        logging.getLogger(__name__).error("Merge recorded without validation digest: ticket=%s", ws.run_id)
    receipt = MergeReceipt(pr_url=pr_url, merge_sha=merge_sha, candidate_digest=digest)
    workspace.write_context(ws, MERGE_FILE, receipt.model_dump_json(indent=2, exclude_none=True))


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
    "PLAN_ORIGIN_FILE", "PlanOrigin",
    "artifacts_root", "run_workspace", "reset", "clear_delivery", "review_approves",
    "reusable_validation", "keep_reusable_validation", "ensure_approved_verdict",
    "write_plan_origin", "ensure_plan_provenance",
    "write_demand", "write_grill",
    "write_minimal_plan", "write_research", "plan_tickets", "sub_ticket_prompt", "write_build", "write_validation",
    "write_merge", "write_deploy", "merge_receipt_from",
]
