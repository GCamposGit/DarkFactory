"""Planning stage for the DarkFac production line (HF-27-04).

Implements section "Planning" of
`docs/handoffs/production-line/HF-27-04.md`: a **read-mode** agent call
(cascade `planning`, Opus/high effort per `.factory/config/line_routing.json`)
turns `DEMAND.md` + `GRILL.md` into a short `SPEC.md`, a `tickets.json` with
1-6 small tickets, and — only when the demand is product-scale or would need
more than 6 tickets — a `milestones.json` listing the remaining milestones,
each submitted as an independent child demand through the existing
`AutonomousIntakeService.accept()` (idempotent by `external_id`).

The agent itself cannot write files (read mode); it returns a single JSON
object which this module validates with Pydantic and uses to render
`SPEC.md`/`tickets.json`/`milestones.json` deterministically. An invalid
reply is retried once with the validation error appended to the prompt; a
second failure ends the stage with `failed(cause_code="plan_invalid")`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Protocol

from pydantic import BaseModel, Field, ValidationError

from core.line import diagnostics, stage_grill, workspace
from core.line.human import HumanRequest, request_human_help
from core.line.route_wait import RouteWaiter
from core.line.routing import RoutingConfig
from core.line.stage_grill import GRILL_FILE, load_prompt, read_lessons, render_prompt
from core.projects.models import ProjectDescriptor
from core.projects.registry import resolve_commands
from core.workflow.control_contracts import IntakeCommand, IntakeReceipt, StageResult

logger = logging.getLogger(__name__)

STAGE = "planning"
_SPEC_FILE = "SPEC.md"
_TICKETS_FILE = "tickets.json"
_MILESTONES_FILE = "milestones.json"
_DEMAND_FILE = "DEMAND.md"
MAX_REPROMPTS = 1

# A grill job can be `succeeded` in the control store while `DEMAND.md`/`GRILL.md` are not on the run's
# branch (observed 2026-10-07: runs whose grill was finished by `scripts/cloud_e2e_task.py`, which never
# touches git). That is not transient, so planning must not poll it: it asks for the grill to be
# re-run at most MAX_GRILL_RESCHEDULES time(s), then parks the run on the owner.
GRILL_ARTIFACTS_MISSING = "grill_artifacts_missing"
MAX_GRILL_RESCHEDULES = 1


# --------------------------------------------------------------------------
# Agent-facing contracts
# --------------------------------------------------------------------------


class PlanningSpec(BaseModel):
    objective: str = Field(min_length=1)
    out_of_scope: list[str] = Field(default_factory=list)
    design: str = ""
    files_to_touch: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class PlanningTicket(BaseModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    goal: str = ""
    files_hint: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    tests_to_add: list[str] = Field(default_factory=list)
    smoke: list[str] = Field(default_factory=list)


class PlanningMilestone(BaseModel):
    title: str = Field(min_length=1)
    demand_text: str = Field(min_length=1)


class PlanningPlan(BaseModel):
    """Parsed JSON reply from `prompts/planning.md`."""

    spec: PlanningSpec
    tickets: list[PlanningTicket] = Field(min_length=1, max_length=6)
    is_product_scale: bool = False
    milestones: list[PlanningMilestone] = Field(default_factory=list)


HumanRequester = Callable[[ProjectDescriptor, HumanRequest], Any]


class IntakeServiceLike(Protocol):
    """Minimal shape of `core.demands.autonomous_intake.AutonomousIntakeService`."""

    def accept(self, command: IntakeCommand, now: Optional[datetime] = None) -> IntakeReceipt: ...


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _job_key(run_id: str) -> str:
    return f"{run_id}:planning"


def _extract_json_object(text: str) -> str:
    return stage_grill.extract_json_object(text, ("spec", "tickets", "milestones"))


def parse_planning_plan(text: str) -> PlanningPlan:
    candidate = _extract_json_object(text)
    data = json.loads(candidate)
    return PlanningPlan.model_validate(data)


def _read_context_file(ws: workspace.RunWorkspace, name: str) -> str:
    path = workspace.context_dir(ws) / name
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _render_commands(project: ProjectDescriptor, repo_dir: Path) -> str:
    commands = resolve_commands(project, repo_dir)
    lines = []
    for label, cmds in (
        ("setup", commands.setup),
        ("validate", commands.validate_cmds),
        ("build", commands.build),
        ("smoke", commands.smoke),
    ):
        if cmds:
            lines.append(f"- {label}: {'; '.join(cmds)}")
    return "\n".join(lines) if lines else "(nenhum comando configurado)"


def _render_spec_markdown(spec: PlanningSpec) -> str:
    lines = ["# SPEC", "", "## Objetivo", spec.objective, ""]
    lines.append("## Fora de escopo")
    if spec.out_of_scope:
        lines.extend(f"- {item}" for item in spec.out_of_scope)
    else:
        lines.append("- (nenhum item registrado)")
    lines.append("")
    lines.append("## Desenho")
    lines.append(spec.design or "(sem detalhes adicionais)")
    lines.append("")
    lines.append("## Arquivos a tocar")
    if spec.files_to_touch:
        lines.extend(f"- {item}" for item in spec.files_to_touch)
    else:
        lines.append("- (a definir durante o development)")
    lines.append("")
    lines.append("## Riscos")
    if spec.risks:
        lines.extend(f"- {item}" for item in spec.risks)
    else:
        lines.append("- (nenhum risco relevante identificado)")
    lines.append("")
    return "\n".join(lines)


def _submit_milestone_children(
    intake_service: IntakeServiceLike,
    project: ProjectDescriptor,
    run_id: str,
    channel: str,
    policy_ref: str,
    milestones: list[PlanningMilestone],
    now: Optional[datetime],
    parent_grill: str,
) -> list[IntakeReceipt]:
    """Submit milestones 2..N as idempotent child demands (`depends_on=run_id`)."""
    receipts: list[IntakeReceipt] = []
    for offset, milestone in enumerate(milestones, start=2):
        command = IntakeCommand(
            project_id=project.id,
            channel=channel,
            external_id=f"{run_id}:m{offset}",
            payload={
                "title": milestone.title,
                "problem": milestone.demand_text,
                "journey": milestone.demand_text,
                "non_goals": [],
                "criteria": [],
                "depends_on": run_id,
                # Embedded (not referenced) because the parent's branch and
                # context dir are gone once its PR merges.
                "parent_grill": parent_grill,
            },
            mode="autonomous",
            policy_ref=policy_ref,
        )
        receipts.append(intake_service.accept(command, now))
    return receipts


def _grill_artifacts_missing_result(
    project: ProjectDescriptor,
    ws: workspace.RunWorkspace,
    run_id: str,
    missing: list[str],
    *,
    reschedules_used: int,
    human_requester: Optional[HumanRequester],
) -> StageResult:
    """Structured, non-polling answer to "the grill's context files are not on the run's branch".

    First time: `retry:grill` (cross-stage bounce, see `core.workflow.successors`) so the grill is
    re-run on the run's own branch and re-materializes `DEMAND.md`/`GRILL.md`. Afterwards (the grill
    already re-ran once, or the budget is spent): `waiting_human(grill_artifacts_missing)` with the
    run, branch and missing files as evidence plus a step-by-step owner request.
    """
    logger.warning(
        "planning run %s: grill artifacts missing on branch %s: %s (grill reschedules used: %d/%d)",
        run_id, ws.branch, ", ".join(missing), reschedules_used, MAX_GRILL_RESCHEDULES,
    )
    if reschedules_used < MAX_GRILL_RESCHEDULES:
        return StageResult(outcome="retry", cause_code=f"retry:grill\n{GRILL_ARTIFACTS_MISSING}")
    request = HumanRequest(
        kind="infra",
        run_id=run_id,
        blocking_stage=STAGE,
        guide_md=(
            f"O planning do run {run_id} nao encontrou {', '.join(missing)} na branch {ws.branch}, mesmo "
            "depois de a fabrica refazer o grill uma vez. Passo a passo: "
            "1. Abra o DarkHub em /live e localize este run. "
            "2. Se a demanda ainda for valida, reenvie-a pela fabrica (o novo run faz o proprio grill) e "
            "cancele este run. "
            "3. Se o run foi criado por um teste ou script (titulo ou canal de teste), apenas cancele-o."
        ),
    )
    try:
        (human_requester or request_human_help)(project, request)
    except Exception as exc:  # the structured outcome below is what matters
        logger.warning("Human request for run %s was not delivered: %s", run_id, exc)
    return StageResult(
        outcome="waiting_human",
        cause_code=GRILL_ARTIFACTS_MISSING,
        evidence_refs=[f"run:{run_id}", f"branch:{ws.branch}", *(f"missing:{name}" for name in missing)],
    )


# --------------------------------------------------------------------------
# run_planning
# --------------------------------------------------------------------------


def run_planning(
    project: ProjectDescriptor,
    run_id: str,
    *,
    channel: str = "line",
    host_caps: Iterable[str] = ("harness:claude", "harness:codex"),
    routing_config: Optional[RoutingConfig] = None,
    intake_service: Optional[IntakeServiceLike] = None,
    policy_ref: str = "darkfac://line/v1",
    now: Optional[datetime] = None,
    route_waiter: Optional[RouteWaiter] = None,
    demand_text: Optional[str] = None,
    grill_reschedules_used: int = 0,
    human_requester: Optional[HumanRequester] = None,
) -> StageResult:
    """Run (or reuse) the planning stage for `run_id`.

    Requires the grill stage to have already committed `DEMAND.md`/
    `GRILL.md` on the run's branch (`workspace.checkout` will see them).
    `route_waiter` decides what "no agent route right now" means (USR-87, see `run_grill`).

    Missing context files never burn retries: a missing `DEMAND.md` is re-rendered from `demand_text`
    (the run's intake payload, when the caller has it); a missing `GRILL.md` cannot be derived, so the
    grill is rescheduled once (`retry:grill`, `grill_reschedules_used` says how many times that
    already happened) and then the run is parked as `waiting_human(grill_artifacts_missing)`.
    """
    ws = workspace.checkout(project, run_id)
    waiter = route_waiter or RouteWaiter()

    existing = workspace.find_commit_by_job(ws, _job_key(run_id))
    if existing:
        return StageResult(outcome="success", output_refs=[existing])

    demand_md = _read_context_file(ws, _DEMAND_FILE)
    grill_text = _read_context_file(ws, GRILL_FILE)
    # A blank file is as good as a missing one (the grill treats it the same way).
    demand_md = demand_md if demand_md.strip() else ""
    grill_text = grill_text if grill_text.strip() else ""
    if grill_text and not demand_md and demand_text:
        # DEMAND.md is a pure function of the intake payload: recover it, committed with the plan below.
        demand_md = stage_grill._render_demand_markdown(project, channel, demand_text)
        workspace.write_context(ws, _DEMAND_FILE, demand_md)
        logger.warning("planning run %s: DEMAND.md re-rendered from the intake payload", run_id)
    if not demand_md or not grill_text:
        missing = [name for name, text in ((_DEMAND_FILE, demand_md), (GRILL_FILE, grill_text)) if not text]
        return _grill_artifacts_missing_result(
            project, ws, run_id, missing,
            reschedules_used=grill_reschedules_used, human_requester=human_requester,
        )

    commands_text = _render_commands(project, ws.path)
    lessons_text = read_lessons(ws.path) or "(nenhuma)"

    base_prompt = render_prompt(
        load_prompt("planning.md"),
        demand=demand_md,
        grill=grill_text,
        commands=commands_text,
        lessons=lessons_text,
    )

    plan: Optional[PlanningPlan] = None
    last_error: str = ""
    last_result = None
    prompt = base_prompt
    excluded = diagnostics.failed_pairs(diagnostics.load_attempts(ws, STAGE))
    for attempt in range(MAX_REPROMPTS + 1):
        result = stage_grill.run_read_agent(
            STAGE, prompt, ws.path, host_caps=host_caps, routing_config=routing_config, exclude=excluded
        )
        last_result = result
        if not result.ok:
            stage_grill._record_failed_agent(ws, run_id, STAGE, result)
            return stage_grill.retry_for_agent_failure(
                result, "planning_agent_failed",
                on_no_route=lambda: waiter.no_route_result(
                    STAGE, project, run_id, host_caps=host_caps, config=routing_config
                ),
            )
        try:
            plan = parse_planning_plan(result.text)
            break
        except (ValueError, json.JSONDecodeError, ValidationError) as exc:
            last_error = str(exc)
            if attempt >= MAX_REPROMPTS:
                plan = None
                break
            prompt = (
                f"{base_prompt}\n\n---\nA resposta anterior era JSON invalido: {last_error}\n"
                "Responda novamente somente com o objeto JSON valido conforme o schema."
            )

    if plan is None:
        # Bad JSON is retried on another harness before it is terminal (see stage_grill).
        return stage_grill.invalid_json_outcome(ws, run_id, STAGE, last_result, "plan_invalid")

    workspace.write_context(ws, _SPEC_FILE, _render_spec_markdown(plan.spec))
    workspace.write_context(
        ws, _TICKETS_FILE, json.dumps([t.model_dump() for t in plan.tickets], indent=2, ensure_ascii=False)
    )

    remaining_milestones = list(plan.milestones)
    if remaining_milestones:
        workspace.write_context(
            ws,
            _MILESTONES_FILE,
            json.dumps([m.model_dump() for m in remaining_milestones], indent=2, ensure_ascii=False),
        )
        if intake_service is not None:
            _submit_milestone_children(
                intake_service, project, run_id, channel, policy_ref, remaining_milestones, now,
                grill_text,
            )
        else:
            logger.warning(
                "planning run %s produced %d child milestones but no intake_service was provided; "
                "they were written to milestones.json but not submitted",
                run_id,
                len(remaining_milestones),
            )

    sha = workspace.commit(ws, "planning: SPEC + tickets", _job_key(run_id))
    workspace.push(ws)
    return StageResult(outcome="success", output_refs=[sha])
