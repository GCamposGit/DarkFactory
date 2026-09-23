"""Tests for DH-10 (USR-51): Roadmap Health and History consultation in DarkHub."""

from pathlib import Path
from fastapi.testclient import TestClient

from hub.backend.main import app

client = TestClient(app)


def test_roadmap_frontend_health_and_history_surface() -> None:
    frontend_dir = Path(__file__).parents[1] / "hub" / "frontend"
    index_html = (frontend_dir / "index.html").read_text(encoding="utf-8")
    roadmap_js = (frontend_dir / "roadmap.js").read_text(encoding="utf-8")

    # Verify buttons in index.html
    assert 'id="roadmap-mode-health"' in index_html
    assert 'id="roadmap-mode-history"' in index_html
    assert 'onclick="selectRoadmapMode(\'health\')"' in index_html
    assert 'onclick="selectRoadmapMode(\'history\')"' in index_html

    # Verify view containers in index.html
    assert 'id="roadmap-health-view"' in index_html
    assert 'id="roadmap-history-view"' in index_html
    assert 'id="roadmap-filters-section"' in index_html

    # Verify functions in roadmap.js
    assert "loadRoadmapHealth" in roadmap_js
    assert "renderRoadmapHealth" in roadmap_js
    assert "loadRoadmapHistory" in roadmap_js
    assert "renderRoadmapHistory" in roadmap_js
    assert "executeRoadmapCompare" in roadmap_js
    assert "renderRoadmapComparisonResult" in roadmap_js

    # Verify routes called in roadmap.js
    assert "/roadmap/health" in roadmap_js
    assert "/roadmap/history" in roadmap_js
    assert "/roadmap/history/compare" in roadmap_js

    # Verify strict read-only nature: no roadmap mutations from frontend
    assert "POST /api/projects" not in roadmap_js
    assert "PUT /api/projects" not in roadmap_js
    assert "DELETE /api/projects" not in roadmap_js
    assert "PATCH /api/projects" not in roadmap_js


def test_roadmap_health_api_consultation() -> None:
    response = client.get("/api/projects/darkfac/roadmap/health")
    assert response.status_code == 200
    data = response.json()
    assert data["project_id"] == "darkfac"
    assert "snapshot_id" in data
    assert "snapshot_hash" in data
    assert "total_items" in data
    assert "confirmed_items" in data
    assert "blocked_items" in data
    assert "conflicts" in data
    assert "warnings" in data
    assert "stale" in data
    assert "sources_consulted" in data
    assert "policy" in data


def test_roadmap_history_and_compare_api_consultation() -> None:
    # 1. Fetch history
    history_resp = client.get("/api/projects/darkfac/roadmap/history")
    assert history_resp.status_code == 200
    history_data = history_resp.json()
    assert history_data["project_id"] == "darkfac"
    snapshots = history_data.get("snapshots", [])
    assert len(snapshots) >= 1

    # 2. Compare the latest snapshot with itself (idempotent / clean diff)
    snapshot_id = snapshots[0]["snapshot_id"]
    compare_resp = client.get(
        f"/api/projects/darkfac/roadmap/history/compare?from_snapshot={snapshot_id}&to_snapshot={snapshot_id}"
    )
    assert compare_resp.status_code == 200
    compare_data = compare_resp.json()
    assert compare_data["project_id"] == "darkfac"
    assert compare_data["from_snapshot"]["snapshot_id"] == snapshot_id
    assert compare_data["to_snapshot"]["snapshot_id"] == snapshot_id
    assert compare_data["added_item_ids"] == []
    assert compare_data["removed_item_ids"] == []
    assert compare_data["changed_items"] == []


def test_roadmap_history_compare_invalid_snapshot_404() -> None:
    compare_resp = client.get(
        "/api/projects/darkfac/roadmap/history/compare?from_snapshot=invalid-id-999&to_snapshot=invalid-id-888"
    )
    assert compare_resp.status_code == 404


def test_roadmap_health_unknown_project_404() -> None:
    response = client.get("/api/projects/unknown-proj/roadmap/health")
    assert response.status_code == 404
