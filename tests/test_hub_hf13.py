"""Tests for HF-13: DarkHub Visibility & Queues, 404 diagnosis/fix, and HF/INFRA ingestion."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.paths import project_root
from core.roadmap.cli import main as roadmap_cli_main
from core.roadmap.models import DeliveryStatus, LifecycleStage, PlanningHorizon, RoadmapItemType
from core.roadmap.service import build_repository_roadmap_service
from core.roadmap.sources import HybridWorkflowPlanSource, InfraRoadmapJsonSource
from hub.backend.main import app


def test_hybrid_workflow_plan_source_parses_waves_and_evidence() -> None:
    root = project_root()
    plan_path = root / "docs" / "HYBRID_WORKFLOW_PLAN_2026-09-08.md"
    evidence_dir = root / ".factory" / "reports"

    source = HybridWorkflowPlanSource(plan_path, evidence_dir=evidence_dir)
    result = source.read("darkfac")

    assert result.state.status == "available"
    records_by_id = {rec.id: rec for rec in result.records}

    # Wave 1 check (HF-01 to HF-15)
    for num in range(1, 16):
        hf_id = f"HF-{num:02d}"
        assert hf_id in records_by_id, f"Missing {hf_id} in Wave 1"
        rec = records_by_id[hf_id]
        assert rec.lifecycle_stage == LifecycleStage.EXECUTION
        assert rec.horizon == PlanningHorizon.NOW
        assert "wave-1" in rec.tags

    # Wave 2 check (HF-20 to HF-25)
    for num in range(20, 26):
        hf_id = f"HF-{num:02d}"
        assert hf_id in records_by_id, f"Missing {hf_id} in Wave 2"
        rec = records_by_id[hf_id]
        assert rec.lifecycle_stage == LifecycleStage.FUTURE
        assert rec.horizon == PlanningHorizon.LATER
        assert "wave-2" in rec.tags
        assert any(d.item_id == "HF-15" for d in rec.dependencies)

    # Dependency check on HF-02 and HF-13
    assert any(d.item_id == "HF-01" for d in records_by_id["HF-02"].dependencies)
    assert any(d.item_id == "HF-05" for d in records_by_id["HF-13"].dependencies)

    # Evidence and completed status check on HF-01 and HF-02
    assert records_by_id["HF-01"].delivery_status == DeliveryStatus.COMPLETED
    assert len(records_by_id["HF-01"].evidence_refs) > 0

    assert records_by_id["HF-02"].delivery_status == DeliveryStatus.COMPLETED
    assert len(records_by_id["HF-02"].evidence_refs) > 0


def test_infra_roadmap_json_source_parses_items_and_prerequisites() -> None:
    root = project_root()
    infra_path = root / ".factory" / "infra" / "roadmap.json"
    evidence_dir = root / ".factory" / "reports"

    source = InfraRoadmapJsonSource(infra_path, evidence_dir=evidence_dir)
    result = source.read("darkfac")

    assert result.state.status == "available"
    records_by_id = {rec.id: rec for rec in result.records}

    # INFRA-01 to INFRA-11 check
    for num in range(1, 12):
        infra_id = f"INFRA-{num:02d}"
        assert infra_id in records_by_id, f"Missing {infra_id}"
        rec = records_by_id[infra_id]
        assert rec.item_type == RoadmapItemType.INFRASTRUCTURE
        assert "infrastructure" in rec.tags

    # Delivered status check
    assert records_by_id["INFRA-01"].delivery_status == DeliveryStatus.COMPLETED
    assert records_by_id["INFRA-02"].delivery_status == DeliveryStatus.COMPLETED
    assert records_by_id["INFRA-08"].delivery_status == DeliveryStatus.COMPLETED
    assert records_by_id["INFRA-09"].delivery_status == DeliveryStatus.PLANNED

    # Prerequisites check
    assert any(d.item_id == "INFRA-05" for d in records_by_id["INFRA-06"].dependencies)
    assert any(d.item_id == "INFRA-06" for d in records_by_id["INFRA-07"].dependencies)
    assert any(d.item_id == "INFRA-07" for d in records_by_id["INFRA-08"].dependencies)


def test_roadmap_service_compiles_unified_snapshot_with_hf_and_infra() -> None:
    root = project_root()
    service = build_repository_roadmap_service(
        root,
        include_demands=True,
        include_hf=True,
        include_infra=True,
    )

    snapshot = service.get_snapshot("darkfac")
    item_ids = {item.id for item in snapshot.items}

    # RM tickets
    for num in range(1, 10):
        assert f"RM-{num:02d}" in item_ids
    # DF tickets
    for num in range(1, 24):
        assert f"DF-{num:02d}" in item_ids
    # HF tickets
    for num in range(1, 16):
        assert f"HF-{num:02d}" in item_ids
    for num in range(20, 26):
        assert f"HF-{num:02d}" in item_ids
    # INFRA tickets
    for num in range(1, 12):
        assert f"INFRA-{num:02d}" in item_ids

    sources_consulted = {s.source_id for s in snapshot.sources_consulted}
    assert "approved-roadmap" in sources_consulted
    assert "development-plan" in sources_consulted
    assert "hybrid-workflow-plan-wave-1" in sources_consulted
    assert "infra-roadmap-json" in sources_consulted


def test_darkhub_http_endpoints_serve_hf_infra_and_prevent_404() -> None:
    client = TestClient(app)

    # 1. Verify /api/projects/darkfac/roadmap includes HF and INFRA items
    roadmap_resp = client.get("/api/projects/darkfac/roadmap")
    assert roadmap_resp.status_code == 200
    roadmap_data = roadmap_resp.json()
    item_ids = {item["id"] for item in roadmap_data["items"]}
    assert "HF-13" in item_ids
    assert "HF-01" in item_ids
    assert "INFRA-01" in item_ids
    assert "INFRA-07" in item_ids
    assert "DF-21" in item_ids

    # 2. Verify all tasks dashboard endpoint variants prevent 404
    routes_to_test = [
        "/api/tasks/dashboard",
        "/api/tasks/dashboard/",
        "/tasks/dashboard",
        "/tasks/dashboard/",
        "/api/tasks",
        "/api/tasks/",
        "/tasks",
        "/tasks/",
    ]
    for route in routes_to_test:
        resp = client.get(route)
        assert resp.status_code == 200, f"Route {route} returned {resp.status_code} instead of 200"
        payload = resp.json()
        assert "queue" in payload
        assert "queued_count" in payload
        assert "sources" in payload


def test_roadmap_cli_snapshot_with_all_flag() -> None:
    output = io.StringIO()
    with redirect_stdout(output):
        exit_code = roadmap_cli_main(["snapshot", "--project", "darkfac", "--all"])
    assert exit_code == 0
    payload = json.loads(output.getvalue())
    item_ids = {item["id"] for item in payload["items"]}
    assert "HF-13" in item_ids
    assert "INFRA-01" in item_ids
    assert "RM-01" in item_ids
    assert "DF-21" in item_ids
