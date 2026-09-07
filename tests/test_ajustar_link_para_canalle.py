"""
Automated Test Suite for USR-14:
Ajustar link para Canalleto na home para iniciar .py antes de redirecionar para localhost.

Reachability Contract:
python -m pytest tests/test_ajustar_link_para_canalle.py -v
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from hub.backend.main import app
from hub.backend.models import (
    HealthCheckResult,
    HealthStatus,
    ServiceCreate,
    ServiceItem,
    ServiceLaunchResponse,
    ServiceUpdate,
)
from hub.backend.service import HubService
import run_canaletto


@pytest.fixture
def client() -> TestClient:
    """FastAPI TestClient fixture with redirect following disabled."""
    return TestClient(app)


# =========================================================================
# 1. Model & Catalog Verification
# =========================================================================

def test_service_models_support_launch_script() -> None:
    """Verify that ServiceItem, ServiceCreate, and ServiceUpdate support launch_script."""
    item = ServiceItem(
        id="test-service",
        name="Test Service",
        url="http://127.0.0.1:9090",
        is_local=True,
        launch_script="run_test.py",
    )
    assert item.launch_script == "run_test.py"
    assert item.is_local is True

    create_data = ServiceCreate(
        name="New Service",
        url="http://127.0.0.1:9091",
        launch_script="start.py",
    )
    assert create_data.launch_script == "start.py"

    update_data = ServiceUpdate(launch_script="new_start.py")
    assert update_data.launch_script == "new_start.py"

    response = ServiceLaunchResponse(
        service_id="canaletto-gallery",
        url="http://127.0.0.1:8899",
        status="online",
        launched=True,
        message="Service ready",
    )
    assert response.launched is True
    assert response.status == "online"


def test_services_catalog_contains_canaletto_launch_script() -> None:
    """Verify that services.json and default_services.json have launch_script for canaletto-gallery."""
    root_dir = Path(__file__).resolve().parent.parent
    services_path = root_dir / "hub" / "data" / "services.json"
    default_services_path = root_dir / "hub" / "data" / "default_services.json"

    assert services_path.exists(), "hub/data/services.json must exist"
    with open(services_path, "r", encoding="utf-8") as f:
        services = json.load(f)
    canaletto = next((s for s in services if s.get("id") == "canaletto-gallery"), None)
    assert canaletto is not None, "canaletto-gallery must exist in services.json"
    assert canaletto.get("launch_script") == "run_canaletto.py"
    assert canaletto.get("is_local") is True

    assert default_services_path.exists(), "hub/data/default_services.json must exist"
    with open(default_services_path, "r", encoding="utf-8") as f:
        default_services = json.load(f)
    default_canaletto = next((s for s in default_services if s.get("id") == "canaletto-gallery"), None)
    assert default_canaletto is not None
    assert default_canaletto.get("launch_script") == "run_canaletto.py"


# =========================================================================
# 2. HubService Business Logic Tests
# =========================================================================

def test_hub_service_launch_when_already_online() -> None:
    """If the service is already online, launch_service returns immediately without spawning."""
    service = HubService()

    online_result = HealthCheckResult(
        service_id="canaletto-gallery",
        url="http://127.0.0.1:8899",
        status=HealthStatus.ONLINE,
        latency_ms=1.5,
        status_code=200,
    )

    with patch.object(service, "ping_url", return_value=online_result) as mock_ping, \
         patch("subprocess.Popen") as mock_popen:
        res = service.launch_service("canaletto-gallery")

        assert res.service_id == "canaletto-gallery"
        assert res.url == "http://127.0.0.1:8899"
        assert res.status == "already_running"
        assert res.launched is False
        mock_popen.assert_not_called()
        mock_ping.assert_called_once()


def test_hub_service_launch_spawns_process_when_offline() -> None:
    """When offline, launch_service starts run_canaletto.py and waits for online status."""
    service = HubService()

    offline_result = HealthCheckResult(
        service_id="canaletto-gallery",
        url="http://127.0.0.1:8899",
        status=HealthStatus.OFFLINE,
        latency_ms=None,
    )
    online_result = HealthCheckResult(
        service_id="canaletto-gallery",
        url="http://127.0.0.1:8899",
        status=HealthStatus.ONLINE,
        latency_ms=2.0,
        status_code=200,
    )

    mock_proc = MagicMock()
    mock_proc.pid = 43210

    with patch.object(service, "ping_url", side_effect=[offline_result, online_result]), \
         patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
        res = service.launch_service("canaletto-gallery", max_wait_sec=2.0, poll_interval=0.01)

        assert res.service_id == "canaletto-gallery"
        assert res.status == "online"
        assert res.launched is True
        assert "43210" in res.message

        mock_popen.assert_called_once()
        call_args, call_kwargs = mock_popen.call_args
        cmd = call_args[0]
        assert cmd[0] == sys.executable
        assert "run_canaletto.py" in str(cmd[1])
        assert "--no-browser" in cmd
        assert call_kwargs["env"]["CANALETTO_NO_BROWSER"] == "1"


def test_hub_service_launch_unknown_service_raises_keyerror() -> None:
    """Requesting launch of an unknown service raises KeyError."""
    service = HubService()
    with pytest.raises(KeyError):
        service.launch_service("unknown-service-xyz")


def test_hub_service_launch_unsupported_service_raises_valueerror() -> None:
    """Requesting launch of a service without launch_script raises ValueError."""
    service = HubService()
    with pytest.raises(ValueError, match="does not define a launch_script"):
        service.launch_service("github-dashboard")


def test_hub_service_launch_missing_file_raises_filenotfound() -> None:
    """If launch_script points to a non-existent file, FileNotFoundError is raised."""
    service = HubService()
    fake_item = ServiceItem(
        id="broken-script-service",
        name="Broken Service",
        url="http://127.0.0.1:9999",
        is_local=True,
        launch_script="non_existent_script_987654.py",
    )
    with patch.object(service, "get_service", return_value=fake_item), \
         patch.object(service, "ping_url", return_value=HealthCheckResult(
             service_id="broken-script-service", url="http://127.0.0.1:9999", status=HealthStatus.OFFLINE
         )):
        with pytest.raises(FileNotFoundError):
            service.launch_service("broken-script-service")


def test_hub_service_get_service_launch_target() -> None:
    """get_service_launch_target launches local services and returns their destination URL."""
    service = HubService()
    with patch.object(service, "launch_service") as mock_launch:
        url = service.get_service_launch_target("canaletto-gallery")
        assert url == "http://127.0.0.1:8899"
        mock_launch.assert_called_once_with("canaletto-gallery", max_wait_sec=5.0)

    # For non-local services without launch_script, does not invoke launch_service
    with patch.object(service, "launch_service") as mock_launch:
        url = service.get_service_launch_target("github-dashboard")
        assert url == "https://github.com/"
        mock_launch.assert_not_called()


# =========================================================================
# 3. REST API Endpoint Tests
# =========================================================================

def test_api_post_launch_service_success(client: TestClient) -> None:
    """POST /api/services/canaletto-gallery/launch returns 200 with ServiceLaunchResponse."""
    expected_resp = ServiceLaunchResponse(
        service_id="canaletto-gallery",
        url="http://127.0.0.1:8899",
        status="online",
        launched=True,
        message="Service started",
    )
    with patch("hub.backend.api.HubService.launch_service", return_value=expected_resp):
        res = client.post("/api/services/canaletto-gallery/launch?timeout=2.0")
        assert res.status_code == 200
        data = res.json()
        assert data["service_id"] == "canaletto-gallery"
        assert data["status"] == "online"
        assert data["launched"] is True
        assert data["url"] == "http://127.0.0.1:8899"


def test_api_post_launch_service_not_found(client: TestClient) -> None:
    """POST /api/services/unknown/launch returns 404."""
    res = client.post("/api/services/servico-fantasma/launch")
    assert res.status_code == 404


def test_api_post_launch_service_not_launchable(client: TestClient) -> None:
    """POST /api/services/github-dashboard/launch returns 400 (not configured)."""
    res = client.post("/api/services/github-dashboard/launch")
    assert res.status_code == 400


def test_api_get_open_redirect_endpoint(client: TestClient) -> None:
    """GET /api/services/{id}/open performs auto-launch and returns HTTP 307 Temporary Redirect."""
    with patch("hub.backend.api.HubService.get_service_launch_target", return_value="http://127.0.0.1:8899") as mock_target:
        res = client.get("/api/services/canaletto-gallery/open", follow_redirects=False)
        assert res.status_code == 307
        assert res.headers["location"] == "http://127.0.0.1:8899"
        mock_target.assert_called_once_with(service_id="canaletto-gallery", max_wait_sec=5.0)


def test_api_get_open_redirect_external_service(client: TestClient) -> None:
    """GET /api/services/github-dashboard/open redirects directly to external URL without errors."""
    res = client.get("/api/services/github-dashboard/open", follow_redirects=False)
    assert res.status_code == 307
    assert res.headers["location"] == "https://github.com/"


# =========================================================================
# 4. run_canaletto.py Headless Execution Flags
# =========================================================================

def test_run_canaletto_respects_no_browser_env() -> None:
    """open_browser_delayed must return immediately when CANALETTO_NO_BROWSER=1 is set."""
    with patch.dict(os.environ, {"CANALETTO_NO_BROWSER": "1"}), \
         patch("webbrowser.open") as mock_browser, \
         patch("time.sleep") as mock_sleep:
        run_canaletto.open_browser_delayed("http://127.0.0.1:8899", delay=0.01)
        mock_browser.assert_not_called()
        mock_sleep.assert_not_called()


def test_run_canaletto_respects_no_browser_argv() -> None:
    """open_browser_delayed must return immediately when --no-browser is present in sys.argv."""
    with patch.dict(os.environ, {}, clear=True), \
         patch.object(sys, "argv", ["run_canaletto.py", "--no-browser"]), \
         patch("webbrowser.open") as mock_browser, \
         patch("time.sleep") as mock_sleep:
        run_canaletto.open_browser_delayed("http://127.0.0.1:8899", delay=0.01)
        mock_browser.assert_not_called()
        mock_sleep.assert_not_called()


# =========================================================================
# 5. Frontend Integration Sanity
# =========================================================================

def test_frontend_app_js_contains_launch_route() -> None:
    """Verify that hub/frontend/app.js references the /api/services/.../open endpoint."""
    app_js_path = Path(__file__).resolve().parent.parent / "hub" / "frontend" / "app.js"
    assert app_js_path.exists()
    content = app_js_path.read_text(encoding="utf-8")
    assert "/services/${sanitizeId(item.id)}/open" in content or "/services/${safeId}/open" in content
    assert "data-launchable" in content
    assert "canaletto-gallery" in content


def test_frontend_app_js_title_and_icon_are_clickable_links() -> None:
    """Verify that hub/frontend/app.js links both the card title and icon to targetHref."""
    app_js_path = Path(__file__).resolve().parent.parent / "hub" / "frontend" / "app.js"
    content = app_js_path.read_text(encoding="utf-8")
    assert 'href="${targetHref}"' in content
    assert '<h3 class="font-semibold text-slate-100 text-sm' in content
    assert 'data-launchable="${isLaunchable}"' in content


def test_hub_service_canaletto_fallback_to_port_8900() -> None:
    """When 8899 is unreachable, ping_url for canaletto-gallery checks port 8900."""
    import urllib.error
    service = HubService()

    mock_resp = MagicMock()
    mock_resp.getcode.return_value = 200

    def fake_open(req, *args, **kwargs):
        req_url = req.full_url if hasattr(req, "full_url") else str(req)
        if ":8900" in req_url:
            return mock_resp
        raise urllib.error.URLError("Connection refused on 8899")

    with patch("urllib.request.OpenerDirector.open", side_effect=fake_open):
        result = service.ping_url("canaletto-gallery", "http://127.0.0.1:8899")
        assert result.status == HealthStatus.ONLINE
        assert result.url == "http://127.0.0.1:8900"


def test_hub_service_get_launch_target_dynamic_port() -> None:
    """If launch_service resolves an alternate port (8900), get_service_launch_target returns it."""
    service = HubService()
    alt_response = ServiceLaunchResponse(
        service_id="canaletto-gallery",
        url="http://127.0.0.1:8900",
        status="online",
        launched=True,
        message="Running on alternate port",
    )
    with patch.object(service, "launch_service", return_value=alt_response):
        target_url = service.get_service_launch_target("canaletto-gallery")
        assert target_url == "http://127.0.0.1:8900"

