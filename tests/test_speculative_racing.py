"""
Automated tests for Speculative Model Racing, Frontier Proximity Index,
Epsilon-Dominance, Thinking Budget Curves, and Empirical Tournaments.
"""

import os
import json
import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from core.benchmarks.models import (
    ModelBenchmarkEntry,
    SpeculativeCandidate,
    SpeculativeRaceResult,
    ModelTier,
    TaskComplexity,
)
from core.benchmarks.frontier import (
    compute_pareto_frontier,
    compute_frontier_proximity_indices,
    get_top_candidates_for_tier,
    evaluate_reasoning_effort,
)
from core.benchmarks.racing import (
    EmpiricalBenchmarkLedger,
    SpeculativeRacingEngine,
)
from core.router.model_router import recommend_model
from hub.backend.main import app


@pytest.fixture
def mock_models():
    """Provides a synthetic set of benchmark models spanning different trade-off points."""
    return [
        ModelBenchmarkEntry(
            model_id="fast-drafter-mini",
            name="Fast Drafter Mini",
            provider="openai",
            context_length=128000,
            input_cost_per_m=0.10,
            output_cost_per_m=0.40,
            cost_per_task=0.0035,
            coding_score=78.0,
            intelligence_score=80.0,
            output_speed_tps=160.0,
            latency_ttft_sec=0.25,
            tokens_per_task=1500,
            tier=ModelTier.FAST_ECONOMY,
        ),
        ModelBenchmarkEntry(
            model_id="balanced-luna",
            name="Balanced Luna",
            provider="openai",
            context_length=200000,
            input_cost_per_m=0.20,
            output_cost_per_m=1.20,
            cost_per_task=0.0080,
            coding_score=87.5,
            intelligence_score=88.0,
            output_speed_tps=120.0,
            latency_ttft_sec=0.35,
            tokens_per_task=2000,
            tier=ModelTier.FRONTIER_HIGH,
        ),
        ModelBenchmarkEntry(
            model_id="near-challenger-pro",
            name="Near Challenger Pro",
            provider="minimax",
            context_length=1000000,
            input_cost_per_m=0.22,
            output_cost_per_m=1.25,
            cost_per_task=0.0086,
            coding_score=87.0,
            intelligence_score=87.5,
            output_speed_tps=110.0,
            latency_ttft_sec=0.38,
            tokens_per_task=2000,
            tier=ModelTier.FRONTIER_HIGH,
        ),
        ModelBenchmarkEntry(
            model_id="heavy-astra-leader",
            name="Heavy Astra Leader",
            provider="openai",
            context_length=200000,
            input_cost_per_m=10.0,
            output_cost_per_m=50.0,
            cost_per_task=0.375,
            coding_score=91.0,
            intelligence_score=93.0,
            output_speed_tps=65.0,
            latency_ttft_sec=1.1,
            tokens_per_task=2500,
            tier=ModelTier.FRONTIER_HIGH,
        ),
        ModelBenchmarkEntry(
            model_id="dominated-legacy",
            name="Dominated Legacy",
            provider="other",
            context_length=32000,
            input_cost_per_m=5.0,
            output_cost_per_m=15.0,
            cost_per_task=0.1625,
            coding_score=75.0,
            intelligence_score=76.0,
            output_speed_tps=50.0,
            latency_ttft_sec=1.5,
            tokens_per_task=2000,
            tier=ModelTier.BALANCED_MID,
        ),
        ModelBenchmarkEntry(
            model_id="ollama/qwen-local",
            name="Local Qwen",
            provider="ollama",
            context_length=32000,
            input_cost_per_m=0.0,
            output_cost_per_m=0.0,
            cost_per_task=0.0,
            coding_score=70.0,
            intelligence_score=72.0,
            output_speed_tps=85.0,
            latency_ttft_sec=0.1,
            tokens_per_task=1500,
            tier=ModelTier.LOCAL_ZERO_COST,
        ),
    ]


def test_frontier_proximity_and_epsilon_gap_calculations(mock_models):
    """Validates FPI calculation, epsilon gaps, and near-Pareto identification."""
    compute_frontier_proximity_indices(mock_models, metric="coding_score")

    model_by_id = {m.model_id: m for m in mock_models}

    # Frontier models must have FPI = 1.0 (100%), eps gaps = 0
    luna = model_by_id["balanced-luna"]
    astra = model_by_id["heavy-astra-leader"]
    assert luna.frontier_proximity_index == 1.0
    assert luna.epsilon_gap_cost == 0.0
    assert luna.epsilon_gap_capability == 0.0
    assert not luna.is_near_pareto  # already optimal

    assert astra.frontier_proximity_index == 1.0

    # Near-challenger must have high proximity (>= 0.90) and opportunity score
    challenger = model_by_id["near-challenger-pro"]
    assert challenger.frontier_proximity_index >= 0.90
    assert challenger.opportunity_score > 0.90
    assert challenger.epsilon_gap_capability >= 0.0

    # Dominated model must have lower FPI and positive epsilon gaps
    dominated = model_by_id["dominated-legacy"]
    assert dominated.frontier_proximity_index < 0.85
    assert dominated.epsilon_gap_capability > 0.0
    assert dominated.epsilon_gap_cost > 0.0
    assert not dominated.is_near_pareto


def test_top3_candidate_selection_by_tier(mock_models):
    """Validates that Top 3 selection assigns complementary roles (Drafter, Challenger, Arbiter)."""
    candidates_high = get_top_candidates_for_tier(mock_models, tier="high", k=3)
    assert len(candidates_high) == 3

    roles = {c.role for c in candidates_high}
    assert "fast_drafter" in roles
    assert "balanced_challenger" in roles
    assert "frontier_arbiter" in roles

    # Arbiter must be the highest capability
    arbiter = next(c for c in candidates_high if c.role == "frontier_arbiter")
    assert arbiter.coding_score >= 87.0

    # Drafter must have low cost or high speed
    drafter = next(c for c in candidates_high if c.role == "fast_drafter")
    assert drafter.cost_per_task <= 0.01 or drafter.output_speed_tps >= 100.0


def test_reasoning_effort_scaling(mock_models):
    """Validates thinking budget curve scaling for reasoning models."""
    luna = mock_models[1]  # Balanced Luna
    c_low, s_low = evaluate_reasoning_effort(luna, "low")
    c_med, s_med = evaluate_reasoning_effort(luna, "medium")
    c_high, s_high = evaluate_reasoning_effort(luna, "high")
    c_max, s_max = evaluate_reasoning_effort(luna, "max")

    # Cost must scale monotonically: low < medium < high < max
    assert c_low < c_med < c_high < c_max
    # Score must scale monotonically
    assert s_low <= s_med <= s_high <= s_max
    assert s_max <= 100.0


def test_speculative_race_execution_and_savings():
    """Validates that speculative cascading achieves cost and latency savings when drafter passes."""
    temp_dir = Path(tempfile.mkdtemp(prefix="speculative_test_"))
    try:
        ledger = EmpiricalBenchmarkLedger(file_path=temp_dir / "empirical_ledger.json")
        engine = SpeculativeRacingEngine(ledger=ledger)

        # Drafter passes verification
        race = engine.execute_speculative_race(
            task_id="test_task_01",
            task_prompt="Write verified binary search",
            complexity="high",
            simulate_drafter_success_rate=1.0,
        )

        assert race.winner_role == "fast_drafter"
        assert not race.escalation_occurred
        assert race.verification_passed is True
        assert race.cost_saved_usd >= 0.0
        assert race.total_cost_usd > 0.0

        # Verify ledger updated
        winner_stat = ledger.get_or_create_stats(race.winner_model_id)
        assert winner_stat.total_tasks_run == 1
        assert winner_stat.successful_tasks == 1
        assert winner_stat.pass_rate_at_1 == 1.0
    finally:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_speculative_race_escalation_on_failure():
    """Validates that if drafter fails deterministic check, task escalates to arbiter."""
    temp_dir = Path(tempfile.mkdtemp(prefix="speculative_test_"))
    try:
        ledger = EmpiricalBenchmarkLedger(file_path=temp_dir / "empirical_ledger.json")
        engine = SpeculativeRacingEngine(ledger=ledger)

        # Custom validator: drafter fails, arbiter passes
        def custom_validator(model_id: str):
            if "flash" in model_id or "fast" in model_id:
                return False, {"error": "syntax_error_in_drafter"}
            return True, {"verified": True}

        race = engine.execute_speculative_race(
            task_id="test_task_escalate",
            task_prompt="Complex AST transform",
            complexity="high",
            validator_func=custom_validator,
        )

        assert race.escalation_occurred is True
        assert race.winner_role == "frontier_arbiter"
        assert race.verification_passed is True
        assert "escalated_to" in race.verification_details
    finally:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_empirical_tournament_and_elo():
    """Validates running an empirical tournament, Elo updating, and JSON serialization."""
    temp_dir = Path(tempfile.mkdtemp(prefix="speculative_test_"))
    try:
        ledger = EmpiricalBenchmarkLedger(file_path=temp_dir / "empirical_ledger.json")
        engine = SpeculativeRacingEngine(ledger=ledger)

        tournament = engine.run_empirical_tournament(complexity="high")
        assert "leaderboard" in tournament
        assert "races" in tournament
        assert len(tournament["races"]) > 0

        # Check that empirical ledger file exists
        assert (temp_dir / "empirical_ledger.json").exists()

        # Reload from disk
        reloaded = EmpiricalBenchmarkLedger(file_path=temp_dir / "empirical_ledger.json")
        assert len(reloaded.stats) > 0
        assert len(reloaded.history) > 0
    finally:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_model_router_speculative_fields():
    """Validates model_router includes speculative_top3 candidate models."""
    rec = recommend_model("coding", complexity="high", offline=False)
    assert "daily_efficiency_frontier" in rec
    frontier = rec["daily_efficiency_frontier"]
    assert "speculative_top3" in frontier
    top3 = frontier["speculative_top3"]
    assert len(top3) == 3
    assert all("role" in c for c in top3)


def test_rest_api_speculative_and_proximity_endpoints():
    """Validates DarkHub REST endpoints for proximity, top3, empirical stats, and speculative race."""
    client = TestClient(app)

    # 1. Proximity endpoint
    resp_prox = client.get("/api/benchmarks/proximity")
    assert resp_prox.status_code == 200
    pdata = resp_prox.json()
    assert "models" in pdata
    assert any("frontier_proximity_index" in m for m in pdata["models"])

    # 2. Top3 speculative endpoint
    resp_top3 = client.get("/api/benchmarks/speculative/top3?tier=high")
    assert resp_top3.status_code == 200
    top3_data = resp_top3.json()
    assert len(top3_data) == 3
    assert any(c["role"] == "frontier_arbiter" for c in top3_data)

    # 3. Empirical stats endpoint
    resp_emp = client.get("/api/benchmarks/empirical")
    assert resp_emp.status_code == 200
    emp_data = resp_emp.json()
    assert "leaderboard" in emp_data

    # 4. Speculative race execution endpoint
    resp_race = client.post("/api/benchmarks/speculative/race?task_id=api_test&complexity=high")
    assert resp_race.status_code == 200
    race_data = resp_race.json()
    assert "race_id" in race_data
    assert "winner_model_id" in race_data
    assert "verification_passed" in race_data
