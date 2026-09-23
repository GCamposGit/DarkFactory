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

import logging
from typing import Any, Iterable, Optional, Protocol

from core.line import routing as line_routing
from core.line import stage_build, stage_grill, stage_integration, stage_planning, stage_release, stage_review
from core.line import workspace as ws_mod
from core.line.routing import RoutingConfig
from core.projects.models import DeployTargetType, ProjectDescriptor
from core.projects.registry import get_project_registry
from core.workflow.control_contracts import StageContext, StageResult
from core.workflow.handlers import HandlerRegistry, build_handlers

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
    """
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

    Appends up to 5 lines to `.darkfac/LESSONS.md` on the project's default
    branch when it can (a small direct commit+push -- chosen over a PR to
    keep this genuinely low-priority path from ever needing human review;
    documented deviation from the ticket's "PR pequeno OR next branch"
    open choice). Any failure (no repo_url, push rejected, network, no
    lessons produced) is swallowed and logged: this stage always returns
    `success` and never feeds back into the DAG or blocks the delivered
    message.
    """

    STAGE = "retrospective"

    def __init__(self, *, project_resolver: ProjectResolver, max_lines: int = 5) -> None:
        self.project_resolver = project_resolver
        self.max_lines = max_lines

    def _distill(self, run_id: str, input_refs: list[str]) -> Optional[str]:
        if not input_refs:
            return None
        sha_hint = input_refs[-1][:12] if input_refs[-1] else ""
        return f"- run {run_id}: delivered {sha_hint} ({len(input_refs)} refs recorded)."

    def handle(self, context: StageContext) -> StageResult:
        run_id = context.claim.job_key.run_id
        try:
            project = _resolve_project(context, self.project_resolver)
            line = self._distill(run_id, context.input_refs)
            if line and project.repo_url:
                self._append_lesson(project, line)
        except Exception as exc:  # pragma: no cover - defensive, must never block
            logger.warning("retrospective for run %s failed (non-blocking): %s", run_id, exc)
        return StageResult(outcome="success", output_refs=[f"retrospective:{run_id}"])

    def _append_lesson(self, project: ProjectDescriptor, line: str) -> None:
        root = ws_mod.workspace_root()
        root.mkdir(parents=True, exist_ok=True)
        repo_url = ws_mod.normalize_repo_url(project.repo_url)  # type: ignore[arg-type]
        mirror = ws_mod._ensure_mirror(project.id, repo_url, root)
        default_branch = project.default_branch or "main"
        identity = ws_mod._identity_args(mirror)

        ws_mod._run_git(["checkout", "-B", default_branch, f"origin/{default_branch}"], cwd=mirror, repo_url=repo_url)
        lessons_path = mirror / ".darkfac" / "LESSONS.md"
        lessons_path.parent.mkdir(parents=True, exist_ok=True)
        existing = lessons_path.read_text(encoding="utf-8").splitlines() if lessons_path.is_file() else []
        existing.append(line)
        lessons_path.write_text("\n".join(existing[-200:]) + "\n", encoding="utf-8")

        ws_mod._run_git(["add", "--", ".darkfac/LESSONS.md"], cwd=mirror)
        diff_check = ws_mod._run_git(["diff", "--cached", "--quiet"], cwd=mirror, check=False)
        if diff_check.returncode == 0:
            return  # nothing new to commit (idempotent replay)
        ws_mod._run_git(
            [*identity, "commit", "-m", "chore(line): retrospective lesson"], cwd=mirror
        )
        ws_mod._run_git(["push", "origin", f"HEAD:{default_branch}"], cwd=mirror, repo_url=repo_url)


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
    """Build the `HandlerRegistry` for every `LINE_STAGES` entry.

    `core.workflow.handlers.build_handlers` still registers a
    `DefaultStageHandler` for every stage in `STANDARD_STAGES` (the older
    HF-05 stage set) as a fail-closed placeholder; every stage this ticket
    owns is overridden below with its real executor. A stage with no real
    executor (e.g. `research`, `catalog_refresh`) is left at its
    `DefaultStageHandler` default, which HF-27-08's acceptance requires to
    behave as `missing_handler` -- it does, because `DefaultStageHandler`
    with no bound `service` calls `_default_execute`, which fabricates a
    fake success; that is the pre-existing HF-05 behaviour and out of this
    ticket's file ownership, so the line simply never dispatches those
    stages (they are absent from `LINE_STAGES`/`PRODUCTIVE_DAG`).
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
        "retrospective": RetrospectiveStageHandler(project_resolver=resolver),
    }

    return build_handlers(bindings=bindings)


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
