"""Tests for Multi-Project Portfolio API and 7 Core Modules (DH-08).

Governed by Universal Engineering Standards (AGENTS.md).
Validates:
1. GET /api/portfolio returns consolidated summaries across all 5 dimensions.
2. GET /api/portfolio/projects/{project_id} returns deep-dive inspection.
3. GET /api/portfolio/efficiency returns worker slots, queues, and budgets.
4. GET /api/portfolio/archetypes returns available project blueprints.
5. Error handling for non-existent projects.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hub.backend.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_get_portfolio_overview(client: TestClient) -> None:
    response = client.get("/api/portfolio")
    assert response.status_code == 200
    data = response.json()

    assert data["total_projects"] >= 4
    assert data["healthy_projects"] + data["warning_projects"] == data["total_projects"]
    assert data["total_budget_limit_usd"] > 0
    assert data["max_heavy_slots"] == 1
    assert data["max_light_slots"] == 4

    # Verify registered projects are present
    project_ids = [p["id"] for p in data["projects"]]
    assert "darkfac" in project_ids
    assert "site-ggcampos" in project_ids
    assert "segundo-cerebro" in project_ids
    assert "jarvis" in project_ids

    # Validate 5 dimensions on a core project
    darkfac = next(p for p in data["projects"] if p["id"] == "darkfac")
    assert darkfac["dev_stage"] == "production"
    assert "health_status" in darkfac
    assert "health_details" in darkfac
    assert darkfac["last_deploy"]["target_type"] in ("dokploy", "dokploy_docker")
    assert "roadmap_summary" in darkfac
    assert darkfac["roadmap_summary"]["total_items"] > 0
    assert "budget_summary" in darkfac
    assert darkfac["budget_summary"]["monthly_limit_usd"] >= 50.0
    assert "adoption_summary" in darkfac
    assert darkfac["adoption_summary"]["is_adopted"] is True
    assert "pilots_summary" in darkfac
    assert "line_summary" in darkfac
    assert darkfac["line_summary"]["active_stages"] == [
        "grill",
        "planning",
        "build",
        "review",
        "integration",
        "release",
    ]
    assert "game_summary" in darkfac
    assert darkfac["game_summary"]["engine"] == "echo-garden"

    # Validate archetype catalog
    arch_ids = [a["id"] for a in data["archetypes"]]
    assert "personal_presence" in arch_ids
    assert "internal_tool" in arch_ids
    assert "second_brain" in arch_ids


def test_get_portfolio_project_detail_success(client: TestClient) -> None:
    response = client.get("/api/portfolio/projects/darkfac")
    assert response.status_code == 200
    data = response.json()

    assert data["project"]["id"] == "darkfac"
    assert "commands" in data
    assert "setup" in data["commands"]
    assert "validate" in data["commands"]
    assert "smoke_checks" in data
    assert "verification" in data
    assert "roadmap_health" in data


def test_portfolio_project_detail_http_has_no_attribute_error(client: TestClient) -> None:
    portfolio_response = client.get("/api/portfolio")
    assert portfolio_response.status_code == 200
    project_ids = [project["id"] for project in portfolio_response.json()["projects"]]
    assert "pilot-hello" in project_ids
    assert "darkfac" in project_ids
    valid_health_statuses = {"healthy", "warning", "stale", "unknown", "error"}

    for project_id in project_ids:
        response = client.get(f"/api/portfolio/projects/{project_id}")
        assert response.status_code == 200, f"{project_id}: {response.text}"
        detail = response.json()
        project = detail["project"]
        assert "error" not in project["health_details"], project_id
        assert project["health_status"] in valid_health_statuses, project_id
        assert "error" not in detail["roadmap_health"], project_id
        assert "has no attribute" not in str(detail["roadmap_health"]), project_id


def test_get_portfolio_project_detail_not_found(client: TestClient) -> None:
    response = client.get("/api/portfolio/projects/non-existent-project-xyz")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_get_portfolio_efficiency(client: TestClient) -> None:
    response = client.get("/api/portfolio/efficiency")
    assert response.status_code == 200
    data = response.json()

    assert data["max_heavy_slots"] == 1
    assert data["max_light_slots"] == 4
    assert isinstance(data["active_heavy_slots"], int)
    assert isinstance(data["active_light_slots"], int)
    assert "queued_jobs_by_project" in data
    assert "starvation_ticks_by_project" in data
    assert len(data["budgets"]) >= 3

    # Check known project budget allocations
    budget_map = {b["project_id"]: b for b in data["budgets"]}
    assert "darkfac" in budget_map
    assert "jarvis" in budget_map
    assert "atrium" in budget_map


def test_get_portfolio_archetypes(client: TestClient) -> None:
    response = client.get("/api/portfolio/archetypes")
    assert response.status_code == 200
    archetypes = response.json()

    assert len(archetypes) >= 3
    arch_map = {a["id"]: a for a in archetypes}

    pp = arch_map["personal_presence"]
    assert pp["kind"] == "personal_presence"
    assert pp["stack"]["framework"] == "Astro 5"
    assert "hostinger_ftp" in pp["stack"]["deployment_targets"]

    it = arch_map["internal_tool"]
    assert it["kind"] == "internal_tool"
    assert "FastAPI" in it["stack"]["framework"]

    sb = arch_map["second_brain"]
    assert sb["kind"] == "second_brain"
    assert "Faster-Whisper" in sb["stack"]["framework"]


def test_core_project_path_falls_back_to_the_service_root_when_the_registered_path_is_missing(tmp_path) -> None:
    """The registry stores the owner's Windows path for `darkfac`; on the Linux cloud worker it does not exist."""
    from types import SimpleNamespace

    from hub.backend.service import HubService

    service = SimpleNamespace(project_root=tmp_path)
    missing = str(tmp_path / "nowhere" / "DarkFac")
    resolve = lambda descriptor: HubService._portfolio_project_path(service, descriptor)  # noqa: E731

    assert resolve(SimpleNamespace(id="darkfac", path=missing)) == tmp_path
    assert resolve(SimpleNamespace(id="darkfac", path=None)) == tmp_path
    assert resolve(SimpleNamespace(id="darkfac", path=str(tmp_path))) == tmp_path
    # other projects keep their registered path (or None) untouched
    assert resolve(SimpleNamespace(id="other", path=missing)) == Path(missing)
    assert resolve(SimpleNamespace(id="other", path=None)) is None


def _summary_with_roadmap_health(monkeypatch, behavior):
    from hub.backend.api import get_hub_service

    service = get_hub_service()
    monkeypatch.setattr(service.roadmap, "get_health", behavior)
    descriptor = service.project_registry.get_project("darkfac")
    return service._build_portfolio_project_summary(descriptor)


def test_portfolio_health_has_no_attribute_error_with_real_roadmap_health(monkeypatch) -> None:
    from datetime import datetime, timezone

    from core.roadmap.models import RoadmapHealth

    def _health(_pid):
        return RoadmapHealth(
            project_id="darkfac", snapshot_id="s", snapshot_hash="h",
            observed_at=datetime.now(timezone.utc), total_items=1, confirmed_items=1,
            blocked_items=0, conflicts=0, warnings=0, stale=False, policy="p",
        )

    summary = _summary_with_roadmap_health(monkeypatch, _health)
    assert "error" not in summary.health_details
    assert "stale" in summary.health_details


def test_portfolio_health_failure_is_not_masked_as_healthy(monkeypatch) -> None:
    def _boom(_pid):
        raise RuntimeError("falha real")

    summary = _summary_with_roadmap_health(monkeypatch, _boom)
    assert summary.health_details.get("error") == "falha real"
    assert summary.health_status != "healthy"


def test_portfolio_snapshot_failure_is_not_masked_as_healthy(monkeypatch) -> None:
    from datetime import datetime, timezone

    from core.roadmap.models import RoadmapHealth
    from hub.backend.api import get_hub_service

    service = get_hub_service()

    def _snapshot_boom(_pid):
        raise RuntimeError("snapshot indisponivel")

    monkeypatch.setattr(service.roadmap, "get_snapshot", _snapshot_boom)
    monkeypatch.setattr(
        service.roadmap,
        "get_health",
        lambda project_id: RoadmapHealth(
            project_id=project_id,
            snapshot_id="s",
            snapshot_hash="h",
            observed_at=datetime.now(timezone.utc),
            total_items=1,
            confirmed_items=1,
            blocked_items=0,
            conflicts=0,
            warnings=0,
            stale=False,
            policy="p",
        ),
    )
    descriptor = service.project_registry.get_project("darkfac")
    summary = service._build_portfolio_project_summary(descriptor)

    assert summary.health_details.get("roadmap_snapshot_error") == "snapshot indisponivel"
    assert summary.health_status == "warning"


def test_portfolio_health_happy_path_keeps_healthy_and_details(monkeypatch) -> None:
    from datetime import datetime, timezone

    from core.roadmap.models import RoadmapHealth

    def _health(project_id):
        return RoadmapHealth(
            project_id=project_id,
            snapshot_id="s",
            snapshot_hash="h",
            observed_at=datetime.now(timezone.utc),
            total_items=1,
            confirmed_items=1,
            blocked_items=0,
            conflicts=0,
            warnings=0,
            stale=False,
            policy="p",
        )

    summary = _summary_with_roadmap_health(monkeypatch, _health)

    assert summary.health_status == "healthy"
    assert {
        "stale",
        "blocked_items",
        "conflicts_count",
        "warnings",
        "sources_unavailable",
    } <= summary.health_details.keys()
    assert "roadmap_snapshot_error" not in summary.health_details
