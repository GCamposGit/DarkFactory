"""Comprehensive automated test suite for INFRA-11:
Painel Unificado de Métricas de Infraestrutura no DarkHub (Hardware & Contêineres).

Validates:
1. Pydantic v2 schemas for HardwareMetrics, ContainersMetrics, and InfraMetricsReport.
2. Host hardware telemetry collection via psutil and fault tolerance.
3. Container and orchestrated workload discovery (Dokploy PaaS & Docker).
4. REST API endpoints: GET /api/infra/metrics and POST /api/infra/metrics/refresh.
5. Integration with DarkHub frontend assets (infra.js).
6. Non-redundancy with USR-15 (cards and metrics co-exist cleanly).
"""

from __future__ import annotations

import json
import socket
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.infra.metrics import (
    ContainerStatusItem,
    ContainersMetrics,
    HardwareMetrics,
    InfraMetricsReport,
    NodeLiveMetrics,
    SecondaryDiskMetrics,
    build_infra_metrics_report,
    collect_desktop_hardware_node,
    collect_containers_metrics,
    collect_host_hardware_metrics,
    get_dokploy_credentials,
)
from core.harness.remote_worker import create_worker_app
from hub.backend.main import app


@pytest.fixture
def client() -> TestClient:
    """FastAPI TestClient fixture."""
    return TestClient(app)


def _sample_hardware(node_id: str = "onprem-z97-server") -> HardwareMetrics:
    return HardwareMetrics(
        node_id=node_id,
        cpu_percent=24.0,
        cpu_cores_logical=16,
        cpu_cores_physical=8,
        ram_total_gb=32.0,
        ram_used_gb=12.0,
        ram_free_gb=20.0,
        ram_percent=37.5,
        disk_primary_mount="C:\\",
        disk_total_gb=1000.0,
        disk_used_gb=400.0,
        disk_free_gb=600.0,
        disk_percent=40.0,
        boot_time="2026-09-01T00:00:00Z",
        uptime_seconds=3600.0,
        os_platform="Windows-11",
    )


def _hardware_response(hardware: HardwareMetrics) -> MagicMock:
    response = MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = None
    response.read.return_value = hardware.model_dump_json().encode("utf-8")
    return response


# ==============================================================================
# 1. Pydantic Model Validation Tests
# ==============================================================================


def test_hardware_metrics_model() -> None:
    """Verify HardwareMetrics model instantiates and validates field types."""
    sec = SecondaryDiskMetrics(
        mount_point="E:\\",
        total_gb=3000.0,
        used_gb=1200.0,
        free_gb=1800.0,
        percent_used=40.0,
    )
    hw = HardwareMetrics(
        node_id="test-node",
        cpu_percent=25.5,
        cpu_cores_logical=16,
        cpu_cores_physical=8,
        cpu_freq_current_mhz=3200.0,
        ram_total_gb=32.0,
        ram_used_gb=16.0,
        ram_free_gb=16.0,
        ram_percent=50.0,
        disk_primary_mount="C:\\",
        disk_total_gb=500.0,
        disk_used_gb=250.0,
        disk_free_gb=250.0,
        disk_percent=50.0,
        secondary_disks=[sec],
        boot_time="2026-09-01T00:00:00Z",
        uptime_seconds=86400.0,
        os_platform="Windows-11-Test",
    )
    assert hw.node_id == "test-node"
    assert hw.cpu_percent == 25.5
    assert hw.ram_total_gb == 32.0
    assert len(hw.secondary_disks) == 1
    assert hw.secondary_disks[0].mount_point == "E:\\"

    # JSON round-trip
    dumped = hw.model_dump_json()
    reparsed = HardwareMetrics.model_validate_json(dumped)
    assert reparsed.cpu_cores_logical == 16


def test_containers_metrics_model() -> None:
    """Verify ContainersMetrics and ContainerStatusItem model validation."""
    item = ContainerStatusItem(
        name="darkfac-cloud",
        service_type="compose",
        status="done",
        orchestrator="Dokploy PaaS",
        service_id="svc-123",
        node_id="darkfac-vps-primary",
        deployment_title="Production deployment",
    )
    assert item.name == "darkfac-cloud"
    assert item.status == "done"

    containers = ContainersMetrics(
        total_containers=1,
        running_containers=1,
        stopped_containers=0,
        orchestrators_detected=["Dokploy PaaS"],
        items=[item],
        docker_daemon_available=False,
        dokploy_api_connected=True,
    )
    assert containers.total_containers == 1
    assert containers.running_containers == 1
    assert containers.dokploy_api_connected is True


def test_infra_metrics_report_model() -> None:
    """Verify composite InfraMetricsReport model."""
    hw = HardwareMetrics(
        node_id="predator-neo-16",
        cpu_percent=10.0,
        cpu_cores_logical=8,
        cpu_cores_physical=4,
        ram_total_gb=16.0,
        ram_used_gb=8.0,
        ram_free_gb=8.0,
        ram_percent=50.0,
        disk_primary_mount="C:\\",
        disk_total_gb=256.0,
        disk_used_gb=128.0,
        disk_free_gb=128.0,
        disk_percent=50.0,
        boot_time="2026-09-01T00:00:00Z",
        uptime_seconds=3600.0,
        os_platform="Windows-11",
    )
    containers = ContainersMetrics(
        total_containers=2,
        running_containers=2,
        stopped_containers=0,
        orchestrators_detected=["Dokploy PaaS"],
    )
    report = InfraMetricsReport(
        version="1.0.0",
        host_node_id="predator-neo-16",
        host_hardware=hw,
        containers=containers,
        overall_health="healthy",
        summary="All systems nominal",
    )
    assert report.version == "1.0.0"
    assert report.overall_health == "healthy"
    assert report.host_hardware.ram_total_gb == 16.0


# ==============================================================================
# 2. Hardware Telemetry Collector Tests
# ==============================================================================


def test_collect_host_hardware_metrics_live() -> None:
    """Verify collect_host_hardware_metrics produces realistic data on the current host."""
    hw = collect_host_hardware_metrics(node_id="predator-neo-16")
    assert hw.node_id == "predator-neo-16"
    assert hw.cpu_cores_logical >= 1
    assert hw.ram_total_gb > 0.0
    assert hw.disk_total_gb > 0.0
    assert hw.uptime_seconds >= 0.0
    assert len(hw.os_platform) > 0


def test_collect_host_hardware_metrics_fallback_on_error() -> None:
    """Verify collect_host_hardware_metrics returns a valid model without raising when psutil fails."""
    with patch("psutil.cpu_percent", side_effect=RuntimeError("psutil hardware error")):
        hw = collect_host_hardware_metrics(node_id="fallback-node")
        assert hw.node_id == "fallback-node"
        assert hw.cpu_percent == 0.0
        assert hw.ram_total_gb == 0.0


def test_collect_desktop_hardware_metrics_online(monkeypatch: pytest.MonkeyPatch) -> None:
    """Desktop worker telemetry is validated and exposed as a live host node."""
    import core.infra.metrics as infra_metrics

    monkeypatch.setattr(infra_metrics, "_desktop_last_contact_at", None)
    sample = _sample_hardware()
    with patch("core.infra.metrics.urllib.request.urlopen", return_value=_hardware_response(sample)) as open_url:
        node = collect_desktop_hardware_node()

    assert node.node_id == "onprem-z97-server"
    assert node.node_name == "Desktop (desktop-g45ipem)"
    assert node.status == "healthy"
    assert node.is_live is True
    assert node.hardware is not None and node.hardware.cpu_percent == 24.0
    assert node.last_contact_at is not None
    assert open_url.call_args.kwargs["timeout"] <= 0.75


def test_collect_desktop_hardware_metrics_offline_keeps_last_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable Desktop is offline and carries the last successful contact time."""
    import core.infra.metrics as infra_metrics

    monkeypatch.setattr(infra_metrics, "_desktop_last_contact_at", None)
    with patch("core.infra.metrics.urllib.request.urlopen", return_value=_hardware_response(_sample_hardware())):
        online = collect_desktop_hardware_node()
    assert online.last_contact_at is not None

    with patch("core.infra.metrics.urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
        offline = collect_desktop_hardware_node()

    assert offline.status == "offline"
    assert offline.is_live is False
    assert offline.hardware is None
    assert offline.last_contact_at == online.last_contact_at


def test_collect_desktop_hardware_metrics_timeout_is_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker read timeout degrades to an offline node without fabricated values."""
    import core.infra.metrics as infra_metrics

    monkeypatch.setattr(infra_metrics, "_desktop_last_contact_at", None)
    with patch("core.infra.metrics.urllib.request.urlopen", side_effect=socket.timeout("worker timed out")):
        node = collect_desktop_hardware_node(timeout=0.2)

    assert node.status == "offline"
    assert node.is_live is False
    assert node.hardware is None
    assert node.last_contact_at is None


def test_collect_desktop_hardware_metrics_rejects_wrong_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable worker must not be shown as Desktop when it reports another host."""
    import core.infra.metrics as infra_metrics

    monkeypatch.setattr(infra_metrics, "_desktop_last_contact_at", None)
    with patch("core.infra.metrics.urllib.request.urlopen", return_value=_hardware_response(_sample_hardware("other-host"))):
        node = collect_desktop_hardware_node()

    assert node.status == "offline"
    assert node.hardware is None
    assert node.last_contact_at is None


def test_remote_worker_hardware_metrics_endpoint() -> None:
    """The worker exposes the shared headless hardware collector over HTTP."""
    sample = _sample_hardware(node_id="desktop-g45ipem")
    worker_client = TestClient(create_worker_app(node_id="desktop-g45ipem"))
    with patch("core.harness.remote_worker.collect_host_hardware_metrics", return_value=sample) as collect:
        response = worker_client.get("/metrics/hardware")

    assert response.status_code == 200
    assert response.json()["node_id"] == "desktop-g45ipem"
    assert response.json()["cpu_percent"] == 24.0
    collect.assert_called_once_with(node_id="desktop-g45ipem")


# ==============================================================================
# 3. Container Metrics Collector Tests
# ==============================================================================


def test_collect_containers_metrics_mocked_dokploy() -> None:
    """Verify container discovery parses Dokploy project/compose/application payloads."""
    mock_dokploy_response = [
        {
            "name": "darkfac-core",
            "environments": [
                {
                    "name": "production",
                    "compose": [
                        {"name": "darkfac-cloud", "composeId": "c1", "composeStatus": "done"},
                        {"name": "Darkhub", "composeId": "c2", "composeStatus": "done"},
                    ],
                    "applications": [
                        {"name": "darkfac-canary", "applicationId": "a1", "applicationStatus": "done"},
                    ],
                }
            ],
        }
    ]

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(mock_dokploy_response).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = collect_containers_metrics(
            timeout=1.0,
            credentials_override=("https://fake-dokploy.local", "fake-token"),
        )
        assert res.dokploy_api_connected is True
        assert res.total_containers == 3
        assert res.running_containers == 3
        assert "Dokploy PaaS" in res.orchestrators_detected
        names = [item.name for item in res.items]
        assert "darkfac-cloud" in names
        assert "Darkhub" in names
        assert "darkfac-canary" in names


def test_collect_containers_metrics_offline_fallback() -> None:
    """Verify collect_containers_metrics degrades gracefully when external Dokploy is unreachable."""
    with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
        res = collect_containers_metrics(
            timeout=0.1,
            credentials_override=("https://unreachable.invalid", "fake-token"),
        )
        # Must return registered catalog fallbacks without raising
        assert res.total_containers >= 4
        names = [item.name for item in res.items]
        assert "darkfac-cloud" in names
        assert "Darkhub" in names


# ==============================================================================
# 4. Report Builder & Health State Tests
# ==============================================================================


def test_build_infra_metrics_report_overall_health() -> None:
    """Verify health categorization thresholds (healthy vs warning vs critical)."""
    # 1. Normal usage -> healthy
    with patch("core.infra.metrics.collect_host_hardware_metrics") as mock_hw, \
         patch("core.infra.metrics.collect_containers_metrics") as mock_c, \
         patch("core.infra.metrics.collect_desktop_hardware_node") as mock_desktop:
        mock_hw.return_value = HardwareMetrics(
            node_id="test", cpu_percent=20.0, cpu_cores_logical=8, cpu_cores_physical=4,
            ram_total_gb=16.0, ram_used_gb=8.0, ram_free_gb=8.0, ram_percent=50.0,
            disk_primary_mount="C:\\", disk_total_gb=200.0, disk_used_gb=50.0, disk_free_gb=150.0, disk_percent=25.0,
            boot_time="2026-09-01T00:00:00Z", uptime_seconds=100.0, os_platform="Win"
        )
        mock_c.return_value = ContainersMetrics(total_containers=2, running_containers=2, stopped_containers=0, dokploy_api_connected=True)
        mock_desktop.return_value = NodeLiveMetrics(
            node_id="onprem-z97-server", node_name="Desktop", role="on_prem_server",
            is_live=True, status="healthy", hardware=_sample_hardware(),
        )
        r = build_infra_metrics_report()
        assert r.overall_health == "healthy"

    # 2. Critical RAM usage (> 92%) -> critical
    with patch("core.infra.metrics.collect_host_hardware_metrics") as mock_hw, \
         patch("core.infra.metrics.collect_containers_metrics") as mock_c, \
         patch("core.infra.metrics.collect_desktop_hardware_node") as mock_desktop:
        mock_hw.return_value = HardwareMetrics(
            node_id="test", cpu_percent=20.0, cpu_cores_logical=8, cpu_cores_physical=4,
            ram_total_gb=16.0, ram_used_gb=15.5, ram_free_gb=0.5, ram_percent=96.0,
            disk_primary_mount="C:\\", disk_total_gb=200.0, disk_used_gb=50.0, disk_free_gb=150.0, disk_percent=25.0,
            boot_time="2026-09-01T00:00:00Z", uptime_seconds=100.0, os_platform="Win"
        )
        mock_c.return_value = ContainersMetrics(total_containers=2, running_containers=2, stopped_containers=0, dokploy_api_connected=True)
        mock_desktop.return_value = NodeLiveMetrics(
            node_id="onprem-z97-server", node_name="Desktop", role="on_prem_server",
            is_live=True, status="healthy", hardware=_sample_hardware(),
        )
        r = build_infra_metrics_report()
        assert r.overall_health == "critical"


def test_build_infra_metrics_report_degrades_when_desktop_is_offline() -> None:
    """Overall health reflects the Desktop host state even when the Notebook is healthy."""
    healthy_notebook = _sample_hardware(node_id="predator-neo-16")
    containers = ContainersMetrics(total_containers=0, running_containers=0, stopped_containers=0)
    desktop = NodeLiveMetrics(
        node_id="onprem-z97-server", node_name="Desktop", role="on_prem_server",
        is_live=False, status="offline", hardware=None,
    )
    with patch("core.infra.metrics.collect_host_hardware_metrics", return_value=healthy_notebook), \
         patch("core.infra.metrics.collect_containers_metrics", return_value=containers), \
         patch("core.infra.metrics.collect_desktop_hardware_node", return_value=desktop):
        report = build_infra_metrics_report()

    assert report.overall_health == "degraded"
    assert next(node for node in report.nodes if node.role == "on_prem_server").status == "offline"


# ==============================================================================
# 5. REST API Endpoints Reachability Tests
# ==============================================================================


def test_api_get_infra_metrics(client: TestClient) -> None:
    """Verify GET /api/infra/metrics returns HTTP 200 and matches InfraMetricsReport contract."""
    resp = client.get("/api/infra/metrics")
    assert resp.status_code == 200
    data = resp.json()

    assert "host_hardware" in data
    assert "containers" in data
    assert "nodes" in data
    assert "overall_health" in data
    assert "summary" in data

    hw = data["host_hardware"]
    assert "cpu_percent" in hw
    assert "ram_percent" in hw
    assert "disk_percent" in hw
    assert hw["ram_total_gb"] > 0

    containers = data["containers"]
    assert "total_containers" in containers
    assert "items" in containers
    assert isinstance(containers["items"], list)
    desktop = next(node for node in data["nodes"] if node["role"] == "on_prem_server")
    assert desktop["node_name"] == "Desktop (desktop-g45ipem)"
    assert desktop["status"] in {"healthy", "warning", "critical", "offline"}
    assert "last_contact_at" in desktop


def test_api_get_infra_metrics_when_desktop_times_out(client: TestClient) -> None:
    """The public report remains available without invented Desktop readings."""
    with patch("core.infra.metrics.urllib.request.urlopen", side_effect=socket.timeout("Desktop offline")):
        response = client.get("/api/infra/metrics?force=true&timeout=0.1")

    assert response.status_code == 200
    report = response.json()
    desktop = next(node for node in report["nodes"] if node["role"] == "on_prem_server")
    assert desktop["status"] == "offline"
    assert desktop["hardware"] is None
    assert report["overall_health"] in {"degraded", "warning", "critical"}


def test_api_post_infra_metrics_refresh(client: TestClient) -> None:
    """Verify POST /api/infra/metrics/refresh triggers a live re-probe and returns HTTP 200."""
    resp = client.post("/api/infra/metrics/refresh?timeout=1.0")
    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == "1.0.0"
    assert "host_hardware" in data


# ==============================================================================
# 6. Non-Redundancy & Coexistence with USR-15 Cards
# ==============================================================================


def test_non_redundancy_cards_and_metrics_coexist(client: TestClient) -> None:
    """Ensure both /api/infra/cards (USR-15) and /api/infra/metrics (INFRA-11) operate without interference."""
    cards_resp = client.get("/api/infra/cards")
    assert cards_resp.status_code == 200
    cards_data = cards_resp.json()
    assert "cards" in cards_data
    assert "total_monthly_budget_usd" in cards_data

    metrics_resp = client.get("/api/infra/metrics")
    assert metrics_resp.status_code == 200
    metrics_data = metrics_resp.json()
    assert "host_hardware" in metrics_data
    assert "containers" in metrics_data


# ==============================================================================
# 7. Frontend Assets Integration Tests
# ==============================================================================


def test_frontend_infra_metrics_assets() -> None:
    """Verify hub/frontend/infra.js defines the live metrics panel and rendering methods."""
    root_dir = Path(__file__).resolve().parent.parent
    js_path = root_dir / "hub" / "frontend" / "infra.js"

    assert js_path.exists(), "hub/frontend/infra.js must exist"
    content = js_path.read_text(encoding="utf-8")

    assert "/api/infra/metrics" in content, "infra.js must fetch /api/infra/metrics"
    assert "infra-metrics-panel" in content, "infra.js must define #infra-metrics-panel container"
    assert "loadInfraMetrics" in content, "infra.js must declare loadInfraMetrics function"
    assert "renderInfraMetrics" in content, "infra.js must declare renderInfraMetrics function"
    assert "btn-refresh-infra-metrics" in content, "infra.js must mount refresh metrics button"
    assert 'renderHardwareNode(notebookNode, "Notebook")' in content
    assert 'renderHardwareNode(desktopNode, "Desktop")' in content
    assert "Último contato" in content
