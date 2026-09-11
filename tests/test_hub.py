"""
Comprehensive automated tests for DarkHub.
Covers headless business logic, Pydantic validation, and REST API endpoints.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from hub.backend.models import (
    ServiceCategory,
    ServiceCreate,
    ServiceItem,
    HealthCheckResult,
    HealthStatus,
    ServiceUpdate,
)
from hub.backend.service import HubService
from hub.backend.main import app
from hub.backend.api import get_hub_service


@pytest.fixture
def temp_service():
    """Creates an isolated HubService in a temporary directory."""
    temp_dir = tempfile.mkdtemp(prefix="darkhub_test_")
    data_dir = Path(temp_dir)

    # Seed initial test data
    default_data = [
        {
            "id": "test-ollama",
            "name": "Local Ollama Test",
            "url": "http://localhost:11434",
            "category": "local_cluster",
            "description": "Local AI cluster for testing",
            "tags": ["local", "test"],
            "color": "#10b981",
            "is_favorite": True,
            "pinned": True,
            "is_local": True,
        },
        {
            "id": "test-claude",
            "name": "Claude Test",
            "url": "https://claude.ai/",
            "category": "llm_chat",
            "description": "Anthropic Claude interface",
            "tags": ["frontier", "claude"],
            "color": "#d97706",
            "is_favorite": False,
            "pinned": False,
            "is_local": False,
        },
    ]

    import json
    (data_dir / "default_services.json").write_text(json.dumps(default_data), encoding="utf-8")
    (data_dir / "default_prompts.json").write_text(json.dumps([
        {
            "id": "p1",
            "title": "Test Prompt",
            "description": "A test prompt",
            "tags": ["test"],
            "content": "Hello {{world}}"
        }
    ]), encoding="utf-8")

    service = HubService(data_dir=data_dir)
    yield service

    # Cleanup
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_list_services_and_filters(temp_service: HubService):
    services = temp_service.list_services()
    assert len(services) == 2

    # Filter by category
    local_services = temp_service.list_services(category=ServiceCategory.LOCAL_CLUSTER)
    assert len(local_services) == 1
    assert local_services[0].id == "test-ollama"

    # Filter by search
    claude_services = temp_service.list_services(search="claude")
    assert len(claude_services) == 1
    assert claude_services[0].id == "test-claude"

    # Filter by favorites
    fav_services = temp_service.list_services(favorites_only=True)
    assert len(fav_services) == 1
    assert fav_services[0].id == "test-ollama"

    # Filter by pinned
    pinned_services = temp_service.list_services(pinned_only=True)
    assert len(pinned_services) == 1
    assert pinned_services[0].id == "test-ollama"


def test_crud_operations(temp_service: HubService):
    # Create
    new_data = ServiceCreate(
        name="ChatGPT Dev",
        url="https://chatgpt.com",
        category=ServiceCategory.LLM_CHAT,
        description="OpenAI official web client",
        tags=["openai", "chat"],
        color="#10a37f",
        is_favorite=False,
        pinned=True,
    )
    created = temp_service.create_service(new_data)
    assert created.id.startswith("chatgpt-dev")
    assert created.name == "ChatGPT Dev"
    assert created.pinned is True

    # Get
    fetched = temp_service.get_service(created.id)
    assert fetched is not None
    assert fetched.url == "https://chatgpt.com"

    # Update
    updated = temp_service.update_service(
        created.id,
        ServiceUpdate(name="ChatGPT Plus", description="Updated description")
    )
    assert updated is not None
    assert updated.name == "ChatGPT Plus"
    assert updated.description == "Updated description"

    # Toggle favorite
    toggled = temp_service.toggle_favorite(created.id)
    assert toggled is not None
    assert toggled.is_favorite is True

    # Delete
    deleted = temp_service.delete_service(created.id)
    assert deleted is True
    assert temp_service.get_service(created.id) is None


def test_local_service_metadata_round_trip(temp_service: HubService, tmp_path: Path) -> None:
    script_path = tmp_path / "start_demo.py"
    script_path.write_text("print('demo')\n", encoding="utf-8")

    created = temp_service.create_service(
        ServiceCreate(
            name="Local Demo",
            url="http://127.0.0.1:9100",
            is_local=True,
            launch_script=str(script_path),
            fallback_urls=["http://127.0.0.1:9101"],
        )
    )

    assert created.launch_script == str(script_path)
    assert created.fallback_urls == ["http://127.0.0.1:9101"]

    restored = temp_service.get_service(created.id)
    assert restored is not None
    assert restored.launch_script == str(script_path)
    assert restored.fallback_urls == ["http://127.0.0.1:9101"]


def test_local_service_launch_uses_declared_script_and_fallback_url(
    temp_service: HubService,
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "start_demo.py"
    script_path.write_text("print('demo')\n", encoding="utf-8")
    created = temp_service.create_service(
        ServiceCreate(
            name="Local Demo",
            url="http://127.0.0.1:9100",
            is_local=True,
            launch_script=str(script_path),
            fallback_urls=["http://127.0.0.1:9101"],
        )
    )
    temp_service.project_root = tmp_path

    primary_offline = HealthCheckResult(
        service_id=created.id,
        url="http://127.0.0.1:9100",
        status=HealthStatus.OFFLINE,
    )
    fallback_offline = HealthCheckResult(
        service_id=created.id,
        url="http://127.0.0.1:9101",
        status=HealthStatus.OFFLINE,
    )
    fallback_online = HealthCheckResult(
        service_id=created.id,
        url="http://127.0.0.1:9101",
        status=HealthStatus.ONLINE,
        status_code=200,
    )
    process = MagicMock(pid=43210)

    with patch.object(
        temp_service,
        "ping_url",
        side_effect=[primary_offline, fallback_offline, primary_offline, fallback_online],
    ), patch("subprocess.Popen", return_value=process) as popen:
        result = temp_service.launch_service(created.id, max_wait_sec=1.0, poll_interval=0.01)

    assert result.status == "online"
    assert result.url == "http://127.0.0.1:9101"
    command = popen.call_args.args[0]
    assert command[0] == sys.executable
    assert command[1] == str(script_path)
    assert "--no-browser" in command
    assert popen.call_args.kwargs["env"]["PYTHONUNBUFFERED"] == "1"


def test_persisted_catalog_migrates_obsolete_launcher_metadata(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    external_script = tmp_path / "external" / "start_demo.py"
    external_script.parent.mkdir()
    external_script.write_text("print('demo')\n", encoding="utf-8")

    default_item = {
        "id": "local-demo",
        "name": "Local Demo",
        "url": "http://127.0.0.1:9100",
        "launch_script": "external/start_demo.py",
        "fallback_urls": ["http://127.0.0.1:9101"],
    }
    import json
    (data_dir / "default_services.json").write_text(json.dumps([default_item]), encoding="utf-8")
    (data_dir / "services.json").write_text(
        json.dumps([{
            **default_item,
            "launch_script": "old_start_demo.py",
            "fallback_urls": [],
        }]),
        encoding="utf-8",
    )

    service = HubService(data_dir=data_dir, project_root=tmp_path)
    migrated = service.get_service("local-demo")

    assert migrated is not None
    assert migrated.launch_script == "external/start_demo.py"
    assert migrated.fallback_urls == []


def test_production_service_open_uses_external_url_without_spawning(
    temp_service: HubService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "start_demo.py"
    script_path.write_text("print('demo')\n", encoding="utf-8")
    created = temp_service.create_service(
        ServiceCreate(
            name="External Demo",
            url="https://demo.example.com",
            launch_script=str(script_path),
        )
    )

    monkeypatch.setenv("DARKHUB_ENV", "production")
    with patch("subprocess.Popen") as popen:
        launch_result = temp_service.launch_service(created.id)
        target_url = temp_service.get_service_launch_target(created.id)

    assert launch_result.status == "external"
    assert launch_result.launched is False
    assert target_url == "https://demo.example.com"
    popen.assert_not_called()


def test_deployment_url_override_is_applied_at_runtime(
    temp_service: HubService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DARKHUB_SERVICE_URL_OVERRIDES",
        '{"test-claude":"https://demo.example.com/claude"}',
    )

    service = temp_service.get_service("test-claude")

    assert service is not None
    assert service.url == "https://demo.example.com/claude"
    assert service.is_local is False


def test_frontend_routes_any_launchable_service_through_open_endpoint() -> None:
    frontend = Path(__file__).parents[1] / "hub" / "frontend" / "app.js"
    content = frontend.read_text(encoding="utf-8")

    assert content.count("Boolean(item.launch_script)") >= 2
    assert "Boolean(selected.launch_script)" in content


def test_prompts_catalog(temp_service: HubService):
    prompts = temp_service.list_prompts()
    assert len(prompts) == 1
    assert prompts[0].id == "p1"

    search_found = temp_service.list_prompts(search="test")
    assert len(search_found) == 1

    search_empty = temp_service.list_prompts(search="nonexistent")
    assert len(search_empty) == 0


def test_ollama_status_schema(temp_service: HubService):
    status = temp_service.get_ollama_status()
    assert hasattr(status, "is_online")
    assert hasattr(status, "models")
    assert isinstance(status.models, list)


def test_api_integration(temp_service: HubService):
    # Override dependency in FastAPI app
    app.dependency_overrides[get_hub_service] = lambda: temp_service
    client = TestClient(app)

    # 1. GET /api/services
    res = client.get("/api/services")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 2

    # 2. POST /api/services
    new_svc = {
        "name": "DeepSeek API",
        "url": "https://platform.deepseek.com",
        "category": "infra_apis",
        "description": "DeepSeek developer console",
        "tags": ["deepseek", "api"],
        "color": "#0284c7",
        "is_favorite": True,
        "pinned": False,
    }
    create_res = client.post("/api/services", json=new_svc)
    assert create_res.status_code == 201
    created_item = create_res.json()
    svc_id = created_item["id"]
    assert created_item["name"] == "DeepSeek API"

    # 3. GET /api/services/{id}
    get_res = client.get(f"/api/services/{svc_id}")
    assert get_res.status_code == 200
    assert get_res.json()["url"] == "https://platform.deepseek.com"

    # 4. PUT /api/services/{id}
    put_res = client.put(f"/api/services/{svc_id}", json={"name": "DeepSeek Platform v2"})
    assert put_res.status_code == 200
    assert put_res.json()["name"] == "DeepSeek Platform v2"

    # 5. POST toggle favorite
    fav_res = client.post(f"/api/services/{svc_id}/toggle-favorite")
    assert fav_res.status_code == 200
    assert fav_res.json()["is_favorite"] is False

    # 6. DELETE /api/services/{id}
    del_res = client.delete(f"/api/services/{svc_id}")
    assert del_res.status_code == 200

    # 7. GET /api/services/{id} -> 404
    get_404 = client.get(f"/api/services/{svc_id}")
    assert get_404.status_code == 404

    # 8. GET /api/prompts
    prompts_res = client.get("/api/prompts")
    assert prompts_res.status_code == 200
    assert len(prompts_res.json()) >= 1

    # Cleanup overrides
    app.dependency_overrides.clear()


def test_openrouter_status_and_key_handling(temp_service: HubService, monkeypatch):
    # Case 1: No key configured
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(temp_service, "get_openrouter_key", lambda: None)
    status_no_key = temp_service.get_openrouter_status()
    assert status_no_key.has_key is False
    assert status_no_key.is_authenticated is False
    assert len(status_no_key.models) >= 3

    # Case 2: Key configured (mocked authentication success)
    monkeypatch.setattr(temp_service, "get_openrouter_key", lambda: "sk-or-v1-mockedkey")
    import unittest.mock as mock

    fake_auth_response = mock.MagicMock()
    fake_auth_response.read.return_value = (
        b'{"data": {"label": "Test Key", "is_free_tier": false, "usage": 1.25}}'
    )
    fake_auth_response.__enter__.return_value = fake_auth_response

    with mock.patch("urllib.request.urlopen", return_value=fake_auth_response):
        status_with_key = temp_service.get_openrouter_status()
        assert status_with_key.has_key is True
        assert status_with_key.is_authenticated is True
        assert status_with_key.key_label == "Test Key"
        assert status_with_key.usage_usd == 1.25


def test_unified_playground_generation(temp_service: HubService, monkeypatch):
    from hub.backend.models import (
        PlaygroundProvider,
        UnifiedGenerateRequest,
        UnifiedGenerateResponse,
    )
    import unittest.mock as mock

    # 1. Test Ollama dispatch (mocked)
    with mock.patch.object(
        temp_service,
        "generate_ollama",
        return_value=mock.MagicMock(response="Resposta do Ollama", model="qwen-fast:latest", done=True, total_duration_ms=45.2),
    ):
        req_ollama = UnifiedGenerateRequest(
            provider=PlaygroundProvider.OLLAMA,
            model="qwen-fast:latest",
            prompt="Escreva um teste",
        )
        res_ollama = temp_service.generate_unified(req_ollama)
        assert isinstance(res_ollama, UnifiedGenerateResponse)
        assert res_ollama.provider == PlaygroundProvider.OLLAMA
        assert res_ollama.response == "Resposta do Ollama"
        assert res_ollama.cost_usd == 0.0

    # 2. Test OpenRouter dispatch (mocked)
    monkeypatch.setattr(temp_service, "get_openrouter_key", lambda: "sk-or-v1-mock")
    fake_chat_resp = mock.MagicMock()
    fake_chat_resp.read.return_value = (
        b'{"model": "anthropic/claude-3.7-sonnet", "choices": [{"message": {"content": "Resposta Claude"}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}'
    )
    fake_chat_resp.__enter__.return_value = fake_chat_resp

    with mock.patch("urllib.request.urlopen", return_value=fake_chat_resp):
        req_or = UnifiedGenerateRequest(
            provider=PlaygroundProvider.OPENROUTER,
            model="anthropic/claude-3.7-sonnet",
            prompt="Explique arquitetura headless",
        )
        res_or = temp_service.generate_unified(req_or)
        assert isinstance(res_or, UnifiedGenerateResponse)
        assert res_or.provider == PlaygroundProvider.OPENROUTER
        assert res_or.response == "Resposta Claude"
        assert res_or.tokens_used == 150
        assert res_or.total_duration_ms is not None


def test_export_and_import_catalog(temp_service: HubService):
    # 1. Export
    exported = temp_service.export_services_data()
    assert exported.version == "1.0"
    assert exported.count == 2
    assert len(exported.services) == 2

    # 2. Import Overwrite
    from hub.backend.models import ImportCatalogRequest, ServiceItem
    new_item = ServiceItem(
        id="imported-item-1",
        name="Imported Tool",
        url="https://imported.dev",
        category=ServiceCategory.CUSTOM,
        description="Newly imported service",
        tags=["imported"],
        color="#8b5cf6",
        is_favorite=True,
        pinned=True,
        is_local=False,
    )
    import_req = ImportCatalogRequest(services=[new_item], merge=False)
    import_res = temp_service.import_services_data(import_req)
    assert import_res.imported_count == 1
    assert import_res.total_count == 1

    services_after = temp_service.list_services()
    assert len(services_after) == 1
    assert services_after[0].id == "imported-item-1"

    # Verify backup was created
    backup_file = temp_service.services_file.with_suffix(".backup.json")
    assert backup_file.exists()

    # 3. Import Merge
    merge_item = ServiceItem(
        id="merged-item-2",
        name="Merged Tool",
        url="https://merged.dev",
        category=ServiceCategory.CUSTOM,
        description="Merged service",
        tags=["merged"],
        color="#3b82f6",
        is_favorite=False,
        pinned=False,
        is_local=False,
    )
    merge_req = ImportCatalogRequest(services=[merge_item], merge=True)
    merge_res = temp_service.import_services_data(merge_req)
    assert merge_res.imported_count == 1
    assert merge_res.total_count == 2

    all_merged = temp_service.list_services()
    assert len(all_merged) == 2


def test_reset_defaults(temp_service: HubService):
    # Alter the catalog
    temp_service.delete_service("test-ollama")
    assert len(temp_service.list_services()) == 1

    # Reset to defaults
    count = temp_service.reset_to_defaults()
    assert count == 2
    assert len(temp_service.list_services()) == 2
    assert temp_service.get_service("test-ollama") is not None


def test_api_playground_and_backup_endpoints(temp_service: HubService, monkeypatch):
    app.dependency_overrides[get_hub_service] = lambda: temp_service
    client = TestClient(app)

    # 1. GET /api/openrouter/status
    monkeypatch.setattr(temp_service, "get_openrouter_key", lambda: None)
    res_or = client.get("/api/openrouter/status")
    assert res_or.status_code == 200
    assert res_or.json()["has_key"] is False

    # 2. GET /api/services/export
    res_exp = client.get("/api/services/export")
    assert res_exp.status_code == 200
    exp_data = res_exp.json()
    assert exp_data["count"] == 2
    assert "services" in exp_data

    # 3. POST /api/services/import
    import_payload = {
        "services": [
            {
                "id": "svc-api-test",
                "name": "API Imported",
                "url": "https://api-imported.com",
                "category": "infra_apis",
                "description": "Via REST endpoint",
                "tags": ["api"],
                "color": "#10b981",
                "is_favorite": False,
                "pinned": False,
                "is_local": False,
            }
        ],
        "merge": False,
    }
    res_imp = client.post("/api/services/import", json=import_payload)
    assert res_imp.status_code == 200
    assert res_imp.json()["total_count"] == 1

    # 4. POST /api/services/reset-defaults
    res_reset = client.post("/api/services/reset-defaults")
    assert res_reset.status_code == 200
    assert res_reset.json()["count"] == 2

    app.dependency_overrides.clear()
