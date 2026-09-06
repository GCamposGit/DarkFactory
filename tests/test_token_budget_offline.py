"""Offline token-budget contract regression test."""

from core.router.model_router import recommend_model


def test_offline_recommendation_includes_minimal_modular_budget() -> None:
    recommendation = recommend_model("coding", "high", offline=True)

    assert recommendation["provider"] == "ollama"
    assert recommendation["token_budget"]["pressure"] == "critical"
    assert recommendation["token_budget"]["reasoning_effort"] == "minimal"
    assert recommendation["token_budget"]["modular_delivery"] is True
