"""Tests for DH-05 (USR-52): Benchmarks and Routing Consultation in DarkHub.

Validates that:
1. Multi-domain benchmarks, Pareto frontiers, speculative top-3, proximity,
   empirical leaderboard, and routing advisor endpoints respond correctly.
2. The frontend consultation script (benchmarks.js) and modal HTML structures
   exist, follow read-only patterns, and define necessary handlers.
3. DarkHub operates strictly in consultation/simulation mode with no mutative workflow execution.
"""

from pathlib import Path
import pytest
from starlette.testclient import TestClient

from hub.backend.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    return TestClient(app)


def test_benchmarks_domains_endpoint(client):
    response = client.get("/api/benchmarks/domains")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    domain_item = data[0]
    assert "domain_key" in domain_item or "domain" in domain_item


def test_benchmarks_domain_frontier_endpoint(client):
    # Coding domain
    response = client.get("/api/benchmarks/domains/coding/frontier")
    assert response.status_code == 200
    data = response.json()
    assert "domain" in data
    assert data["domain"] == "coding"
    assert "models" in data or "frontier_models" in data
    assert isinstance(data.get("models") or data.get("frontier_models"), list)


def test_benchmarks_speculative_top3_endpoint(client):
    response = client.get("/api/benchmarks/speculative/top3?tier=high")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) <= 3


def test_benchmarks_proximity_endpoint(client):
    response = client.get("/api/benchmarks/proximity")
    assert response.status_code == 200
    data = response.json()
    assert "challengers" in data or "near_pareto" in data or "models" in data or isinstance(data, dict)


def test_benchmarks_empirical_endpoint(client):
    response = client.get("/api/benchmarks/empirical")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)
    assert "leaderboard" in data or "models" in data or "total_runs" in data


def test_benchmarks_route_task_simulation(client):
    payload = {
        "requirement": "Refatorar módulo de pagamentos com validação Pydantic e testes de holdout",
        "complexity": "high",
        "offline": True,
    }
    response = client.post("/api/benchmarks/route-task", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "detected_domain" in data or "selected_domain" in data or "domain" in data
    assert "optimal_model_id" in data or "optimal_model_name" in data or "recommended_model" in data


def test_benchmarks_frontend_modal_dom_elements():
    index_html = (REPO_ROOT / "hub" / "frontend" / "index.html").read_text(encoding="utf-8")
    # Modal tabs exist
    assert 'id="bench-tab-models"' in index_html
    assert 'id="bench-tab-domains"' in index_html
    assert 'id="bench-tab-speculative"' in index_html
    assert 'id="bench-tab-empirical"' in index_html
    assert 'id="bench-tab-router"' in index_html
    # Views exist
    assert 'id="bench-view-domains"' in index_html
    assert 'id="bench-view-speculative"' in index_html
    assert 'id="bench-view-empirical"' in index_html
    assert 'id="bench-view-router"' in index_html
    # Domain select and simulation inputs exist
    assert 'id="bench-domain-select"' in index_html
    assert 'id="bench-router-requirement"' in index_html
    assert 'id="bench-router-complexity"' in index_html
    assert 'id="bench-router-results"' in index_html
    # Script tag is included
    assert 'src="/static/benchmarks.js?v=20260924a"' in index_html


def test_benchmarks_js_is_read_only_and_defines_handlers():
    bench_js = (REPO_ROOT / "hub" / "frontend" / "benchmarks.js").read_text(encoding="utf-8")
    # Verify core handler functions
    assert "function switchBenchmarkTab" in bench_js
    assert "function loadBenchmarkDomains" in bench_js
    assert "function loadDomainFrontier" in bench_js
    assert "function loadSpeculativeAndProximity" in bench_js
    assert "function loadEmpiricalBenchmarks" in bench_js
    assert "function simulateTaskRouting" in bench_js

    # Verify strictly read-only / simulation: no mutations to tickets, runs, or factory config
    assert "/api/demands/tickets" not in bench_js
    assert "/api/cloud/deploy" not in bench_js
    assert "DELETE" not in bench_js
    assert "PATCH" not in bench_js
