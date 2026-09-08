"""Scale, latency budget and telemetry tests for RM-08 (dense graphs and incremental cache)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.roadmap.compiler import RoadmapCompiler
from core.roadmap.models import (
    ConfidenceLevel,
    DeliveryStatus,
    DependencyType,
    LifecycleStage,
    PlanningHorizon,
    RoadmapCandidate,
    RoadmapDependency,
    RoadmapItemType,
    RoadmapSourceRef,
    RoadmapSourceState,
)
from core.roadmap.service import RoadmapQueryService, build_repository_roadmap_service
from core.roadmap.sources import RoadmapSourceResult
from core.roadmap.store import RoadmapSnapshotStore


@dataclass
class SyntheticDenseGraphSource:
    """Generates a dense DAG of N items and M relations for stress and scale testing."""

    source_id: str = "synthetic-dense-graph"
    priority: int = 10
    total_items: int = 500
    total_relations: int = 1500
    _cached_result: RoadmapSourceResult | None = None

    def read(self, project_id: str) -> RoadmapSourceResult:
        if self._cached_result is not None:
            return self._cached_result
        records: list[RoadmapCandidate] = []
        stages = [
            LifecycleStage.FOUNDATIONS,
            LifecycleStage.EXECUTION,
            LifecycleStage.NEXT_STEPS,
            LifecycleStage.FUTURE,
        ]

        # 1. Generate items partitioned across stages to ensure DAG acyclicity
        relations_per_item = max(1, self.total_relations // self.total_items)
        generated_relations = 0

        for i in range(self.total_items):
            item_id = f"SCALE-{i:04d}"
            stage = stages[min(len(stages) - 1, (i * len(stages)) // self.total_items)]

            # Dependencies can only point to preceding items (guarantees acyclicity)
            deps: list[RoadmapDependency] = []
            if i > 0 and generated_relations < self.total_relations:
                # Add up to 3 dependencies from earlier items
                num_deps = min(relations_per_item, i)
                step = max(1, i // (num_deps + 1))
                for k in range(1, num_deps + 1):
                    target_idx = max(0, i - (k * step))
                    deps.append(
                        RoadmapDependency(
                            item_id=f"SCALE-{target_idx:04d}",
                            type=DependencyType.REQUIRES,
                            label="Dense dependency",
                        )
                    )
                    generated_relations += 1

            records.append(
                RoadmapCandidate(
                    id=item_id,
                    project_id=project_id,
                    title=f"Synthetic item {item_id}",
                    description=f"Generated scale item {item_id} with {len(deps)} deps",
                    item_type=RoadmapItemType.FEATURE,
                    lifecycle_stage=stage,
                    delivery_status=DeliveryStatus.PLANNED,
                    horizon=PlanningHorizon.NOW,
                    confidence=ConfidenceLevel.MEDIUM,
                    dependencies=deps,
                    source_id=self.source_id,
                    source_priority=self.priority,
                    source_refs=[
                        RoadmapSourceRef(
                            source_id=self.source_id,
                            source_kind="synthetic",
                            label="Scale generator",
                            locator="memory://scale",
                        )
                    ],
                )
            )

        state = RoadmapSourceState(
            source_id=self.source_id,
            label="Synthetic Dense Graph",
            source_kind="synthetic",
            locator="memory://scale",
            revision="rev-01",
            content_hash="scale-hash-01",
        )
        self._cached_result = RoadmapSourceResult(state=state, records=records)
        return self._cached_result


def test_dense_graph_500_items_1500_relations_latency_budget() -> None:
    source = SyntheticDenseGraphSource(total_items=500, total_relations=1500)
    compiler = RoadmapCompiler([source])
    store = RoadmapSnapshotStore()

    # 1. Compile scale graph and measure latency budget (< 500ms target)
    start_time = time.perf_counter()
    snapshot = store.get_or_compile("scale-proj", compiler)
    first_duration_ms = (time.perf_counter() - start_time) * 1000

    assert len(snapshot.items) == 500
    total_edges = sum(len(it.dependencies) for it in snapshot.items)
    assert total_edges >= 1000, f"Expected dense relations, got {total_edges}"
    assert len(snapshot.issues) == 0, f"Unexpected issues in synthetic DAG: {snapshot.issues}"
    assert first_duration_ms < 500.0, f"Compilation exceeded latency budget: {first_duration_ms:.2f}ms"

    # 2. Warm cache hit latency budget (< 100ms target)
    warm_start = time.perf_counter()
    warm_snapshot = store.get_or_compile("scale-proj", compiler)
    warm_duration_ms = (time.perf_counter() - warm_start) * 1000

    assert warm_snapshot.snapshot_hash == snapshot.snapshot_hash
    assert warm_duration_ms < 100.0, f"Warmed cache hit took too long: {warm_duration_ms:.2f}ms"

    # 3. Telemetry inspection
    telemetry = store.get_telemetry("scale-proj")
    assert telemetry["requests_total"] == 2
    assert telemetry["cache_hits"] == 1
    assert telemetry["cache_misses"] == 1
    assert telemetry["hit_ratio"] == 0.5
    assert telemetry["items_count"] == 500


def test_store_telemetry_source_change_detection() -> None:
    store = RoadmapSnapshotStore()

    @dataclass
    class MutableSource:
        source_id: str = "mut-source"
        priority: int = 10
        revision: str = "v1"

        def read(self, project_id: str) -> RoadmapSourceResult:
            state = RoadmapSourceState(
                source_id=self.source_id,
                label=self.source_id,
                source_kind="doc",
                locator="doc",
                revision=self.revision,
                content_hash=f"hash-{self.revision}",
            )
            item = RoadmapCandidate(
                id="T-01",
                project_id=project_id,
                title="Mutable ticket",
                item_type=RoadmapItemType.FEATURE,
                lifecycle_stage=LifecycleStage.EXECUTION,
                delivery_status=DeliveryStatus.PLANNED,
                horizon=PlanningHorizon.NOW,
                confidence=ConfidenceLevel.MEDIUM,
                source_id=self.source_id,
                source_priority=self.priority,
            )
            return RoadmapSourceResult(state=state, records=[item])

    src = MutableSource(revision="v1")
    compiler1 = RoadmapCompiler([src])
    store.get_or_compile("proj", compiler1)

    # First request: 1 miss, 0 changes detected
    t1 = store.get_telemetry("proj")
    assert t1["requests_total"] == 1
    assert t1["source_changes_detected"] == 0

    # Modify source revision
    src.revision = "v2"
    compiler2 = RoadmapCompiler([src])
    store.get_or_compile("proj", compiler2)

    # Second request: source change detected!
    t2 = store.get_telemetry("proj")
    assert t2["requests_total"] == 2
    assert t2["source_changes_detected"] == 1


def test_service_health_exposes_telemetry() -> None:
    service = build_repository_roadmap_service(Path(__file__).parents[1])
    health = service.get_health("darkfac")

    assert health.telemetry is not None
    assert "hit_ratio" in health.telemetry
    assert "requests_total" in health.telemetry
    assert health.telemetry["requests_total"] >= 1
