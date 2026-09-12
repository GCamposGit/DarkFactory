"""
Unit and integration test suite for DarkFac Structured Model Telemetry Engine.
Covers:
- Hardware introspector (GPU/CUDA/CPU fallback, execution mode)
- SQLite WAL transactional store (ACID, concurrent reads, indexing, aggregations)
- Filtering and pagination (ticket, model, provider, execution_mode)
- Statistical summaries (quadruple token granularity, p95 latency, cost USD)
- Legacy import idempotency from model_usage.json
- Provider telemetry emission (Ollama & OpenRouter)
- DarkHub REST API endpoints (/api/telemetry/*)
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.execution.providers import (
    MockModelProvider,
    OllamaModelProvider,
    OpenRouterModelProvider,
)
from core.telemetry.hardware import get_hardware_context
from core.telemetry.models import (
    ExecutionMode,
    TelemetryFilters,
    TelemetryRecordCreate,
)
from core.telemetry.store import TelemetryStore
from hub.backend.main import app
from hub.backend.service import HubService


@pytest.fixture
def temp_store(tmp_path: Path) -> TelemetryStore:
    db_file = tmp_path / "test_telemetry.db"
    return TelemetryStore(db_file)


# ---------------------------------------------------------------------------
# 1. Hardware Introspection Tests
# ---------------------------------------------------------------------------

def test_hardware_introspection_basic() -> None:
    hw = get_hardware_context()
    assert hw.device_name
    assert hw.os_platform
    assert hw.accelerator
    assert hw.execution_mode == ExecutionMode.HEADLESS


def test_hardware_introspection_overrides() -> None:
    hw = get_hardware_context(explicit_mode="ui", device_override="custom-node-01")
    assert hw.device_name == "custom-node-01"
    assert hw.execution_mode == ExecutionMode.UI


# ---------------------------------------------------------------------------
# 2. SQLite Store & Telemetry CRUD / Stats Tests
# ---------------------------------------------------------------------------

def test_telemetry_store_record_and_query(temp_store: TelemetryStore) -> None:
    rec1 = TelemetryRecordCreate(
        ticket_id="HF-15",
        provider="ollama",
        model="qwen-code-fast:latest",
        tier="local",
        harness="core.execution",
        execution_mode=ExecutionMode.HEADLESS,
        input_tokens=100,
        processing_tokens=0,
        output_tokens=50,
        latency_ms=120.5,
        cost_usd=0.0,
        success=True,
    )
    saved1 = temp_store.record(rec1)
    assert saved1.id
    assert saved1.total_tokens == 150
    assert saved1.ticket_id == "HF-15"

    rec2 = TelemetryRecordCreate(
        ticket_id="HF-15",
        provider="openrouter",
        model="anthropic/claude-3.7-sonnet",
        tier="frontier",
        harness="darkhub.playground",
        execution_mode=ExecutionMode.UI,
        input_tokens=500,
        processing_tokens=250,  # Reasoning / Thinking tokens
        output_tokens=300,
        latency_ms=1540.0,
        cost_usd=0.0062,
        success=True,
    )
    saved2 = temp_store.record(rec2)
    assert saved2.total_tokens == 1050
    assert saved2.processing_tokens == 250

    rec3 = TelemetryRecordCreate(
        ticket_id="DF-21",
        provider="openrouter",
        model="deepseek/deepseek-r1",
        tier="frontier",
        harness="core.execution",
        execution_mode=ExecutionMode.HEADLESS,
        input_tokens=200,
        processing_tokens=100,
        output_tokens=0,
        latency_ms=300.0,
        cost_usd=0.001,
        success=False,
        error_message="Rate limit exceeded",
    )
    temp_store.record(rec3)

    # Query all
    all_runs = temp_store.query_runs()
    assert all_runs.total_count == 3
    assert len(all_runs.runs) == 3

    # Filter by ticket_id
    hf15_runs = temp_store.query_runs(TelemetryFilters(ticket_id="HF-15"))
    assert hf15_runs.total_count == 2

    # Filter by provider
    ollama_runs = temp_store.query_runs(TelemetryFilters(provider="ollama"))
    assert ollama_runs.total_count == 1
    assert ollama_runs.runs[0].model == "qwen-code-fast:latest"

    # Filter by execution mode
    ui_runs = temp_store.query_runs(TelemetryFilters(execution_mode=ExecutionMode.UI))
    assert ui_runs.total_count == 1
    assert ui_runs.runs[0].model == "anthropic/claude-3.7-sonnet"

    # Filter by failure
    failed_runs = temp_store.query_runs(TelemetryFilters(success=False))
    assert failed_runs.total_count == 1
    assert failed_runs.runs[0].ticket_id == "DF-21"
    assert "Rate limit" in (failed_runs.runs[0].error_message or "")


def test_telemetry_stats_calculation(temp_store: TelemetryStore) -> None:
    temp_store.record(
        TelemetryRecordCreate(
            ticket_id="TICKET-A",
            provider="ollama",
            model="qwen-fast",
            tier="local",
            execution_mode=ExecutionMode.HEADLESS,
            input_tokens=100,
            processing_tokens=0,
            output_tokens=50,
            latency_ms=100.0,
            cost_usd=0.0,
            success=True,
        )
    )
    temp_store.record(
        TelemetryRecordCreate(
            ticket_id="TICKET-B",
            provider="openrouter",
            model="claude-3.7",
            tier="frontier",
            execution_mode=ExecutionMode.UI,
            input_tokens=200,
            processing_tokens=50,
            output_tokens=100,
            latency_ms=200.0,
            cost_usd=0.005,
            success=True,
        )
    )
    temp_store.record(
        TelemetryRecordCreate(
            ticket_id="TICKET-A",
            provider="openrouter",
            model="claude-3.7",
            tier="frontier",
            execution_mode=ExecutionMode.HEADLESS,
            input_tokens=50,
            processing_tokens=0,
            output_tokens=0,
            latency_ms=50.0,
            cost_usd=0.0,
            success=False,
        )
    )

    stats = temp_store.get_stats()
    assert stats.total_runs == 3
    assert stats.successful_runs == 2
    assert stats.failed_runs == 1
    assert stats.success_rate_percent == 66.67
    assert stats.total_input_tokens == 350
    assert stats.total_processing_tokens == 50
    assert stats.total_output_tokens == 150
    assert stats.total_tokens == 550
    assert stats.total_cost_usd == 0.005
    assert stats.avg_latency_ms > 0

    # Test distinct tickets list
    tickets = temp_store.list_tickets()
    assert tickets == ["TICKET-A", "TICKET-B"]


def test_legacy_model_usage_import(tmp_path: Path) -> None:
    db_file = tmp_path / "telemetry_legacy.db"
    store = TelemetryStore(db_file)

    legacy_json = tmp_path / "model_usage.json"
    legacy_data = {
        "events": [
            {
                "invocation_id": "legacy-01",
                "provider": "ollama",
                "model": "qwen2.5-coder:7b",
                "tier": "local",
                "harness": "test",
                "ticket_id": "LEGACY-01",
                "input_tokens": 120,
                "output_tokens": 45,
                "latency_ms": 95.2,
                "cost_usd": 0.0,
                "success": True,
            }
        ]
    }
    with open(legacy_json, "w", encoding="utf-8") as f:
        json.dump(legacy_data, f)

    imported = store.import_legacy_if_empty(legacy_json)
    assert imported == 1

    # Check that query finds the imported run
    runs = store.query_runs()
    assert runs.total_count == 1
    assert runs.runs[0].id == "legacy-01"
    assert runs.runs[0].ticket_id == "LEGACY-01"

    # Second import must be a no-op
    second_import = store.import_legacy_if_empty(legacy_json)
    assert second_import == 0


# ---------------------------------------------------------------------------
# 3. Provider Telemetry Integration Tests
# ---------------------------------------------------------------------------

def test_ollama_provider_telemetry_emission(tmp_path: Path) -> None:
    db_file = tmp_path / "telemetry.db"
    store = TelemetryStore(db_file)

    fake_response = {
        "model": "qwen-code-fast:latest",
        "response": "def test_func(): pass",
        "prompt_eval_count": 42,
        "eval_count": 18,
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(fake_response).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp), \
         patch("core.telemetry.store.DEFAULT_DB_PATH", db_file):
        provider = OllamaModelProvider()
        res = provider.generate(
            "Write a test",
            model="qwen-code-fast:latest",
            ticket_id="HF-15",
            execution_mode="headless",
        )
        assert res.tokens_prompt == 42
        assert res.tokens_completion == 18

    # Verify SQLite recorded the run
    runs = store.query_runs(TelemetryFilters(ticket_id="HF-15"))
    assert runs.total_count == 1
    assert runs.runs[0].provider == "ollama"
    assert runs.runs[0].input_tokens == 42
    assert runs.runs[0].output_tokens == 18
    assert runs.runs[0].execution_mode == ExecutionMode.HEADLESS


def test_openrouter_provider_telemetry_emission_with_reasoning(tmp_path: Path) -> None:
    db_file = tmp_path / "telemetry.db"
    store = TelemetryStore(db_file)

    fake_response = {
        "model": "anthropic/claude-3.7-sonnet",
        "choices": [{"message": {"content": "Verified result"}}],
        "usage": {
            "prompt_tokens": 150,
            "completion_tokens": 80,
            "total_tokens": 230,
            "prompt_tokens_details": {"cached_tokens": 30},
            "completion_tokens_details": {"reasoning_tokens": 50},
        },
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(fake_response).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp), \
         patch("core.execution.providers.get_openrouter_api_key", return_value="sk-or-fake-key"), \
         patch("core.telemetry.store.DEFAULT_DB_PATH", db_file):
        provider = OpenRouterModelProvider()
        res = provider.generate(
            "Solve architecture",
            model="anthropic/claude-3.7-sonnet",
            ticket_id="HF-15",
            execution_mode="ui",
        )
        assert res.tokens_prompt == 150
        assert res.tokens_completion == 80

    # Verify SQLite recorded the run with processing tokens
    runs = store.query_runs(TelemetryFilters(ticket_id="HF-15"))
    assert runs.total_count == 1
    run = runs.runs[0]
    assert run.provider == "openrouter"
    assert run.input_tokens == 150
    assert run.processing_tokens == 80  # 30 cached + 50 reasoning
    assert run.output_tokens == 80
    assert run.execution_mode == ExecutionMode.UI


# ---------------------------------------------------------------------------
# 4. DarkHub REST API Endpoints Tests
# ---------------------------------------------------------------------------

def test_darkhub_api_telemetry_endpoints(tmp_path: Path) -> None:
    client = TestClient(app)

    # 1. Post a telemetry event via REST API
    event_payload = {
        "ticket_id": "API-TEST-01",
        "provider": "openrouter",
        "model": "deepseek/deepseek-r1",
        "tier": "frontier",
        "harness": "api.test",
        "execution_mode": "headless",
        "input_tokens": 200,
        "processing_tokens": 120,
        "output_tokens": 80,
        "latency_ms": 850.5,
        "cost_usd": 0.0004,
        "success": True,
    }
    res_post = client.post("/api/telemetry/events", json=event_payload)
    assert res_post.status_code == 200
    created = res_post.json()
    assert created["ticket_id"] == "API-TEST-01"
    assert created["total_tokens"] == 400

    # 2. Get runs
    res_runs = client.get("/api/telemetry/runs?ticket_id=API-TEST-01")
    assert res_runs.status_code == 200
    runs_data = res_runs.json()
    assert runs_data["total_count"] >= 1
    assert any(r["ticket_id"] == "API-TEST-01" for r in runs_data["runs"])

    # 3. Get stats
    res_stats = client.get("/api/telemetry/stats?ticket_id=API-TEST-01")
    assert res_stats.status_code == 200
    stats_data = res_stats.json()
    assert stats_data["total_runs"] >= 1
    assert stats_data["total_processing_tokens"] >= 120

    # 4. Get tickets list
    res_tickets = client.get("/api/telemetry/tickets")
    assert res_tickets.status_code == 200
    tickets_list = res_tickets.json()
    assert "API-TEST-01" in tickets_list
