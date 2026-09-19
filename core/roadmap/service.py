"""Query and aggregation service shared by library, CLI and HTTP."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from core.roadmap.compiler import RoadmapCompiler
from core.roadmap.models import (
    ConfidenceLevel,
    DeliveryStatus,
    LifecycleStage,
    PlanningHorizon,
    RoadmapHealth,
    RoadmapSnapshotComparison,
    RoadmapSnapshotHistory,
    RoadmapItem,
    RoadmapProjectSummary,
    RoadmapSnapshot,
    RoadmapSourceDocument,
    RoadmapStats,
)
from core.roadmap.sources import (
    ContinuousAutonomyPlanSource,
    HybridWorkflowPlanSource,
    InfraRoadmapJsonSource,
    JsonRoadmapSource,
    MarkdownDevelopmentPlanSource,
    RoadmapSource,
    UserDemandsRoadmapSource,
)
from core.roadmap.store import RoadmapSnapshotStore


DEFAULT_PROJECTS = (
    RoadmapProjectSummary(
        id="darkfac",
        name="DarkFac",
        description="Núcleo compartilhado da fábrica autônoma e do DarkHub.",
    ),
)


def _load_registered_projects() -> list[RoadmapProjectSummary]:
    try:
        from core.projects.registry import get_project_registry
        return [p.to_roadmap_summary() for p in get_project_registry().list_projects()]
    except Exception:
        return list(DEFAULT_PROJECTS)


class RoadmapQueryService:
    def __init__(
        self,
        compiler: RoadmapCompiler | None = None,
        store: RoadmapSnapshotStore | None = None,
        projects: Iterable[RoadmapProjectSummary] | None = None,
        source_documents: dict[str, RoadmapSourceDocument] | None = None,
    ) -> None:
        self.compiler = compiler or RoadmapCompiler([])
        self.store = store or RoadmapSnapshotStore()
        initial_projects = projects if projects is not None else _load_registered_projects()
        self.projects = {project.id: project for project in initial_projects}
        self.source_documents = source_documents or {}

    def list_projects(self) -> list[RoadmapProjectSummary]:
        # Refresh from registry to pick up newly added projects without server restart
        registered = _load_registered_projects()
        for p in registered:
            if p.id not in self.projects:
                self.projects[p.id] = p
        return [self.projects[key] for key in sorted(self.projects)]

    def get_snapshot(
        self,
        project_id: str,
        *,
        search: str | None = None,
        item_type: str | None = None,
        lifecycle_stage: str | None = None,
        delivery_status: str | None = None,
        horizon: str | None = None,
        confidence: str | None = None,
        source_id: str | None = None,
    ) -> RoadmapSnapshot:
        self._ensure_project(project_id)
        snapshot = self.store.get_or_compile(project_id, self.compiler)
        items = self.filter_items(
            snapshot.items,
            search=search,
            item_type=item_type,
            lifecycle_stage=lifecycle_stage,
            delivery_status=delivery_status,
            horizon=horizon,
            confidence=confidence,
            source_id=source_id,
        )
        if len(items) == len(snapshot.items):
            return snapshot
        return snapshot.model_copy(update={
            "items": items,
            "stats": self._stats(items),
        })

    def get_item(self, project_id: str, item_id: str) -> RoadmapItem | None:
        snapshot = self.get_snapshot(project_id)
        return next((item for item in snapshot.items if item.id == item_id), None)

    def get_health(self, project_id: str) -> RoadmapHealth:
        snapshot = self.get_snapshot(project_id)
        error_count = sum(issue.severity == "error" for issue in snapshot.issues)
        warning_count = sum(issue.severity == "warning" for issue in snapshot.issues)
        return RoadmapHealth(
            project_id=project_id,
            snapshot_id=snapshot.snapshot_id,
            snapshot_hash=snapshot.snapshot_hash,
            observed_at=snapshot.observed_at,
            total_items=snapshot.stats.total_items,
            confirmed_items=snapshot.stats.confirmed_items,
            blocked_items=snapshot.stats.blocked_items,
            conflicts=snapshot.stats.conflict_items,
            warnings=warning_count + error_count,
            stale=bool(snapshot.sources_unavailable) or snapshot.stats.stale_items > 0,
            sources_consulted=snapshot.sources_consulted,
            sources_unavailable=snapshot.sources_unavailable,
            policy=snapshot.derivation_policy,
            telemetry=self.get_telemetry(project_id),
        )

    def get_telemetry(self, project_id: str | None = None) -> dict[str, Any]:
        """Expose operational and scale telemetry from the snapshot store."""
        return self.store.get_telemetry(project_id)

    def get_history(self, project_id: str, *, limit: int | None = None) -> RoadmapSnapshotHistory:
        """Return retained snapshot metadata after compiling the current state."""

        self._ensure_project(project_id)
        self.store.get_or_compile(project_id, self.compiler)
        return RoadmapSnapshotHistory(
            project_id=project_id,
            snapshots=self.store.get_history(project_id, limit=limit),
        )

    def compare_snapshots(
        self,
        project_id: str,
        from_snapshot_id: str,
        to_snapshot_id: str,
    ) -> RoadmapSnapshotComparison:
        """Compare two retained snapshots without mutating either one."""

        self._ensure_project(project_id)
        self.store.get_or_compile(project_id, self.compiler)
        return self.store.compare(project_id, from_snapshot_id, to_snapshot_id)

    def get_source_document(self, project_id: str, source_id: str) -> RoadmapSourceDocument | None:
        self._ensure_project(project_id)
        return self.source_documents.get(source_id)

    @staticmethod
    def filter_items(
        items: Iterable[RoadmapItem],
        *,
        search: str | None = None,
        item_type: str | None = None,
        lifecycle_stage: str | None = None,
        delivery_status: str | None = None,
        horizon: str | None = None,
        confidence: str | None = None,
        source_id: str | None = None,
    ) -> list[RoadmapItem]:
        normalized_search = search.strip().lower() if search else ""
        result: list[RoadmapItem] = []
        for item in items:
            if item_type and item.item_type.value != item_type:
                continue
            if lifecycle_stage and item.lifecycle_stage.value != lifecycle_stage:
                continue
            if delivery_status and item.delivery_status.value != delivery_status:
                continue
            if horizon and item.horizon.value != horizon:
                continue
            if confidence and item.confidence.value != confidence:
                continue
            if source_id and not any(ref.source_id == source_id for ref in item.source_refs):
                continue
            if normalized_search:
                haystack = " ".join([
                    item.id,
                    item.title,
                    item.description,
                    " ".join(item.tags),
                ]).lower()
                if normalized_search not in haystack:
                    continue
            result.append(item)
        return result

    def _ensure_project(self, project_id: str) -> None:
        if project_id not in self.projects:
            raise KeyError(f"unknown project: {project_id}")

    @staticmethod
    def _stats(items: list[RoadmapItem]) -> RoadmapStats:
        from core.roadmap.compiler import RoadmapCompiler

        return RoadmapCompiler._stats(items)


def build_repository_roadmap_service(
    repository_root: Path,
    *,
    demands_path: Path | None = None,
    include_demands: bool = False,
    include_hf: bool = False,
    include_infra: bool = False,
    include_continuous_autonomy: bool = False,
    hf_plan_path: Path | None = None,
    infra_roadmap_path: Path | None = None,
    continuous_autonomy_plan_path: Path | None = None,
) -> RoadmapQueryService:
    """Build the default DarkHub service from all versioned roadmap sources."""

    repository_root = Path(repository_root)
    manifest_path = repository_root / ".factory" / "roadmap" / "darkfac.json"
    development_plan_path = repository_root / "docs" / "DEVELOPMENT_PLAN_2026-09-05.md"
    evidence_dir = repository_root / ".factory" / "reports"

    target_continuous_path = continuous_autonomy_plan_path or (
        repository_root / ".factory" / "planning" / "continuous-autonomy" / "plan.json"
    )
    plan_to_enrich = target_continuous_path if target_continuous_path.exists() else None

    manifest_source = JsonRoadmapSource(
        manifest_path,
        evidence_dir=evidence_dir,
        plan_path=plan_to_enrich,
    )
    development_plan_source = MarkdownDevelopmentPlanSource(
        development_plan_path,
        evidence_dir=evidence_dir,
    )
    sources: list[RoadmapSource] = [manifest_source]
    documents: dict[str, RoadmapSourceDocument] = {
        manifest_source.source_id: RoadmapSourceDocument(
            source_id=manifest_source.source_id,
            label=manifest_source.label,
            locator=".factory/roadmap/darkfac.json",
            content=(
                manifest_path.read_text(encoding="utf-8")
                if manifest_path.exists() else ""
            ),
            content_type="application/json",
        ),
    }

    if include_demands or demands_path is not None:
        target_demands_path = demands_path or (repository_root / ".factory" / "demands" / "demands.json")
        user_demands_source = UserDemandsRoadmapSource(
            target_demands_path,
            evidence_dir=evidence_dir,
        )
        sources.append(user_demands_source)
        documents[user_demands_source.source_id] = RoadmapSourceDocument(
            source_id=user_demands_source.source_id,
            label=user_demands_source.label,
            locator=target_demands_path.relative_to(repository_root).as_posix() if target_demands_path.is_relative_to(repository_root) else target_demands_path.as_posix(),
            content=(
                target_demands_path.read_text(encoding="utf-8")
                if target_demands_path.exists() else ""
            ),
            content_type="application/json",
        )

    sources.append(development_plan_source)
    documents[development_plan_source.source_id] = RoadmapSourceDocument(
        source_id=development_plan_source.source_id,
        label=development_plan_source.label,
        locator="docs/DEVELOPMENT_PLAN_2026-09-05.md",
        content=(
            development_plan_path.read_text(encoding="utf-8")
            if development_plan_path.exists() else ""
        ),
        content_type="text/markdown",
    )

    if include_infra or infra_roadmap_path is not None:
        target_infra_path = infra_roadmap_path or (repository_root / ".factory" / "infra" / "roadmap.json")
        infra_source = InfraRoadmapJsonSource(
            target_infra_path,
            evidence_dir=evidence_dir,
        )
        sources.append(infra_source)
        documents[infra_source.source_id] = RoadmapSourceDocument(
            source_id=infra_source.source_id,
            label=infra_source.label,
            locator=target_infra_path.relative_to(repository_root).as_posix() if target_infra_path.is_relative_to(repository_root) else target_infra_path.as_posix(),
            content=(
                target_infra_path.read_text(encoding="utf-8")
                if target_infra_path.exists() else ""
            ),
            content_type="application/json",
        )

    if include_hf or hf_plan_path is not None:
        target_hf_path = hf_plan_path or (repository_root / "docs" / "HYBRID_WORKFLOW_PLAN_2026-09-08.md")
        hf_source = HybridWorkflowPlanSource(
            target_hf_path,
            evidence_dir=evidence_dir,
        )
        sources.append(hf_source)
        documents[hf_source.source_id] = RoadmapSourceDocument(
            source_id=hf_source.source_id,
            label=hf_source.label,
            locator=target_hf_path.relative_to(repository_root).as_posix() if target_hf_path.is_relative_to(repository_root) else target_hf_path.as_posix(),
            content=(
                target_hf_path.read_text(encoding="utf-8")
                if target_hf_path.exists() else ""
            ),
            content_type="text/markdown",
        )

    if include_continuous_autonomy:
        ca_source = ContinuousAutonomyPlanSource(
            target_continuous_path,
            evidence_dir=evidence_dir,
        )
        sources.append(ca_source)
        documents[ca_source.source_id] = RoadmapSourceDocument(
            source_id=ca_source.source_id,
            label=ca_source.label,
            locator=target_continuous_path.relative_to(repository_root).as_posix() if target_continuous_path.is_relative_to(repository_root) else target_continuous_path.as_posix(),
            content=(
                target_continuous_path.read_text(encoding="utf-8")
                if target_continuous_path.exists() else ""
            ),
            content_type="application/json",
        )

    return RoadmapQueryService(
        compiler=RoadmapCompiler(sources),
        source_documents=documents,
    )
