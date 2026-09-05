"""
Automated unit and integration test suite for Task-Adaptive Multi-Domain Benchmark Router.
Tests intent classification, multi-domain Pareto frontier computations, speculative Top-3 candidate selection,
offline fallbacks, and DarkHub REST API endpoints.
"""

import pytest
from fastapi.testclient import TestClient

from core.benchmarks.models import (
    BenchmarkDomain,
    BenchmarkRoutingDecision,
    ModelBenchmarkEntry,
    ModelTier,
    TaskComplexity,
)
from core.benchmarks.frontier import (
    DOMAIN_METADATA,
    compute_pareto_frontier,
    compute_domain_pareto_frontiers,
    get_domain_top3_candidates,
)
from core.benchmarks.fetcher import ensure_daily_benchmark
from core.benchmarks.router import (
    TaskBenchmarkRouter,
    get_benchmark_router,
)
from hub.backend.main import app


@pytest.fixture
def router() -> TaskBenchmarkRouter:
    return get_benchmark_router()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. Intent Classification Tests
# ---------------------------------------------------------------------------

def test_classify_coding_intent(router: TaskBenchmarkRouter):
    prompt = "Fix race condition in thread pool worker and write unit tests for the AST parser"
    domain, conf, explanation = router.classify_intent(prompt)
    assert domain == BenchmarkDomain.CODING
    assert conf >= 0.70
    assert "Software Engineering" in explanation or "coding" in explanation.lower()


def test_classify_web_research_intent(router: TaskBenchmarkRouter):
    prompt = "Perform deep web search and investigate competitive pricing trends across e-commerce sites"
    domain, conf, explanation = router.classify_intent(prompt)
    assert domain == BenchmarkDomain.DEEP_RESEARCH
    assert conf >= 0.75
    assert "Deep Web Research" in explanation


def test_classify_legal_contract_intent(router: TaskBenchmarkRouter):
    prompt = "Review master services agreement and identify indemnification liability and clause compliance"
    domain, conf, explanation = router.classify_intent(prompt)
    assert domain == BenchmarkDomain.LEGAL_CONTRACT
    assert conf >= 0.75
    assert "Legal" in explanation


def test_classify_business_automation_intent(router: TaskBenchmarkRouter):
    prompt = "Automate RPA workflow to extract invoice data, call REST API endpoints, and execute OS tasks"
    domain, conf, explanation = router.classify_intent(prompt)
    assert domain == BenchmarkDomain.BUSINESS_AUTOMATION
    assert conf >= 0.75
    assert "Business Workflow Automation" in explanation


def test_classify_formal_reasoning_intent(router: TaskBenchmarkRouter):
    prompt = "Solve complex mathematical proof using formal symbolic logic and Olympiad geometry"
    domain, conf, explanation = router.classify_intent(prompt)
    assert domain == BenchmarkDomain.FORMAL_REASONING
    assert conf >= 0.75
    assert "Formal Logic" in explanation


def test_classify_audio_intent(router: TaskBenchmarkRouter):
    prompt = "Transcribe dual-channel stereo audio recording from executive call using faster-whisper VAD"
    domain, conf, explanation = router.classify_intent(prompt)
    assert domain == BenchmarkDomain.MULTIMODAL_AUDIO
    assert conf >= 0.75
    assert "Audio" in explanation or "Multimodal" in explanation


def test_classify_fallback_default(router: TaskBenchmarkRouter):
    prompt = "Please proceed with the current step according to instructions"
    domain, conf, explanation = router.classify_intent(prompt)
    assert domain == BenchmarkDomain.CODING
    assert conf >= 0.70
    assert "SWE-bench" in explanation


# ---------------------------------------------------------------------------
# 2. Multi-Domain Pareto Frontier Tests
# ---------------------------------------------------------------------------

def test_compute_domain_pareto_frontiers():
    ledger = ensure_daily_benchmark()
    models = list(ledger.models.values())
    assert len(models) >= 15

    frontiers = compute_domain_pareto_frontiers(models)

    expected_domains = [d.value for d in BenchmarkDomain]
    for dom in expected_domains:
        assert dom in frontiers, f"Missing frontier for domain {dom}"
        assert len(frontiers[dom]) >= 2, f"Domain {dom} should have multiple frontier models"

    legal_frontier = frontiers["legal_contract"]
    legal_model_ids = [m.model_id for m in legal_frontier]
    assert any("claude" in mid or "gpt-5-6" in mid or "minimax" in mid for mid in legal_model_ids)


def test_get_domain_top3_candidates():
    ledger = ensure_daily_benchmark()
    models = list(ledger.models.values())

    top3_reasoning = get_domain_top3_candidates(
        models,
        domain="formal_reasoning",
        complexity="critical",
        offline=False,
    )
    assert len(top3_reasoning) == 3
    roles = [c.role for c in top3_reasoning]
    assert "fast_drafter" in roles
    assert "balanced_challenger" in roles
    assert "frontier_arbiter" in roles

    arbiter = next(c for c in top3_reasoning if c.role == "frontier_arbiter")
    assert arbiter.model_id
    assert arbiter.frontier_proximity >= 0.0

    top3_offline = get_domain_top3_candidates(
        models,
        domain="coding",
        complexity="medium",
        offline=True,
    )
    assert len(top3_offline) >= 1
    assert all(c.cost_per_task == 0.0 for c in top3_offline)


# ---------------------------------------------------------------------------
# 3. End-to-End Routing Tests
# ---------------------------------------------------------------------------

def test_route_task_online(router: TaskBenchmarkRouter):
    decision = router.route_task(
        requirement="Scrape competitors website, browse pagination, and summarize product catalogue",
        complexity="high",
        offline=False,
    )

    assert isinstance(decision, BenchmarkRoutingDecision)
    assert decision.detected_domain == "deep_research"
    assert decision.canonical_benchmark == "GAIA Benchmark & BrowseBench"
    assert decision.domain_score >= 80.0
    assert decision.cost_per_task > 0.0
    assert len(decision.speculative_candidates) == 3


def test_route_task_offline_fallback(router: TaskBenchmarkRouter):
    decision = router.route_task(
        requirement="Analyze legal liability in terms of service",
        complexity="medium",
        offline=True,
    )

    assert decision.detected_domain == "legal_contract"
    assert "LegalBench" in decision.canonical_benchmark
    assert decision.cost_per_task == 0.0
    assert "ollama" in decision.optimal_model_id.lower() or "qwen" in decision.optimal_model_id.lower()


def test_route_task_domain_override(router: TaskBenchmarkRouter):
    decision = router.route_task(
        requirement="Solve mathematical integral formula",
        complexity="low",
        offline=False,
        domain_override="business_automation",
    )

    assert decision.detected_domain == "business_automation"
    assert "OSWorld" in decision.canonical_benchmark
    assert decision.confidence == 1.0



# ---------------------------------------------------------------------------
# 4. REST API Endpoint Tests
# ---------------------------------------------------------------------------

def test_api_list_benchmark_domains(client: TestClient):
    resp = client.get("/api/benchmarks/domains")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 6
    domain_keys = [d["domain_key"] for d in data]
    assert "coding" in domain_keys
    assert "deep_research" in domain_keys
    assert "legal_contract" in domain_keys
    assert "business_automation" in domain_keys
    assert "formal_reasoning" in domain_keys
    assert "multimodal_audio" in domain_keys


def test_api_get_domain_frontier_valid(client: TestClient):
    resp = client.get("/api/benchmarks/domains/formal_reasoning/frontier")
    assert resp.status_code == 200
    data = resp.json()
    assert data["domain"] == "formal_reasoning"
    assert data["canonical_benchmark"] == "AIME 2024/2025 & MATH-500"
    assert data["frontier_count"] >= 3
    assert len(data["models"]) == data["frontier_count"]


def test_api_get_domain_frontier_invalid(client: TestClient):
    resp = client.get("/api/benchmarks/domains/non_existent_domain/frontier")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_api_route_task_endpoint(client: TestClient):
    payload = {
        "requirement": "Execute OS terminal scripts, manipulate desktop windows and automate browser workflows",
        "complexity": "high",
        "offline": False,
    }
    resp = client.post("/api/benchmarks/route-task", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["detected_domain"] == "business_automation"
    assert "OSWorld" in data["canonical_benchmark"]
    assert data["domain_score"] >= 80.0
    assert len(data["speculative_candidates"]) == 3
