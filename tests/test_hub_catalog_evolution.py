"""Tests for DarkHub API endpoints for Evolution & Catalog (HF-25)."""

from fastapi.testclient import TestClient
import pytest

from hub.backend.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_api_list_catalog_components(client: TestClient) -> None:
    resp = client.get("/api/catalog/components")
    assert resp.status_code == 200
    data = resp.json()
    assert "components" in data
    assert data["count"] >= 5


def test_api_evolution_status(client: TestClient) -> None:
    resp = client.get("/api/evolution/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_proposals" in data
    assert "active_promotions" in data
