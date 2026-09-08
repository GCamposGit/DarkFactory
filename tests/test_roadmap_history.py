"""Regression coverage for RM-09 snapshot history and comparison contracts."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from core.roadmap.cli import main as roadmap_cli_main
from core.roadmap.models import (
    ConfidenceLevel,
    DeliveryStatus,
    LifecycleStage,
    PlanningHorizon,
    RoadmapCandidate,
    RoadmapItemType,
    RoadmapSourceRef,
    RoadmapSourceState,
)
from core.roadmap.compiler import RoadmapCompiler
from core.roadmap.service import build_repository_roadmap_service
from core.roadmap.sources import JsonRoadmapSource, RoadmapSourceResult
from core.roadmap.store import RoadmapSnapshotStore
from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.service import HubService


@dataclass
class MutableRoadmapSource:
    title: str = "First title"
    revision: str = "v1"
    source_id: str = "mutable"
    priority: int = 10

    def read(self, project_id: str) -> RoadmapSourceResult:
        item = RoadmapCandidate(
            id="RM-09",
            project_id=project_id,
            title=self.title,
            description="History fixture",
            item_type=RoadmapItemType.FEATURE,
            lifecycle_stage=LifecycleStage.NEXT_STEPS,
            delivery_status=DeliveryStatus.PLANNED,
            horizon=PlanningHorizon.EXPLORATORY,
            confidence=ConfidenceLevel.HIGH,
            source_refs=[RoadmapSourceRef(
                source_id=self.source_id,
                source_kind="fixture",
                label="Mutable fixture",
                locator="memory://roadmap-history",
            )],
            source_id=self.source_id,
            source_priority=self.priority,
        )
        return RoadmapSourceResult(
            state=RoadmapSourceState(
                source_id=self.source_id,
                label="Mutable fixture",
                source_kind="fixture",
                locator="memory://roadmap-history",
                revision=self.revision,
                content_hash=self.revision,
            ),
            records=[item],
        )


def _write_repository(root: Path, *, title: str = "First title") -> Path:
    manifest = root / ".factory" / "roadmap" / "darkfac.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({
        "project_id": "darkfac",
        "items": [{
            "id": "RM-09",
            "project_id": "darkfac",
            "title": title,
            "description": "Snapshot history",
            "item_type": "feature",
            "lifecycle_stage": "next_steps",
            "delivery_status": "planned",
            "horizon": "exploratory",
            "confidence": "high",
            "dependencies": [],
            "completion_criteria": ["Snapshots can be compared."],
        }],
    }), encoding="utf-8")
    plan = root / "docs" / "DEVELOPMENT_PLAN_2026-09-05.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "| ID | Escopo | Depende | Critério |\n"
        "| --- | --- | --- | --- |\n"
        "| DF-01 | contrato | — | validação |\n",
        encoding="utf-8",
    )
    return manifest


def test_store_retains_versions_and_compares_provenance() -> None:
    source = MutableRoadmapSource()
    store = RoadmapSnapshotStore(history_limit=3)
    compiler = RoadmapCompiler([source])

    first = store.get_or_compile("darkfac", compiler)
    source.title = "Second title"
    source.revision = "v2"
    second = store.get_or_compile("darkfac", compiler)

    history = store.get_history("darkfac")
    assert [entry.snapshot_id for entry in history] == [first.snapshot_id, second.snapshot_id]

    comparison = store.compare("darkfac", first.snapshot_id, second.snapshot_id)
    assert comparison.added_item_ids == []
    assert comparison.removed_item_ids == []
    assert comparison.changed_items[0].item_id == "RM-09"
    assert comparison.changed_items[0].changed_fields == ["title"]
    assert comparison.changed_items[0].before.title == "First title"
    assert comparison.changed_items[0].after.title == "Second title"


def test_json_source_marks_rm_report_as_completion_evidence(tmp_path: Path) -> None:
    manifest = _write_repository(tmp_path)
    reports = tmp_path / ".factory" / "reports"
    reports.mkdir()
    report = reports / "rm-09-history-report.md"
    report.write_text("RM-09 implementation evidence", encoding="utf-8")

    result = JsonRoadmapSource(manifest, evidence_dir=reports).read("darkfac")

    assert result.records[0].delivery_status == DeliveryStatus.COMPLETED
    assert result.records[0].evidence_refs[0].locator.endswith("rm-09-history-report.md")


def test_repository_history_api_and_cli_are_read_only(tmp_path: Path) -> None:
    manifest = _write_repository(tmp_path)
    service = HubService(data_dir=tmp_path / "data", roadmap_root=tmp_path)
    app.dependency_overrides[get_hub_service] = lambda: service
    client = TestClient(app)
    try:
        first_response = client.get("/api/projects/darkfac/roadmap")
        assert first_response.status_code == 200
        first_id = first_response.json()["snapshot_id"]

        manifest.write_text(manifest.read_text(encoding="utf-8").replace("First title", "Second title"), encoding="utf-8")
        service.roadmap.store.clear("darkfac")
        second_response = client.get("/api/projects/darkfac/roadmap")
        assert second_response.status_code == 200
        second_id = second_response.json()["snapshot_id"]

        history_response = client.get("/api/projects/darkfac/roadmap/history")
        assert history_response.status_code == 200
        assert [entry["snapshot_id"] for entry in history_response.json()["snapshots"]] == [first_id, second_id]

        comparison_response = client.get(
            "/api/projects/darkfac/roadmap/history/compare",
            params={"from_snapshot": first_id, "to_snapshot": second_id},
        )
        assert comparison_response.status_code == 200
        assert comparison_response.json()["changed_items"][0]["after"]["title"] == "Second title"
    finally:
        app.dependency_overrides.clear()

    output = io.StringIO()
    with redirect_stdout(output):
        assert roadmap_cli_main(["history", "--project", "darkfac"]) == 0
    payload = json.loads(output.getvalue())
    assert payload["project_id"] == "darkfac"
    assert payload["snapshots"]


def test_repository_status_uses_rm_evidence_and_keeps_rm09_planned(tmp_path: Path) -> None:
    _write_repository(tmp_path)
    reports = tmp_path / ".factory" / "reports"
    reports.mkdir()
    (reports / "rm-07-visual-report.md").write_text("RM-07", encoding="utf-8")
    (reports / "rm-08-scale-report.md").write_text("RM-08", encoding="utf-8")
    (reports / "roadmap-operacional-report.md").write_text(
        "Implementado o painel RM-01–RM-07; RM-09 permanece evolução.",
        encoding="utf-8",
    )
    service = build_repository_roadmap_service(tmp_path)
    snapshot = service.get_snapshot("darkfac")

    item = next(item for item in snapshot.items if item.id == "RM-09")
    assert item.delivery_status == DeliveryStatus.PLANNED
