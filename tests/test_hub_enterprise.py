"""Tests for DarkHub Enterprise Profile API endpoints (HF-24)."""

from fastapi.testclient import TestClient
import pytest

from hub.backend.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_api_get_enterprise_status(client: TestClient) -> None:
    resp = client.get("/api/enterprise/status?project=darkfac")
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] == "darkfac"
    assert "config" in data
    assert "sla" in data
    assert "audit_chain" in data


def test_api_verify_enterprise_audit(client: TestClient) -> None:
    resp = client.post("/api/enterprise/verify-audit", json={"project": "darkfac"})
    assert resp.status_code == 200
    data = resp.json()
    assert "is_valid" in data
    assert "total_events" in data
