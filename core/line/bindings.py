"""Line stage registry and capability requirements (HF-27-08).

Wires every `core/line/stage_*.py` handler from HF-27-04/05/06/07 into the
generic `core.workflow.handlers.HandlerRegistry`/`HandlerDescriptor`
machinery, and defines the DAG's capability contract (D-a) consumed both by
`core.orchestrator.cloud_worker.CloudWorker` (to publish what a host can
claim) and by `core.workflow.successors.materialize_result` (to stamp a
freshly created job's `required_capabilities`).

Deviation from the ticket's original sketch, documented per the HF-27-08
report contract: `build_line_registry` takes a `store` (a `ControlStore`)
in addition to `host_caps`, because the `grill`/`planning` handlers need to
read the run's original intake payload (`ControlStore.get_run_payload`,
added in this ticket) -- `StageContext` carries no payload field, only refs.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional, Protocol

from core.line import routing as line_routing
from core.line import stage_build, stage_grill, stage_integration, stage_planning, stage_release, stage_review
from core.line.routing import RoutingConfig
from core.projects.models import DeployTargetType, ProjectDescriptor
from core.projects.registry import get_project_registry
from core.workflow.control_contracts import StageContext, StageResult
from core.workflow.handlers import HandlerRegistry

logger = logging.getLogger(__name__)

# HF-27-08 D-a: target_journey is merged into build_deploy. The stage KEY
# "build_deploy" is what the DAG (core.workflow.successors) and the worker
# use to claim/dispatch; it binds to ReleaseStageHandler, whose own module
# constant `STAGE = "release"` is purely an internal label on that class and
# is never used for routing/capability lookups.
LINE_STAGES: tuple[str, ...] = (
    "grill",
    "planning",
    "development",
    "validation",
    "independent_review",
    "integration",
    "build_deploy",
    "retrospective",
)

# Stages that read/write the run's git worktree and call out to an agent
# harness. "harness:any" is a synthetic capability CloudWorker publishes
# (HF-27-08 item D) whenever it has at least one authenticated harness:*,
# since ControlStore.claim()'s capability filter is a strict AND-of-membership
# and cannot express "any one of harness:claude/codex/grok/antigravity".
_AGENT_STAGES: frozenset[str] = frozenset(
    {"grill", "planning", "development", "independent_review", "integration"}
)


class ProjectResolver(Protocol):
    def __call__(self, project_id: str) -> Optional[ProjectDescriptor]: ...


class UnknownProjectError(RuntimeError):
    """Raised when a job's project (JobKey.ticket_id, see D-f note below) is unregistered.

    Line runs store `project_id` in `JobKey.ticket_id` (set by
    `ControlStore.accept()`'s initial grill job insert); every other stage's
    job is materialized from that same run, so `ticket_id` is stable across
    the whole run.
    """


def default_project_resolver() -> ProjectResolver:
    return get_project_registry().get_project


def required_caps(project: ProjectDescriptor, stage: str) -> list[str]:
    """Extra `required_capabilities` a job for `stage` on `project` needs.

    - grill/planning/development/independent_review/integration: git + at
      least one harness, plus the project's own `exec_affinity`.
    - validation: git + `exec_affinity` (deterministic, no agent harness).
    - build_deploy: git, plus `target:local_service` only when
      `deploy.type == local_service` (so a worker without that adapter
      never claims it), plus `exec_affinity`.
    - retrospective: git only (best-effort, never blocks).

    A project with no `repo_url` can never actually run through the line
    (every stage from `grill` onward opens with `workspace.checkout`, which
    raises `WorkspaceError` without one) -- gating it would only ever
    silently starve a job for a non-line project (e.g. the default
    `darkfac` registry entry, which many pre-existing, non-line
    `ControlStore.accept()` callers/tests use with no `repo_url` at all) of
    workers that would otherwise process it correctly. `required_caps()`
    is a no-op for those, exactly like before HF-27-08.
    """
    if not project.repo_url:
        return []

    caps: list[str] = []
    if stage in _AGENT_STAGES:
        caps.extend(["git", "harness:any"])
    elif stage == "validation":
        caps.append("git")
    elif stage == "build_deploy":
        caps.append("git")
        if project.deploy is not None and project.deploy.type == DeployTargetType.LOCAL_SERVICE:
            caps.append("target:local_service")
    elif stage == "retrospective":
        caps.append("git")

    if stage in _AGENT_STAGES or stage in ("validation", "build_deploy"):
        for affinity in project.exec_affinity:
            if affinity not in caps:
                caps.append(affinity)

    return caps


# --------------------------------------------------------------------------
# StageContext -> (project, run_id) resolution
# --------------------------------------------------------------------------


def _resolve_project(context: StageContext, resolver: ProjectResolver) -> ProjectDescriptor:
    project_id = context.claim.job_key.ticket_id
    project = resolver(project_id)
    if project is None:
        raise UnknownProjectError(
            f"unknown project '{project_id}' for job {context.claim.job_key.canonical_key()}"
        )
    return project


def _render_demand_text(payload: dict[str, Any]) -> str:
    """Render the intake payload's required keys into the free-text demand the grill agent reads."""
    lines = [f"# {payload.get('title') or '(sem titulo)'}", "", "## Problema", str(payload.get("problem") or "")]
    journey = payload.get("journey")
    journey_text = journey if isinstance(journey, str) else "\n".join(str(j) for j in (journey or []))
    lines += ["", "## Jornada", journey_text]
    non_goals = payload.get("non_goals") or []
    if non_goals:
        lines += ["", "## Fora de escopo"] + [f"- {item}" for item in non_goals]
    criteria = payload.get("criteria") or []
    if criteria:
        lines += ["", "## Criterios de aceite"] + [f"- {item}" for item in criteria]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Thin StageHandler adapters over the function-shaped HF-27-04 stages
# --------------------------------------------------------------------------


class GrillStageHandler:
    """Adapts `stage_grill.run_grill` (a function) to the `StageHandler` protocol."""

    STAGE = "grill"

    def __init__(
        self,
        *,
        store: Any,
        host_caps: Iterable[str],
        routing_config: Optional[RoutingConfig],
        project_resolver: ProjectResolver,
    ) -> None:
        self.store = store
        self.host_caps = list(host_caps)
        self.routing_config = routing_config
        self.project_resolver = project_resolver

    def handle(self, context: StageContext) -> StageResult:
        project = _resolve_project(context, self.project_resolver)
        run_id = context.claim.job_key.run_id
        payload = self.store.get_run_payload(run_id) or {}
        demand_text = _render_demand_text(payload)
        parent_grill = payload.get("parent_grill")
        return stage_grill.run_grill(
            project,
            run_id,
            demand_text,
            host_caps=self.host_caps,
            routing_config=self.routing_config,
            parent_grill=parent_grill,
        )


class PlanningStageHandler:
    """Adapts `stage_planning.run_planning` (a function) to the `StageHandler` protocol."""

    STAGE = "planning"

    def __init__(
        self,
        *,
        store: Any,
        host_caps: Iterable[str],
        routing_config: Optional[RoutingConfig],
        project_resolver: ProjectResolver,
        intake_service: Optional[Any] = None,
    ) -> None:
        self.store = store
        self.host_caps = list(host_caps)
        self.routing_config = routing_config
        self.project_resolver = project_resolver
        self.intake_service = intake_service

    def handle(self, context: StageContext) -> StageResult:
        project = _resolve_project(context, self.project_resolver)
        run_id = context.claim.job_key.run_id
        payload = self.store.get_run_payload(run_id) or {}
        return stage_planning.run_planning(
            project,
            run_id,
            host_caps=self.host_caps,
            routing_config=self.routing_config,
            intake_service=self.intake_service,
            policy_ref=payload.get("policy_ref", "darkfac://line/v1"),
        )


class DevelopmentStageHandler:
    """Adapts `stage_build.DevelopmentStage.run(project, run_id)` to the `StageHandler` protocol."""

    STAGE = "development"

    def __init__(self, *, project_resolver: ProjectResolver, **stage_kwargs: Any) -> None:
        self.project_resolver = project_resolver
        self._stage = stage_build.DevelopmentStage(**stage_kwargs)

    def handle(self, context: StageContext) -> StageResult:
        project = _resolve_project(context, self.project_resolver)
        return self._stage.run(project, context.claim.job_key.run_id)


class ValidationStageHandler:
    """Adapts `stage_build.ValidationStage.run(project, run_id)` to the `StageHandler` protocol."""

    STAGE = "validation"

    def __init__(self, *, project_resolver: ProjectResolver, **stage_kwargs: Any) -> None:
        self.project_resolver = project_resolver
        self._stage = stage_build.ValidationStage(**stage_kwargs)

    def handle(self, context: StageContext) -> StageResult:
        project = _resolve_project(context, self.project_resolver)
        return self._stage.run(project, context.claim.job_key.run_id)


class ReviewStageHandler:
    """Adapts `stage_review.ReviewStage.run(project, run_id)` to the `StageHandler` protocol."""

    STAGE = "independent_review"

    def __init__(self, *, project_resolver: ProjectResolver, **stage_kwargs: Any) -> None:
        self.project_resolver = project_resolver
        self._stage = stage_review.ReviewStage(**stage_kwargs)

    def handle(self, context: StageContext) -> StageResult:
        project = _resolve_project(context, self.project_resolver)
        return self._stage.run(project, context.claim.job_key.run_id)


class IntegrationStageAdapter:
    """Resolves the project per call and delegates to `IntegrationStageHandler`.

    `IntegrationStageHandler.__init__` takes `project` positionally (one
    instance per project), while the registry is shared across every
    project's jobs -- a fresh handler is constructed per `handle()` call to
    keep that per-project constructor contract intact.
    """

    STAGE = "integration"

    def __init__(self, *, project_resolver: ProjectResolver, **handler_kwargs: Any) -> None:
        self.project_resolver = project_resolver
        self._handler_kwargs = handler_kwargs

    def handle(self, context: StageContext) -> StageResult:
        project = _resolve_project(context, self.project_resolver)
        handler = stage_integration.IntegrationStageHandler(project, **self._handler_kwargs)
        return handler.handle(context)


class ReleaseStageAdapter:
    """Resolves the project per call and delegates to `ReleaseStageHandler` (build_deploy)."""

    STAGE = "build_deploy"

    def __init__(self, *, project_resolver: ProjectResolver, **handler_kwargs: Any) -> None:
        self.project_resolver = project_resolver
        self._handler_kwargs = handler_kwargs

    def handle(self, context: StageContext) -> StageResult:
        project = _resolve_project(context, self.project_resolver)
        handler = stage_release.ReleaseStageHandler(project, **self._handler_kwargs)
        return handler.handle(context)


class RetrospectiveStageHandler:
    """Best-effort, never-blocking retrospective (HF-27-08 item H, low priority).

    Review item 8: a direct push to the target repo's default branch on
    every single run was removed -- on a project with Dokploy's
    push-triggered auto-deploy that push itself would trigger a production
    deploy, and it bypassed branch protection entirely. This stage now only
    *records a run summary* (stages touched, each stage's max iteration,
    total cost, final outcome) to `core.telemetry.store.TelemetryStore`
    (`.factory/telemetry.db`) when available, else as a single structured
    log line. It never writes to the target repo. Generating
    `.darkfac/LESSONS.md` via a reviewed PR (not a direct push) is a
    documented follow-up, not solved here.
    """

    STAGE = "retrospective"

    def __init__(self, *, store: Any, project_resolver: ProjectResolver) -> None:
        self.store = store
        self.project_resolver = project_resolver

    def handle(self, context: StageContext) -> StageResult:
        run_id = context.claim.job_key.run_id
        try:
            summary = self._build_summary(context)
            self._record_summary(run_id, summary)
        except Exception as exc:  # pragma: no cover - defensive, must never block
            logger.warning("retrospective summary for run %s failed (non-blocking): %s", run_id, exc)
        return StageResult(outcome="success", output_refs=[f"retrospective:{run_id}"])

    def _build_summary(self, context: StageContext) -> dict[str, Any]:
        run_id = context.claim.job_key.run_id
        project_id = context.claim.job_key.ticket_id
        status: Optional[dict[str, Any]] = None
        getter = getattr(self.store, "get_run_status", None)
        if getter is not None:
            try:
                status = getter(run_id)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("get_run_status(%s) failed for retrospective: %s", run_id, exc)

        jobs = (status or {}).get("jobs", [])
        stages: dict[str, dict[str, Any]] = {}
        total_cost = 0.0
        for job in jobs:
            stage = job.get("stage")
            if not stage:
                continue
            cost = float(job.get("actual_cost") or 0.0)
            total_cost += cost
            entry = stages.setdefault(stage, {"max_iteration": 0, "last_status": job.get("status")})
            entry["max_iteration"] = max(entry["max_iteration"], int(job.get("iteration") or 0))
            entry["last_status"] = job.get("status")

        return {
            "run_id": run_id,
            "project_id": project_id,
            "final_outcome": (status or {}).get("status", "unknown"),
            "total_cost_usd": round(total_cost, 4),
            "stages": stages,
        }

    def _record_summary(self, run_id: str, summary: dict[str, Any]) -> None:
        try:
            from core.telemetry.models import TelemetryRecordCreate
            from core.telemetry.store import TelemetryStore

            TelemetryStore().record(
                TelemetryRecordCreate(
                    ticket_id=run_id,
                    provider="darkfac.line",
                    model="retrospective-summary",
                    harness="core.line.retrospective",
                    cost_usd=summary["total_cost_usd"],
                    success=summary["final_outcome"] == "completed",
                    metadata=summary,
                )
            )
            return
        except Exception as exc:  # pragma: no cover - defensive, TelemetryStore is optional
            logger.debug("TelemetryStore unavailable for run %s (%s); logging structured summary instead", run_id, exc)

        logger.info("retrospective_run_summary %s", json.dumps(summary, ensure_ascii=False, default=str))


# --------------------------------------------------------------------------
# Registry construction
# --------------------------------------------------------------------------


def build_line_registry(
    host_caps: Iterable[str],
    *,
    store: Any,
    routing_config: Optional[RoutingConfig] = None,
    project_resolver: Optional[ProjectResolver] = None,
    intake_service: Optional[Any] = None,
    gh_executable: str = "gh",
) -> HandlerRegistry:
    """Build the `HandlerRegistry` for exactly `LINE_STAGES`, fail-closed otherwise.

    Review item 3: `core.workflow.handlers.build_handlers(bindings=...)`
    (used by the older HF-05 stage set) also registers a
    `DefaultStageHandler` for every one of `STANDARD_STAGES`, and
    `DefaultStageHandler` with no bound `service` *fabricates a fake
    success* (`_default_execute`) rather than failing closed. Calling
    `build_handlers` directly here would let a legacy stage like `research`
    or `catalog_refresh` "succeed" through the line's own worker, silently
    breaking fail-closed. This registry is built from an empty
    `HandlerRegistry` instead, populated with exactly the `LINE_STAGES`
    bindings below; `HandlerRegistry.dispatch()`'s own `__missing__`/lookup
    path already returns `StageResult(outcome="failed",
    cause_code="missing_handler")` for any other stage key, with no
    successor (see `core.workflow.handlers.HandlerRegistry.dispatch`).
    """
    host_caps = list(host_caps)
    cfg = routing_config or line_routing.load_routing_config()
    resolver = project_resolver or default_project_resolver()

    bindings: dict[str, Any] = {
        "grill": GrillStageHandler(store=store, host_caps=host_caps, routing_config=cfg, project_resolver=resolver),
        "planning": PlanningStageHandler(
            store=store, host_caps=host_caps, routing_config=cfg, project_resolver=resolver, intake_service=intake_service
        ),
        "development": DevelopmentStageHandler(project_resolver=resolver, host_caps=host_caps, routing_config=cfg),
        "validation": ValidationStageHandler(project_resolver=resolver),
        "independent_review": ReviewStageHandler(project_resolver=resolver, host_caps=host_caps, routing_config=cfg),
        "integration": IntegrationStageAdapter(
            project_resolver=resolver, gh_executable=gh_executable, host_caps=host_caps, routing_config=cfg
        ),
        "build_deploy": ReleaseStageAdapter(project_resolver=resolver),
        "retrospective": RetrospectiveStageHandler(store=store, project_resolver=resolver),
    }

    registry = HandlerRegistry()
    for stage, handler in bindings.items():
        registry[(stage, "v1")] = handler
    return registry


__all__ = [
    "LINE_STAGES",
    "ProjectResolver",
    "UnknownProjectError",
    "default_project_resolver",
    "required_caps",
    "build_line_registry",
    "GrillStageHandler",
    "PlanningStageHandler",
    "DevelopmentStageHandler",
    "ValidationStageHandler",
    "ReviewStageHandler",
    "IntegrationStageAdapter",
    "ReleaseStageAdapter",
    "RetrospectiveStageHandler",
]
