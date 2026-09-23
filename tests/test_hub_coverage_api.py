"""Unit and integration tests for DarkHub coverage endpoint (DH-14 / USR-45)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hub.backend.coverage import (
    CoverageSummaryResponse,
    get_coverage_summary,
    load_roadmap_details,
)
from hub.backend.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_hub_coverage_endpoint_returns_200(client: TestClient) -> None:
    res = client.get("/api/hub/coverage")
    assert res.status_code == 200
    data = res.json()

    # Validate against Pydantic schema
    summary = CoverageSummaryResponse.model_validate(data)
    assert 0 <= summary.coverage_percentage <= 100
    assert 0.0 <= summary.coverage_ratio <= 1.0
    assert summary.surfaced > 0
    assert summary.pending > 0
    assert summary.total_owner_facing == summary.surfaced + summary.pending
    assert summary.ok is True
    assert isinstance(summary.problems, list)


def test_hub_coverage_pending_grouped_by_roadmap(client: TestClient) -> None:
    res = client.get("/api/hub/coverage")
    assert res.status_code == 200
    data = res.json()
    summary = CoverageSummaryResponse.model_validate(data)

    assert len(summary.pending_by_roadmap) > 0
    # DH-02 must be in pending items
    assert "DH-02" in summary.pending_by_roadmap
    dh02_items = summary.pending_by_roadmap["DH-02"]
    assert any(it.key == "POST /api/demands/intake" for it in dh02_items)

    # Check roadmap metadata
    assert "DH-02" in summary.roadmap_items
    meta = summary.roadmap_items["DH-02"]
    assert meta.id == "DH-02"
    assert "Intake" in meta.title
    assert meta.horizon == "Agora"


def test_roadmap_details_parser() -> None:
    details = load_roadmap_details()
    assert len(details) >= 15
    for r_id, item in details.items():
        assert r_id.startswith("DH-")
        assert item.title
        assert item.horizon in {"Agora", "Depois", "Futuro"}


def test_coverage_summary_cache_and_force_refresh() -> None:
    first = get_coverage_summary()
    cached = get_coverage_summary()
    assert first is cached  # Exact same cached reference

    refreshed = get_coverage_summary(force_refresh=True)
    assert refreshed is not None
    assert refreshed.surfaced == first.surfaced
