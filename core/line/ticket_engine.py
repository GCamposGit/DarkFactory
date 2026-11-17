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
import logging
import os
import shutil
import tempfile
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
    StageArtifactMissing,
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


class BuildEvidenceMismatch(RuntimeError):
    """A preserved BUILD artifact exists but its plan/candidate/worktree no longer matches.

    Raised instead of silently reusing or silently resetting: the caller must fail closed,
    keep the worktree and the canonical PLAN/BUILD artifacts, and never replay a finished slice.
    """


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


def has_any(ws: workspace.RunWorkspace) -> bool:
    directory = workspace.context_dir(ws)
    return directory.is_dir() and any(directory.iterdir())


def _demand_text(ticket: Any) -> str:
    lines = [f"# {ticket.id}: {ticket.title}", "", "## Problema", ticket.problem_statement or "(sem descricao)", ""]
    lines.append("## Criterios de aceite")
    lines.extend(f"- {c}" for c in ticket.acceptance_criteria or ["(nenhum registrado)"])
    return "\n".join(lines) + "\n"


def write_demand(ws: workspace.RunWorkspace, ticket: Any) -> None:
    workspace.write_context(ws, DEMAND_FILE, _demand_text(ticket))


# Artifacts that survive a relaunch whose plan is reused: the demand the plan answers, the plan, and
# the BUILD progress that records which plan slices are already done.
_PLAN_KEEP: tuple[str, ...] = (DEMAND_FILE, GRILL_FILE, SPEC_FILE, TICKETS_FILE, BUILD_FILE)


def reusable_plan(ws: workspace.RunWorkspace, ticket: Any) -> bool:
    """True when an earlier attempt left a valid plan for this very demand (no re-planning needed)."""
    directory = workspace.context_dir(ws)
    try:
        if (directory / DEMAND_FILE).read_text(encoding="utf-8") != _demand_text(ticket):
            return False
        return bool(require_artifact(Stage.PLAN, ws).tickets)  # type: ignore[union-attr]
    except (OSError, StageArtifactMissing):
        return False


def reset_keeping_plan(ws: workspace.RunWorkspace) -> None:
    """Like :func:`reset`, but the demand, plan and BUILD progress stay; everything else is dropped."""
    directory = workspace.context_dir(ws)
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if entry.name in _PLAN_KEEP:
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


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


def plan_digest(ws: workspace.RunWorkspace) -> str:
    """Identity of the PLAN artifact actually on disk (SPEC.md + tickets.json), not the ticket's."""
    from core.line.admission_store import safe_digest

    context = workspace.context_dir(ws)
    spec = (context / SPEC_FILE).read_text(encoding="utf-8") if (context / SPEC_FILE).is_file() else ""
    tickets = (context / TICKETS_FILE).read_text(encoding="utf-8") if (context / TICKETS_FILE).is_file() else ""
    return safe_digest({"spec": spec, "tickets": tickets})


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


def write_build(
    ws: workspace.RunWorkspace,
    harness: Optional[str],
    tickets_done: Iterable[str],
    *,
    plan_digest: str = "",
    candidate_digest: str = "",
    worktree: str = "",
) -> None:
    """Persist BUILD progress. Called once per finished slice (USR-475 T1), not only at the end.

    An empty ``tickets_done`` is a durable BUILD-start marker. The checkpoint is replaced
    atomically so a crash cannot leave half a JSON document after a completed slice.
    """
    if not plan_digest or not candidate_digest or not worktree:
        raise BuildEvidenceMismatch(
            "novo checkpoint BUILD exige identidade completa de plano/candidato/worktree"
        )
    completed = list(tickets_done)
    report = BuildReport(
        harness=harness, ticket_id=ws.run_id, tickets_done=completed,
        completed_slice_count=len(completed),
        plan_digest=plan_digest, candidate_digest=candidate_digest, worktree=worktree,
    )
    path = workspace.context_dir(ws) / BUILD_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        _validate_build_checkpoint_advance(path, report)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(report.model_dump_json(indent=2))
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _validate_build_checkpoint_advance(path: Path, next_report: BuildReport) -> None:
    """Reject rewrites that roll BUILD progress or its evidence back.

    A new completed slice changes the digest of the worktree contents, so the candidate digest
    may advance only when ``tickets_done`` grows. Rewriting the same prefix must keep the exact
    candidate; every update must retain the plan and worktree identity. Legacy checkpoints may be
    upgraded in place only with the same completed prefix, after the caller has verified its
    evidence.
    """
    from pydantic import ValidationError

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        previous = BuildReport.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise BuildEvidenceMismatch(
            f"checkpoint BUILD anterior invalido; arquivo preservado em {path}"
        ) from exc
    if previous.ticket_id and previous.ticket_id != next_report.ticket_id:
        raise BuildEvidenceMismatch(
            f"checkpoint BUILD pertence ao ticket {previous.ticket_id!r}; arquivo preservado em {path}"
        )
    if isinstance(raw, dict) and "completed_slice_count" in raw:
        if previous.completed_slice_count != len(previous.tickets_done):
            raise BuildEvidenceMismatch(
                f"completed_slice_count diverge no checkpoint anterior; arquivo preservado em {path}"
            )

    old_ids = previous.tickets_done
    new_ids = next_report.tickets_done
    if len(new_ids) < len(old_ids) or new_ids[:len(old_ids)] != old_ids:
        raise BuildEvidenceMismatch(
            f"checkpoint BUILD nao pode regredir ou trocar fatias concluidas; arquivo preservado em {path}"
        )
    grew = len(new_ids) > len(old_ids)

    old_identity = (previous.plan_digest, previous.candidate_digest, previous.worktree)
    new_identity = (next_report.plan_digest, next_report.candidate_digest, next_report.worktree)
    if any(old_identity) and not all(old_identity):
        raise BuildEvidenceMismatch(
            f"checkpoint BUILD anterior tem identidade parcial; arquivo preservado em {path}"
        )
    if grew:
        if not all(old_identity) or not all(new_identity):
            raise BuildEvidenceMismatch(
                f"nao e seguro avancar BUILD sem identidade completa de plano/candidato/worktree; "
                f"arquivo preservado em {path}"
            )
        if previous.plan_digest != next_report.plan_digest or previous.worktree != next_report.worktree:
            raise BuildEvidenceMismatch(
                f"plano ou worktree mudou durante BUILD; checkpoint preservado em {path}"
            )
        return

    # An unchanged legacy prefix can be upgraded with identity during verified migration. Once
    # classified, however, its evidence is immutable until a new slice has completed.
    if all(old_identity):
        if previous.plan_digest != next_report.plan_digest or previous.worktree != next_report.worktree:
            raise BuildEvidenceMismatch(
                f"plano ou worktree mudou sem concluir nova fatia; checkpoint preservado em {path}"
            )
        if previous.candidate_digest != next_report.candidate_digest:
            raise BuildEvidenceMismatch(
                f"candidato mudou sem concluir nova fatia; checkpoint preservado em {path}"
            )
    elif any(new_identity) and not all(new_identity):
        raise BuildEvidenceMismatch(
            f"nova identidade de BUILD esta incompleta; checkpoint preservado em {path}"
        )


def resumable_build(ws: workspace.RunWorkspace, candidate_digest: str, worktree: str) -> Optional[BuildReport]:
    """Preserved BUILD progress safe to continue, or ``None`` when there is none to resume.

    ``None`` means there is no BUILD checkpoint. Classified checkpoints must match their plan,
    candidate, worktree and ordered completed-slice prefix. A legacy, unclassified checkpoint is
    migrated only when it proves the entire current plan completed in order; partial legacy state
    cannot be safely resumed and fails closed without changing any artifact.
    """
    from pydantic import ValidationError

    path = workspace.context_dir(ws) / BUILD_FILE
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        report = BuildReport.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise BuildEvidenceMismatch(
            f"progress.json invalido ou ilegivel ({type(exc).__name__}); arquivo preservado em {path}"
        ) from exc
    if report.ticket_id and report.ticket_id != ws.run_id:
        raise BuildEvidenceMismatch(
            f"progress.json pertence ao ticket {report.ticket_id!r}, esperado {ws.run_id!r}; "
            f"arquivo preservado em {path}"
        )
    classified = bool(report.plan_digest and report.candidate_digest and report.worktree)
    if not classified:
        if report.plan_digest or report.candidate_digest or report.worktree:
            raise BuildEvidenceMismatch(
                f"progress.json tem identidade parcial (plan/candidate/worktree); arquivo preservado em {path}"
            )
        legacy_count_present = isinstance(raw, dict) and "completed_slice_count" in raw
        if legacy_count_present and report.completed_slice_count != len(report.tickets_done):
            raise BuildEvidenceMismatch(
                "completed_slice_count diverge das fatias registradas no checkpoint legado; "
                f"progress.json preservado em {path}"
            )
        try:
            planned_ids = [item.id for item in plan_tickets(ws)]
        except Exception as exc:  # noqa: BLE001 - legacy state must not trigger reset/replan
            raise BuildEvidenceMismatch(
                f"nao foi possivel validar o plano legado ({type(exc).__name__}); arquivo preservado em {path}"
            ) from exc
        if not planned_ids or len(set(planned_ids)) != len(planned_ids):
            raise BuildEvidenceMismatch(
                f"plano legado ausente ou com ids de fatia duplicados; artefatos preservados em {path.parent}"
            )
        if not report.tickets_done or report.tickets_done != planned_ids:
            raise BuildEvidenceMismatch(
                "checkpoint legado nao comprova todas as fatias do plano em ordem; "
                f"tickets_done={report.tickets_done!r}, plano={planned_ids!r}; "
                f"artefatos preservados em {path.parent}"
            )
        if not candidate_digest or not worktree:
            raise BuildEvidenceMismatch("evidencia atual de candidato/worktree ausente para migrar progress.json")
        write_build(
            ws, report.harness, report.tickets_done,
            plan_digest=plan_digest(ws), candidate_digest=candidate_digest, worktree=worktree,
        )
        return BuildReport(
            harness=report.harness, ticket_id=ws.run_id, tickets_done=report.tickets_done,
            completed_slice_count=len(report.tickets_done), plan_digest=plan_digest(ws),
            candidate_digest=candidate_digest, worktree=worktree,
        )
    if not candidate_digest or not worktree:
        raise BuildEvidenceMismatch("evidencia atual de candidato/worktree ausente")
    current_plan_digest = plan_digest(ws)
    if (
        report.plan_digest != current_plan_digest
        or report.candidate_digest != candidate_digest
        or report.worktree != worktree
    ):
        raise BuildEvidenceMismatch(
            "progress.json preservado nao bate com o plano/candidato/worktree atual "
            f"(plan {report.plan_digest[:12]} vs {current_plan_digest[:12]}; "
            f"candidate {report.candidate_digest[:12]} vs {candidate_digest[:12]}; "
            f"worktree {report.worktree!r} vs {worktree!r})"
        )
    try:
        planned_ids = [item.id for item in plan_tickets(ws)]
    except Exception as exc:  # noqa: BLE001 - never discard finished BUILD because plan evidence broke
        raise BuildEvidenceMismatch(
            f"plano preservado invalido ({type(exc).__name__}); progress.json continua em {path}"
        ) from exc
    if len(set(planned_ids)) != len(planned_ids):
        raise BuildEvidenceMismatch(
            f"plano contem ids de fatia duplicados; progress.json preservado em {path}"
        )
    if report.tickets_done != planned_ids[:len(report.tickets_done)]:
        raise BuildEvidenceMismatch(
            "tickets_done nao e prefixo ordenado do plano: "
            f"tickets_done={report.tickets_done!r}, plano={planned_ids!r}; progress.json preservado em {path}"
        )
    expected_count = len(report.tickets_done)
    count_present = isinstance(raw, dict) and "completed_slice_count" in raw
    if count_present and report.completed_slice_count != expected_count:
        raise BuildEvidenceMismatch(
            "completed_slice_count diverge das fatias registradas: "
            f"{report.completed_slice_count} vs {expected_count}; progress.json preservado em {path}"
        )
    # Checkpoints written by the initial USR-475 implementation already carried the three
    # digests but not the explicit ticket/index fields. Upgrade those only after the prefix and
    # all other evidence have been verified.
    if not report.ticket_id or not count_present:
        write_build(
            ws, report.harness, report.tickets_done,
            plan_digest=report.plan_digest,
            candidate_digest=report.candidate_digest,
            worktree=report.worktree,
        )
        report = BuildReport(
            harness=report.harness, ticket_id=ws.run_id, tickets_done=report.tickets_done,
            completed_slice_count=expected_count, plan_digest=report.plan_digest,
            candidate_digest=report.candidate_digest, worktree=report.worktree,
        )
    return report


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
    "BuildEvidenceMismatch",
    "artifacts_root", "run_workspace", "reset", "clear_delivery", "review_approves",
    "reusable_validation", "keep_reusable_validation", "ensure_approved_verdict", "has_any",
    "write_demand", "write_grill",
    "write_minimal_plan", "write_research", "plan_tickets", "plan_digest", "sub_ticket_prompt",
    "write_build", "resumable_build", "write_validation",
    "write_merge", "write_deploy", "merge_receipt_from",
]
