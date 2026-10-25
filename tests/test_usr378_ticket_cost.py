"""USR-378: custo por ticket com tentativas falhas e custo implicito de assinatura."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.usage.models import ModelCallEvent
from core.usage.ticket_cost import (
    cost_by_ticket,
    load_subscription_prices,
    read_ticket_quota_history,
    record_ticket_quota_cost,
)
from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.service import HubService

PRICES = {"codex": 43.45}  # 43.45 / 4.345 = 10 USD por semana


def test_failed_attempt_counts_in_ticket_cost() -> None:
    records = [
        {"ticket_id": "T-1", "harness": "codex", "success": False, "weekly_delta": 5.0},
        {"ticket_id": "T-1", "harness": "codex", "success": True, "weekly_delta": 5.0},
    ]
    [row] = cost_by_ticket(records, PRICES)
    assert (row.attempts, row.failed_attempts) == (2, 1)
    assert row.implicit_cost_usd == pytest.approx(1.0)


def test_implicit_cost_uses_weekly_delta_with_legacy_fallback() -> None:
    records = [
        {"ticket_id": "T-2", "harness": "codex", "weekly_delta": 10.0, "delta_headroom": 99.0},
        {"ticket_id": "T-2", "harness": "codex", "delta_headroom": 20.0},  # linha antiga
        {"ticket_id": "T-2", "kind": "api_call", "harness": "openrouter", "api_cost_usd": 0.5},
    ]
    [row] = cost_by_ticket(records, PRICES)
    assert row.implicit_cost_usd == pytest.approx(3.0)
    assert row.api_cost_usd == pytest.approx(0.5)
    assert row.total_cost_usd == pytest.approx(3.5)


def test_record_without_ticket_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        record_ticket_quota_cost("", "codex", 90.0, 88.0, reports_dir=tmp_path)
    row = record_ticket_quota_cost("T-3", "codex", 90.0, 88.0, success=False, reports_dir=tmp_path)
    assert read_ticket_quota_history(tmp_path)[0]["attempt_id"] == row["attempt_id"]
    assert cost_by_ticket([{"harness": "codex", "weekly_delta": 5.0}], PRICES) == []
    assert load_subscription_prices().get("claude", 0) > 0


def test_ticket_costs_endpoint_returns_aggregation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    service = HubService(data_dir=tmp_path)
    monkeypatch.setattr(HubService, "_ticket_reports_dir", staticmethod(lambda: tmp_path / "reports"))
    record_ticket_quota_cost("T-4", "codex", 90.0, 80.0, success=False, reports_dir=tmp_path / "reports")
    service.model_usage_ledger.record(
        ModelCallEvent(provider="openrouter", model="m-1", ticket_id="T-4", cost_usd=0.25, source="test")
    )
    app.dependency_overrides[get_hub_service] = lambda: service
    try:
        body = TestClient(app).get("/api/usage/ticket-costs").json()
    finally:
        app.dependency_overrides.clear()
    [row] = [r for r in body if r["ticket_id"] == "T-4"]
    assert row["failed_attempts"] == 1
    assert row["api_cost_usd"] == pytest.approx(0.25)
    assert row["implicit_cost_usd"] > 0
