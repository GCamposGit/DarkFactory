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
from typing import Iterable, Optional, Protocol

from pydantic import BaseModel, Field, ValidationError

from core.line import stage_grill, workspace
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


class IntakeServiceLike(Protocol):
    """Minimal shape of `core.demands.autonomous_intake.AutonomousIntakeService`."""

    def accept(self, command: IntakeCommand, now: Optional[datetime] = None) -> IntakeReceipt: ...


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _job_key(run_id: str) -> str:
    return f"{run_id}:planning"


def _extract_json_object(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in agent output")
    return stripped[start : end + 1]


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
) -> StageResult:
    """Run (or reuse) the planning stage for `run_id`.

    Requires the grill stage to have already committed `DEMAND.md`/
    `GRILL.md` on the run's branch (`workspace.checkout` will see them).
    """
    ws = workspace.checkout(project, run_id)

    existing = workspace.find_commit_by_job(ws, _job_key(run_id))
    if existing:
        return StageResult(outcome="success", output_refs=[existing])

    demand_text = _read_context_file(ws, _DEMAND_FILE)
    grill_text = _read_context_file(ws, GRILL_FILE)
    if not demand_text or not grill_text:
        return StageResult(outcome="retry", cause_code="grill_not_ready")

    commands_text = _render_commands(project, ws.path)
    lessons_text = read_lessons(ws.path) or "(nenhuma)"

    base_prompt = render_prompt(
        load_prompt("planning.md"),
        demand=demand_text,
        grill=grill_text,
        commands=commands_text,
        lessons=lessons_text,
    )

    plan: Optional[PlanningPlan] = None
    last_error: str = ""
    prompt = base_prompt
    for attempt in range(MAX_REPROMPTS + 1):
        result = stage_grill.run_read_agent(
            STAGE, prompt, ws.path, host_caps=host_caps, routing_config=routing_config
        )
        if not result.ok:
            return StageResult(outcome="retry", cause_code=result.error_kind or "planning_agent_failed")
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
        return StageResult(outcome="failed", cause_code="plan_invalid")

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
