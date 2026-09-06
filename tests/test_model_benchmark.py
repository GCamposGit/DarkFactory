"""
Comprehensive automated tests for Dark Factory Model Benchmark & Pareto Frontier Engine.
Validates math, Pareto algorithm, daily idempotency, error resilience, router integration, and REST API.
"""

import os
import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient

from core.benchmarks.models import (
    ModelBenchmarkEntry,
    DailyBenchmarkLedger,
    ParetoFrontierSummary,
    ModelTier,
    TaskComplexity,
    FieldProvenance,
    MetricAcquisitionMode,
    MetricQuality,
)
from core.benchmarks.frontier import (
    calculate_task_cost,
    calculate_efficiency_score,
    compute_pareto_frontier,
    build_frontier_summary,
    select_best_model_for_task,
)
from core.benchmarks.fetcher import DailyBenchmarkService, ensure_daily_benchmark
from core.router.model_router import recommend_model
from hub.backend.main import app


def test_task_cost_and_efficiency_calculations():
    # 25,000 prompt tokens @ $1.00/1M = $0.025
    # 2,500 completion tokens @ $4.00/1M = $0.010
    # Expected: $0.035
    cost = calculate_task_cost(input_cost_per_m=1.0, output_cost_per_m=4.0, input_tokens=25000, output_tokens=2500)
    assert cost == 0.035

    # Efficiency score: 84.0 / 0.035 = 2400.0
    eff = calculate_efficiency_score(capability_score=84.0, cost_per_task=cost)
    assert eff == 2400.0

    # Zero cost model ($0.0) should not raise ZeroDivisionError
    eff_zero = calculate_efficiency_score(capability_score=70.0, cost_per_task=0.0)
    assert eff_zero > 0


def test_pareto_frontier_filtering():
    """
    Tests that dominated models (higher cost AND lower/equal score) are pruned.
    """
    models = [
        # Cheap, decent score -> ON FRONTIER
        ModelBenchmarkEntry(
            model_id="model-a",
            name="Model A",
            provider="test",
            context_length=128000,
            input_cost_per_m=0.5,
            output_cost_per_m=1.0,
            cost_per_task=0.02,
            coding_score=80.0,
            intelligence_score=81.0,
            output_speed_tps=100.0,
            latency_ttft_sec=0.5,
            tokens_per_task=2000,
        ),
        # More expensive than A and LOWER score than A -> DOMINATED! Must NOT be on frontier.
        ModelBenchmarkEntry(
            model_id="model-b-dominated",
            name="Model B",
            provider="test",
            context_length=128000,
            input_cost_per_m=1.0,
            output_cost_per_m=2.0,
            cost_per_task=0.04,
            coding_score=78.0,
            intelligence_score=79.0,
            output_speed_tps=80.0,
            latency_ttft_sec=0.7,
            tokens_per_task=2000,
        ),
        # More expensive than A, but higher score -> ON FRONTIER
        ModelBenchmarkEntry(
            model_id="model-c",
            name="Model C",
            provider="test",
            context_length=128000,
            input_cost_per_m=2.0,
            output_cost_per_m=6.0,
            cost_per_task=0.08,
            coding_score=86.0,
            intelligence_score=88.0,
            output_speed_tps=90.0,
            latency_ttft_sec=0.6,
            tokens_per_task=2000,
        ),
        # Most expensive, highest score -> ON FRONTIER
        ModelBenchmarkEntry(
            model_id="model-d",
            name="Model D",
            provider="test",
            context_length=128000,
            input_cost_per_m=5.0,
            output_cost_per_m=20.0,
            cost_per_task=0.20,
            coding_score=91.0,
            intelligence_score=93.0,
            output_speed_tps=70.0,
            latency_ttft_sec=1.1,
            tokens_per_task=2000,
        ),
    ]

    frontier = compute_pareto_frontier(models, metric="coding_score")
    frontier_ids = [m.model_id for m in frontier]

    assert "model-a" in frontier_ids
    assert "model-c" in frontier_ids
    assert "model-d" in frontier_ids
    assert "model-b-dominated" not in frontier_ids


def test_daily_idempotency_and_caching():
    """
    Validates that the service runs on the first call of the day,
    and returns False for should_run_today() afterwards.
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="benchmark_test_"))
    try:
        service = DailyBenchmarkService(workspace_root=temp_dir)
        today = service.get_today_str()

        # Initially, no ledger exists -> should run
        assert service.should_run_today() is True

        # Run benchmark
        ledger = service.build_daily_ledger()
        assert ledger.date == today
        assert service.latest_file.exists()

        # Subsequent call on same day -> must NOT run again
        assert service.should_run_today() is False

        # If date in ledger is set to yesterday -> should run again
        with open(service.latest_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["date"] = "2020-01-01"
        with open(service.latest_file, "w", encoding="utf-8") as f:
            json.dump(data, f)

        assert service.should_run_today() is True
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_fallback_when_network_offline():
    """
    Validates fail-safe behavior: when OpenRouter or network fails,
    system safely constructs ledger from baseline catalog without throwing.
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="benchmark_offline_"))
    try:
        service = DailyBenchmarkService(workspace_root=temp_dir)

        # Mock network error
        with patch.object(service, "fetch_openrouter_catalog", return_value=[]):
            ledger = service.build_daily_ledger()
            assert ledger.total_models_scanned > 0
            assert len(ledger.pareto_coding_models) > 0
            assert service.latest_file.exists()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_select_best_model_by_complexity():
    """Validates model recommendation logic for varying task requirements."""
    models = [
        ModelBenchmarkEntry(
            model_id="ollama/qwen-code-fast:latest",
            name="Qwen Fast",
            provider="ollama",
            context_length=8192,
            input_cost_per_m=0.0,
            output_cost_per_m=0.0,
            cost_per_task=0.0,
            coding_score=68.0,
            intelligence_score=69.0,
            output_speed_tps=85.0,
            latency_ttft_sec=0.1,
            tokens_per_task=1900,
            tier=ModelTier.LOCAL_ZERO_COST,
        ),
        ModelBenchmarkEntry(
            model_id="deepseek/deepseek-v4-pro",
            name="DeepSeek V4 Pro",
            provider="deepseek",
            context_length=256000,
            input_cost_per_m=0.45,
            output_cost_per_m=1.80,
            cost_per_task=0.015,
            coding_score=86.4,
            intelligence_score=88.0,
            output_speed_tps=90.0,
            latency_ttft_sec=0.7,
            tokens_per_task=2400,
            efficiency_score_coding=5760.0,
            tier=ModelTier.BALANCED_MID,
        ),
        ModelBenchmarkEntry(
            model_id="openai/gpt-6-astra",
            name="GPT-6 Astra",
            provider="openai",
            context_length=1000000,
            input_cost_per_m=10.0,
            output_cost_per_m=50.0,
            cost_per_task=0.375,
            coding_score=91.0,
            intelligence_score=93.0,
            output_speed_tps=65.0,
            latency_ttft_sec=1.2,
            tokens_per_task=2500,
            efficiency_score_coding=242.0,
            tier=ModelTier.FRONTIER_HIGH,
        ),
    ]

    # Offline mode must strictly return local Ollama model
    local_rec, _ = select_best_model_for_task(models, "coding", "medium", offline=True)
    assert local_rec.tier == ModelTier.LOCAL_ZERO_COST
    assert local_rec.cost_per_task == 0.0

    # High complexity cloud should pick capable Pareto model
    high_rec, _ = select_best_model_for_task(models, "coding", "high", offline=False)
    assert high_rec.coding_score >= 84.0


def test_model_router_recommend_integration():
    """Validates that model_router.py integrates daily efficiency frontier."""
    rec = recommend_model("coding", complexity="high", offline=False)
    assert "model" in rec
    assert "daily_efficiency_frontier" in rec
    frontier_info = rec["daily_efficiency_frontier"]
    assert "optimal_cloud_model" in frontier_info
    assert frontier_info["coding_score"] > 0


def test_hub_rest_api_benchmarks():
    """Validates FastAPI endpoints for benchmarks in DarkHub."""
    client = TestClient(app)

    resp_latest = client.get("/api/benchmarks/latest")
    assert resp_latest.status_code == 200
    data = resp_latest.json()
    assert "date" in data
    assert "total_models_scanned" in data
    assert "models" in data

    resp_frontier = client.get("/api/benchmarks/frontier")
    assert resp_frontier.status_code == 200
    frontier_data = resp_frontier.json()
    assert "coding_frontier" in frontier_data
    assert len(frontier_data["coding_frontier"]) > 0

    resp_refresh = client.post("/api/benchmarks/refresh")
    assert resp_refresh.status_code == 200
    refreshed = resp_refresh.json()
    assert "date" in refreshed


def test_field_provenance_round_trip_preserves_unknown_values():
    """An absent metric remains unknown instead of receiving a heuristic default."""
    entry = ModelBenchmarkEntry(
        model_id="provider/unknown",
        name="Unknown",
        provider="provider",
        context_length=None,
        input_cost_per_m=0.2,
        output_cost_per_m=0.8,
        cost_per_task=0.007,
        coding_score=None,
        intelligence_score=None,
        output_speed_tps=None,
        latency_ttft_sec=None,
        tokens_per_task=None,
        field_provenance={
            "input_cost_per_m": FieldProvenance(
                source="https://example.test/pricing",
                observed_at="2026-09-06T12:00:00+00:00",
                unit="USD/1M input tokens",
                acquisition_mode=MetricAcquisitionMode.LIVE_API,
                quality=MetricQuality.OBSERVED,
            )
        },
    )

    assert entry.coding_score is None
    assert not entry.is_measured("coding_score")
    assert entry.field_provenance["coding_score"].quality is MetricQuality.UNKNOWN
    restored = ModelBenchmarkEntry.from_dict(entry.to_dict())
    assert restored.coding_score is None
    assert restored.field_provenance["input_cost_per_m"].source.endswith("example.test/pricing")


def test_offline_fixture_is_deterministic_and_never_calls_external_sources():
    with tempfile.TemporaryDirectory(prefix="benchmark_provenance_offline_") as folder:
        service = DailyBenchmarkService(Path(folder))
        with patch.object(service, "fetch_openrouter_catalog", side_effect=AssertionError("network")):
            with patch.object(service, "fetch_artificial_analysis_api", side_effect=AssertionError("network")):
                first = service.build_daily_ledger(offline=True)
                second = service.build_daily_ledger(offline=True)

        assert first.to_dict()["models"] == second.to_dict()["models"]
        assert first.metadata["offline_fixture"] is True
        sample = first.models["anthropic/claude-fable-5-1"]
        provenance = sample.field_provenance["coding_score"]
        assert provenance.source == "Artificial Analysis v4.2 & Coding Agent Index"
        assert provenance.observed_at == "2026-09-04T00:00:00+00:00"
        assert provenance.unit == "score (0-100)"
        assert provenance.acquisition_mode is MetricAcquisitionMode.OFFLINE_FIXTURE
        assert provenance.quality is MetricQuality.REPORTED


def test_openrouter_discovery_does_not_promote_heuristic_scores():
    with tempfile.TemporaryDirectory(prefix="benchmark_provenance_discovery_") as folder:
        service = DailyBenchmarkService(Path(folder))
        discovered = {
            "id": "openai/brand-new-unknown",
            "name": "Brand New Unknown",
            "context_length": 64000,
            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        }
        with patch.object(service, "fetch_artificial_analysis_api", return_value=[]):
            with patch.object(service, "fetch_openrouter_catalog", return_value=[discovered]):
                ledger = service.build_daily_ledger()

        entry = ledger.models["openai/brand-new-unknown"]
        assert entry.coding_score is None
        assert entry.intelligence_score is None
        assert entry.output_speed_tps is None
        assert entry.field_provenance["coding_score"].quality is MetricQuality.UNKNOWN
        assert entry.field_provenance["input_cost_per_m"].acquisition_mode is MetricAcquisitionMode.LIVE_API
        assert "openai/brand-new-unknown" in ledger.metadata["unknown_capability_models"]
        assert "openai/brand-new-unknown" not in ledger.pareto_coding_models


def test_live_price_update_does_not_relabel_baseline_score_source():
    with tempfile.TemporaryDirectory(prefix="benchmark_provenance_merge_") as folder:
        service = DailyBenchmarkService(Path(folder))
        update = {
            "id": "openai/gpt-6-astra",
            "pricing": {"prompt": "0.0000003", "completion": "0.0000012"},
        }
        with patch.object(service, "fetch_artificial_analysis_api", return_value=[]):
            with patch.object(service, "fetch_openrouter_catalog", return_value=[update]):
                ledger = service.build_daily_ledger()

        entry = ledger.models["openai/gpt-6-astra"]
        assert entry.field_provenance["input_cost_per_m"].source == "https://openrouter.ai/api/v1/models"
        assert entry.field_provenance["coding_score"].source == "Artificial Analysis v4.2 & Coding Agent Index"
        assert entry.field_provenance["coding_score"].acquisition_mode is MetricAcquisitionMode.OFFLINE_FIXTURE
