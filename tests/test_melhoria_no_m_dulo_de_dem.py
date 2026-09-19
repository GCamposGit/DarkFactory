"""Reachability and regression tests for USR-AUTO: Demands module improvements.

Validates:
1. Configurable timeout and keep_alive in DemandSpecifier (via constructor, method, and env vars).
2. Robust JSON parsing (_extract_json_object) handling markdown code fences and thought blocks.
3. Precise diagnostic messages differentiating actual timeouts from other network/server errors.
4. Auto ID assignment preventing accidental overwrite when id="USR-AUTO" or generic placeholders are submitted.
5. Strict isolation preventing test execution from polluting production .factory/demands/demands.json.
6. Integrity and preservation of real user demands (USR-09, USR-12, USR-AUTO).
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.demands.models import DemandInput, DemandOrigin, UserTicket, TAG_USER_DEMAND
from core.demands.service import DemandsService
from core.demands.specifier import (
    DemandSpecifier,
    HeuristicDemandSpecifier,
    _extract_json_object,
)
from core.demands.store import DemandsStore
from core.roadmap.models import DeliveryStatus
from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.service import HubService


def test_specifier_timeout_and_keep_alive_configuration():
    """Verify that timeout and keep_alive can be configured via constructor and env vars."""
    with patch.dict(os.environ, {"DEMANDS_OLLAMA_TIMEOUT": "75.0", "DEMANDS_OLLAMA_KEEP_ALIVE": "15m"}):
        from importlib import reload
        import core.demands.specifier as spec_module
        reload(spec_module)
        assert spec_module.DEFAULT_OLLAMA_TIMEOUT == 75.0
        assert spec_module.DEFAULT_OLLAMA_KEEP_ALIVE == "15m"

        specifier = spec_module.DemandSpecifier()
        assert specifier.timeout == 75.0
        assert specifier.keep_alive == "15m"

    # Direct override in constructor
    custom = DemandSpecifier(timeout=120.0, keep_alive="45m")
    assert custom.timeout == 120.0
    assert custom.keep_alive == "45m"


def test_extract_json_object_resilience():
    """Verify JSON extractor parses markdown blocks, whitespace, and thought tags."""
    # 1. Plain JSON
    assert _extract_json_object('{"readiness_score": 90}') == {"readiness_score": 90}

    # 2. Markdown fence with json tag
    fenced = '```json\n{"readiness_score": 85, "title": "Teste"}\n```'
    assert _extract_json_object(fenced) == {"readiness_score": 85, "title": "Teste"}

    # 3. Model thought tokens (<think>...</think>)
    with_thought = '<think>Analisei a demanda e conclui que é viável.</think>\n```json\n{"readiness_score": 80}\n```'
    assert _extract_json_object(with_thought) == {"readiness_score": 80}

    # 4. Invalid input raises ValueError
    with pytest.raises(ValueError):
        _extract_json_object("Nenhum json aqui")


def test_specifier_error_differentiation():
    """Verify that timeouts are distinguished from connection errors in suggestions."""
    specifier = DemandSpecifier(timeout=30.0)
    inp = DemandInput(title="Demanda de teste para timeout")

    # Mock Ollama model availability
    with patch.object(specifier, "get_available_local_model", return_value="qwen-fast:latest"):
        # Case A: TimeoutError
        with patch.object(specifier, "_call_ollama", side_effect=TimeoutError("Request timed out")):
            guidance = specifier.guide_demand(inp, timeout=25.0)
            assert any("não respondeu a tempo (timeout=25s)" in s for s in guidance.suggestions)

        # Case B: Connection refused
        with patch.object(specifier, "_call_ollama", side_effect=urllib.error.URLError("Connection refused [WinError 10061]")):
            guidance = specifier.guide_demand(inp)
            assert any("Ollama local inacessível" in s for s in guidance.suggestions)


def test_auto_id_allocation_prevents_usr_auto_overwrite():
    """Verify that saving a new ticket with id='USR-AUTO' assigns the next ID without overwriting."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = Path(tmpdir) / "demands.json"
        store = DemandsStore(store_path)
        service = DemandsService(store=store, specifier=DemandSpecifier(heuristic_specifier=HeuristicDemandSpecifier()))

        # Save an original ticket with USR-AUTO
        original = UserTicket(
            id="USR-AUTO",
            project_id="darkfac",
            title="Demanda Original Importante",
            problem_statement="Problema original",
        )
        service.create_ticket(original)
        assert store.get_ticket("USR-AUTO").title == "Demanda Original Importante"

        # Now save another ticket also using USR-AUTO with a DIFFERENT title (e.g. from UI without guidance)
        new_ticket = UserTicket(
            id="USR-AUTO",
            project_id="darkfac",
            title="Segunda Demanda Criada",
            problem_statement="Outro problema",
        )
        saved = service.create_ticket(new_ticket)

        # It must NOT overwrite USR-AUTO, but allocate next sequential ID (e.g. USR-01)
        assert saved.id != "USR-AUTO"
        assert saved.id == "USR-01"
        assert saved.title == "Segunda Demanda Criada"

        # The original USR-AUTO must remain intact
        reloaded_original = store.get_ticket("USR-AUTO")
        assert reloaded_original is not None
        assert reloaded_original.title == "Demanda Original Importante"


def test_api_guide_timeout_query_param():
    """Verify /api/demands/guide accepts timeout query parameter and routes it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_hub_service = HubService(data_dir=Path(tmpdir))
        app.dependency_overrides[get_hub_service] = lambda: test_hub_service
        try:
            client = TestClient(app)
            payload = {
                "project_id": "darkfac",
                "title": "Verificar parâmetro de timeout",
                "problem_statement": "Garantir que timeout flui pela API",
            }
            # Testing force_heuristic works with timeout parameter
            resp = client.post("/api/demands/guide?force_heuristic=true&timeout=120", json=payload)
            assert resp.status_code == 200
            guidance = resp.json()
            assert guidance["readiness_score"] > 0
            assert guidance["suggested_ticket"] is not None
            assert guidance["suggested_ticket"]["id"] == "USR-01"
        finally:
            app.dependency_overrides.clear()


def test_production_demands_integrity_and_no_leakage():
    """Verify the versioned demands snapshot; this is not a probe of a production host."""
    prod_path = Path(__file__).resolve().parents[1] / ".factory" / "demands" / "demands.json"
    assert prod_path.exists(), "Production demands.json must exist"

    store = DemandsStore(prod_path)
    tickets = store.list_tickets()
    ticket_ids = {t.id for t in tickets}

    # Must preserve real tickets
    assert "USR-09" in ticket_ids, "USR-09 (Grill de demandas) must be preserved"
    assert "USR-12" in ticket_ids, "USR-12 (Controle de créditos) must be preserved"
    assert "USR-AUTO" in ticket_ids, "USR-AUTO (Melhoria demandas) must be preserved"

    # Must NOT have the repetitive fake test tickets
    for t in tickets:
        if "webhook de notifica" in t.title.lower():
            pytest.fail(f"Found spurious test ticket in production demands.json: {t.id} - {t.title}")
