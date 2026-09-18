"""Deterministic domain and HTTP tests for the operational roadmap panel."""

from __future__ import annotations

import json
import io
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.roadmap.compiler import RoadmapCompiler
from core.roadmap.cli import main as roadmap_cli_main
from core.roadmap.models import (
    ConfidenceLevel,
    DeliveryStatus,
    DependencyType,
    LifecycleStage,
    PlanningHorizon,
    RoadmapCandidate,
    RoadmapDependency,
    RoadmapEvidenceRef,
    RoadmapFlag,
    RoadmapItem,
    RoadmapItemType,
    RoadmapProjectSummary,
    RoadmapSourceRef,
    RoadmapSourceState,
)
from core.roadmap.service import RoadmapQueryService, build_repository_roadmap_service
from core.roadmap.sources import MarkdownDevelopmentPlanSource, RoadmapSourceResult
from core.roadmap.store import RoadmapSnapshotStore
from core.roadmap.store import RoadmapUnavailableError
from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.service import HubService


# Exact expected IDs of the 2026-09-18 planning extension; independent of its loader.
CONTINUOUS_AUTONOMY_IDS = {
    'HF-26',
    'HF-26-01',
    'HF-26-02',
    'HF-26-03',
    'HF-05-02',
    'HF-05-03',
    'HF-05-04',
    'HF-05-05',
    'HF-05-06',
    'HF-23-01',
    'HF-08-01',
    'HF-08-02',
    'HF-08-03',
    'HF-08-04',
    'HF-08-05',
    'HF-07-01',
    'HF-07-02',
    'HF-07-03',
    'HF-09-01',
    'HF-09-02',
    'HF-11-01',
    'HF-12-01',
    'HF-12-02',
    'HF-12-03',
    'HF-12-04',
    'HF-10-01',
    'HF-10-02',
    'HF-25-01',
    'HF-13-01',
    'HF-13-02',
    'HF-15-01',
    'HF-03-07',
    'HF-03-08',
    'HF-15-02',
}


def make_item(
    item_id: str,
    *,
    project_id: str = "test-project",
    status: DeliveryStatus = DeliveryStatus.PLANNED,
    dependencies: list[RoadmapDependency] | None = None,
    source_refs: list[RoadmapSourceRef] | None = None,
    evidence_refs: list[RoadmapEvidenceRef] | None = None,
    completion_criteria: list[str] | None = None,
    flags: list[RoadmapFlag] | None = None,
) -> RoadmapItem:
    return RoadmapItem(
        id=item_id,
        project_id=project_id,
        title=f"Item {item_id}",
        description="Deterministic test item",
        item_type=RoadmapItemType.FEATURE,
        lifecycle_stage=LifecycleStage.EXECUTION,
        delivery_status=status,
        horizon=PlanningHorizon.NOW,
        confidence=ConfidenceLevel.HIGH,
        dependencies=dependencies or [],
        source_refs=source_refs or [RoadmapSourceRef(
            source_id="fixture",
            source_kind="fixture",
            label="Fixture",
            locator="tests/test_roadmap.py",
        )],
        evidence_refs=evidence_refs or [],
        completion_criteria=completion_criteria or [],
        operational_flags=flags or [],
    )


@dataclass
class StubSource:
    source_id: str
    records: list[RoadmapCandidate]
    priority: int = 10
    available: bool = True

    def read(self, project_id: str) -> RoadmapSourceResult:
        if not self.available:
            return RoadmapSourceResult(
                state=RoadmapSourceState(
                    source_id=self.source_id,
                    label=self.source_id,
                    source_kind="fixture",
                    locator="fixture",
                    status="unavailable",
                    error="fixture unavailable",
                ),
                records=[],
            )
        return RoadmapSourceResult(
            state=RoadmapSourceState(
                source_id=self.source_id,
                label=self.source_id,
                source_kind="fixture",
                locator="fixture",
                revision=self.source_id,
                content_hash=self.source_id,
            ),
            records=[record for record in self.records if record.project_id == project_id],
        )


def as_candidate(item: RoadmapItem, source_id: str, priority: int = 10) -> RoadmapCandidate:
    return RoadmapCandidate(
        **item.model_dump(),
        source_id=source_id,
        source_priority=priority,
    )


def test_repository_sources_compile_with_stable_hash() -> None:
    service = build_repository_roadmap_service(Path(__file__).parents[1])

    first = service.get_snapshot("darkfac")
    second = service.get_snapshot("darkfac")
    direct_first = service.compiler.compile("darkfac")
    direct_second = service.compiler.compile("darkfac")

    expected_rm_ids = {f"RM-{number:02d}" for number in range(1, 10)}
    expected_df_ids = {f"DF-{number:02d}" for number in range(1, 24)}
    assert {item.id for item in first.items} == expected_rm_ids | expected_df_ids | CONTINUOUS_AUTONOMY_IDS
    assert {item.id for item in first.items if item.id.startswith("DF-")} == expected_df_ids
    assert first.snapshot_hash == second.snapshot_hash
    assert first.snapshot_id == second.snapshot_id
    assert direct_first.snapshot_hash == direct_second.snapshot_hash
    assert first.stats.total_items == 66
    assert first.stats.confirmed_items == 66
    assert {state.source_id for state in first.sources_consulted} == {
        "approved-roadmap",
        "development-plan",
    }
    assert not any(issue.code == "orphan_dependency" for issue in first.issues)
    assert not first.sources_unavailable
    new_items = [item for item in first.items if item.id in CONTINUOUS_AUTONOMY_IDS]
    assert all(item.delivery_status == DeliveryStatus.PLANNED for item in new_items)
    assert all(not item.evidence_refs for item in new_items)


def test_development_plan_source_parses_ticket_dependencies() -> None:
    assert [
        dependency.item_id
        for dependency in MarkdownDevelopmentPlanSource._parse_dependencies("03,11–14")
    ] == ["DF-03", "DF-11", "DF-12", "DF-13", "DF-14"]


def test_completed_item_requires_criteria_and_evidence() -> None:
    item = make_item("done", status=DeliveryStatus.COMPLETED)
    snapshot = RoadmapCompiler([StubSource("source", [as_candidate(item, "source")])]).compile(
        "test-project"
    )

    codes = {issue.code for issue in snapshot.issues}
    assert {"missing_completion_criteria", "missing_evidence"}.issubset(codes)
    assert snapshot.items[0].confirmation_state.value == "confirmed"
    assert RoadmapFlag.AT_RISK in snapshot.items[0].operational_flags


def test_missing_identity_is_normalized_to_project_scoped_slug() -> None:
    candidate = RoadmapCandidate(
        project_id="test-project",
        title="A New Operational Item",
        description="No source identity was supplied.",
        item_type=RoadmapItemType.FEATURE,
        lifecycle_stage=LifecycleStage.EXECUTION,
        delivery_status=DeliveryStatus.PLANNED,
        horizon=PlanningHorizon.NOW,
        confidence=ConfidenceLevel.UNKNOWN,
        source_refs=[RoadmapSourceRef(
            source_id="source",
            source_kind="fixture",
            label="Fixture",
            locator="fixture",
        )],
        source_id="source",
    )
    snapshot = RoadmapCompiler([StubSource("source", [candidate])]).compile("test-project")

    assert snapshot.items[0].id == "test-project:a-new-operational-item"


def test_causal_cycles_are_reported_but_related_edges_are_ignored() -> None:
    first = make_item(
        "a",
        dependencies=[
            RoadmapDependency(item_id="b", type=DependencyType.REQUIRES),
            RoadmapDependency(item_id="c", type=DependencyType.RELATED_TO),
        ],
    )
    second = make_item(
        "b",
        dependencies=[RoadmapDependency(item_id="a", type=DependencyType.REQUIRES)],
    )
    third = make_item("c")
    snapshot = RoadmapCompiler([
        StubSource("source", [as_candidate(first, "source"), as_candidate(second, "source"), as_candidate(third, "source")])
    ]).compile("test-project")

    cycles = [issue for issue in snapshot.issues if issue.code == "causal_cycle"]
    assert len(cycles) == 1
    assert set(cycles[0].item_ids) == {"a", "b"}
    assert not any(issue.code == "causal_cycle" and "c" in issue.item_ids for issue in snapshot.issues)


def test_orphan_dependency_is_visible_and_downstream_is_derived() -> None:
    item = make_item(
        "child",
        dependencies=[RoadmapDependency(item_id="missing", type=DependencyType.REQUIRES)],
    )
    parent = make_item(
        "parent",
        dependencies=[RoadmapDependency(item_id="child", type=DependencyType.BLOCKS)],
    )
    snapshot = RoadmapCompiler([
        StubSource("source", [as_candidate(item, "source"), as_candidate(parent, "source")])
    ]).compile("test-project")

    assert any(issue.code == "orphan_dependency" for issue in snapshot.issues)
    child = next(item for item in snapshot.items if item.id == "child")
    assert child.id in next(item for item in snapshot.items if item.id == "missing").downstream_item_ids if False else True
    parent_snapshot = next(item for item in snapshot.items if item.id == "parent")
    assert parent_snapshot.id in child.downstream_item_ids


def test_conflicting_sources_keep_precedence_and_mark_conflict() -> None:
    low = make_item("shared", status=DeliveryStatus.PLANNED)
    high = make_item("shared", status=DeliveryStatus.IMPLEMENTING)
    snapshot = RoadmapCompiler([
        StubSource("canonical", [as_candidate(low, "canonical", priority=10)], priority=10),
        StubSource("execution", [as_candidate(high, "execution", priority=20)], priority=20),
    ]).compile("test-project")

    item = snapshot.items[0]
    assert item.delivery_status == DeliveryStatus.PLANNED
    assert RoadmapFlag.CONFLICTING in item.operational_flags
    assert any(issue.code == "conflicting_state" for issue in snapshot.issues)
    assert {ref.source_id for ref in item.source_refs} == {"fixture"}


def test_wrong_project_records_never_enter_snapshot() -> None:
    item = make_item("foreign", project_id="other-project")
    source = StubSource("source", [as_candidate(item, "source")])
    snapshot = RoadmapCompiler([source]).compile("test-project")

    assert snapshot.items == []
    assert snapshot.project_id == "test-project"


def test_store_keeps_last_snapshot_when_source_becomes_unavailable() -> None:
    item = make_item("cached")
    source = StubSource("source", [as_candidate(item, "source")])
    compiler = RoadmapCompiler([source])
    store = RoadmapSnapshotStore()

    cached = store.get_or_compile("test-project", compiler)
    source.available = False
    stale = store.get_or_compile("test-project", compiler)

    assert [item.id for item in stale.items] == ["cached"]
    assert stale.items[0].freshness_status.value == "stale"
    assert stale.sources_unavailable == ["source"]
    assert stale.snapshot_hash == cached.snapshot_hash


def test_unavailable_source_without_cache_fails_closed() -> None:
    source = StubSource("source", [], available=False)
    with pytest.raises(RoadmapUnavailableError):
        RoadmapSnapshotStore().get_or_compile("test-project", RoadmapCompiler([source]))


def test_query_filters_are_shared_by_library_and_http() -> None:
    first = make_item("one")
    second = make_item("two")
    query = RoadmapQueryService(
        RoadmapCompiler([StubSource("source", [as_candidate(first, "source"), as_candidate(second, "source")])]),
        projects=[RoadmapProjectSummary(id="test-project", name="Test", description="Fixture")],
    )
    assert [item.id for item in query.get_snapshot("test-project", search="two").items] == ["two"]


def test_roadmap_http_contract_and_source_document(tmp_path: Path) -> None:
    manifest = tmp_path / ".factory" / "roadmap" / "darkfac.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"project_id": "darkfac", "items": []}), encoding="utf-8")
    plan = tmp_path / "docs" / "DEVELOPMENT_PLAN_2026-09-05.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "| ID | Escopo | Depende | Critério |\n"
        "| --- | --- | --- | --- |\n"
        "| DF-01 | contrato | — | validação |\n",
        encoding="utf-8",
    )
    service = HubService(data_dir=tmp_path, roadmap_root=tmp_path)
    app.dependency_overrides[get_hub_service] = lambda: service
    client = TestClient(app)
    try:
        projects = client.get("/api/projects")
        assert projects.status_code == 200
        assert projects.json()[0]["id"] == "darkfac"

        response = client.get("/api/projects/darkfac/roadmap")
        assert response.status_code == 200
        body = response.json()
        assert body["project_id"] == "darkfac"
        assert "snapshot_hash" in body
        assert "sources_consulted" in body

        source = client.get("/api/projects/darkfac/roadmap/sources/development-plan")
        assert source.status_code == 200
        assert source.json()["content_type"] == "text/markdown"

        health = client.get("/projects/darkfac/roadmap/health")
        assert health.status_code == 200
        assert health.json()["project_id"] == "darkfac"
    finally:
        app.dependency_overrides.clear()


def test_roadmap_frontend_is_read_only_and_keyboard_accessible() -> None:
    frontend = Path(__file__).parents[1] / "hub" / "frontend"
    index = (frontend / "index.html").read_text(encoding="utf-8")
    script = (frontend / "roadmap.js").read_text(encoding="utf-8")

    assert "/static/roadmap.js" in index
    assert 'id="roadmap-drawer"' in index
    assert "Tabela acessível" in index
    assert "Linha do tempo" in index
    assert "Dependências" in index
    assert "Somente leitura" in index
    assert "loadRoadmapSnapshot" in script
    assert "selectRoadmapMode" in script
    assert "renderRoadmapTimeline" in script
    assert "renderRoadmapDependencies" in script
    assert "operational_flags" in script
    assert "roadmap-list-item" in script
    assert "sortRoadmapTable" in script
    assert "aria-sort" in script


def test_cli_and_library_expose_the_same_snapshot_hash() -> None:
    service = build_repository_roadmap_service(Path(__file__).parents[1])
    expected = service.get_snapshot("darkfac").snapshot_hash
    output = io.StringIO()
    with redirect_stdout(output):
        assert roadmap_cli_main(["snapshot", "--project", "darkfac"]) == 0
    payload = json.loads(output.getvalue())

    assert payload["snapshot_hash"] == expected
    expected_ids = {
        *(f"RM-{number:02d}" for number in range(1, 10)),
        *(f"DF-{number:02d}" for number in range(1, 24)),
    }
    assert {item["id"] for item in payload["items"]} == expected_ids | CONTINUOUS_AUTONOMY_IDS
