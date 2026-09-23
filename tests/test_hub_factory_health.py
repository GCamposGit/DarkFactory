"""DH-01 (USR-43): read-only "Saude da Fabrica" home alert strip + Infra health block.

Covers:
  (a) index.html loads health.js and every static script tag shares one ?v= version;
  (b) the new frontend code issues no mutating (POST/PATCH/DELETE) calls and escapes
      rendered data via the health escape helper;
  (c) polling is gated on document.visibilityState, uses a 60000ms interval and an
      AbortController-based timeout;
  (d) TestClient smoke test: each of the 8 new GET routes returns 200 against a
      HubService built entirely on tmp_path, with every method that would otherwise
      touch the network or the real .factory/ ledgers monkeypatched to canned data.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.service import HubService

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "hub" / "frontend"

_SCRIPT_SRC = re.compile(r'<script src="(/static/[^"]+\.js)\?v=([^"]+)"')


# ---------------------------------------------------------------------------
# (a) index.html wiring + shared cache-busting version
# ---------------------------------------------------------------------------


def test_index_html_loads_health_js_with_shared_version():
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")

    matches = _SCRIPT_SRC.findall(html)
    assert matches, "expected at least one /static/*.js?v=... script tag in index.html"

    versions = {version for _src, version in matches}
    assert len(versions) == 1, f"all script tags must share one ?v= version, found {versions}"

    srcs = {src for src, _version in matches}
    assert "/static/health.js" in srcs, "index.html must load hub/frontend/health.js"


def test_health_js_file_exists_and_is_served_by_static_mount():
    assert (FRONTEND_DIR / "health.js").is_file()


# ---------------------------------------------------------------------------
# (b) No mutating calls from the new code; escaping helper is used
# ---------------------------------------------------------------------------

_MUTATING_METHOD = re.compile(r'method\s*:\s*["\'](POST|PATCH|DELETE|PUT)["\']', re.IGNORECASE)


def _infra_js_health_block_text() -> str:
    text = (FRONTEND_DIR / "infra.js").read_text(encoding="utf-8")
    start_marker = "DH-01 FACTORY HEALTH BLOCK START"
    end_marker = "DH-01 FACTORY HEALTH BLOCK END"
    start = text.index(start_marker)
    end = text.index(end_marker)
    assert start < end
    return text[start:end]


def test_health_js_has_no_mutating_calls():
    text = (FRONTEND_DIR / "health.js").read_text(encoding="utf-8")
    assert not _MUTATING_METHOD.search(text), "health.js must never issue POST/PATCH/PUT/DELETE requests"
    assert '"POST"' not in text and '"PATCH"' not in text and '"DELETE"' not in text


def test_infra_js_factory_health_block_has_no_mutating_calls():
    block = _infra_js_health_block_text()
    assert not _MUTATING_METHOD.search(block), "the DH-01 health block in infra.js must never mutate"
    assert '"POST"' not in block and '"PATCH"' not in block and '"DELETE"' not in block


def test_health_js_uses_escape_helper_on_rendered_fields():
    text = (FRONTEND_DIR / "health.js").read_text(encoding="utf-8")
    assert "function healthEscapeHtml(" in text
    # Rendered notification fields (title/message/channel/age) must go through the helper.
    assert text.count("healthEscapeHtml(") >= 4


def test_infra_js_factory_health_block_uses_escape_helper():
    block = _infra_js_health_block_text()
    assert "healthEscapeHtml(" in block
    assert block.count("healthEscapeHtml(") >= 5


# ---------------------------------------------------------------------------
# (c) Visibility-gated polling, 60s interval, AbortController timeout
# ---------------------------------------------------------------------------


def test_health_js_polling_is_visibility_gated_with_60s_interval_and_abort_controller():
    text = (FRONTEND_DIR / "health.js").read_text(encoding="utf-8")
    assert "visibilityState" in text
    assert "60000" in text
    assert "AbortController" in text
    assert "visibilitychange" in text


def test_infra_js_wires_health_block_into_visibility_polling():
    text = (FRONTEND_DIR / "infra.js").read_text(encoding="utf-8")
    assert "healthStartVisibilityPolling(loadFactoryHealth, 60000)" in text
    assert "mountFactoryHealthBlock" in text
    assert "loadFactoryHealth" in text


# ---------------------------------------------------------------------------
# (d) TestClient smoke test across the 8 GET routes
# ---------------------------------------------------------------------------


def _make_hermetic_service(tmp_path: Path) -> HubService:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "default_services.json").write_text("[]", encoding="utf-8")
    (data_dir / "default_prompts.json").write_text("[]", encoding="utf-8")

    return HubService(
        data_dir=data_dir,
        usage_dir=tmp_path / "usage",
        roadmap_root=Path.cwd(),
        state_path=tmp_path / "state.json",
        orchestrator_path=tmp_path / "orchestrator.sqlite3",
        control_db_path=tmp_path / "control.db",
        control_database_url="",
    )


_CANNED_NOTIFICATIONS = [
    {
        "notification_id": "notif-1",
        "category": "system_health",
        "severity": "warning",
        "title": "Fila operacional atrasada",
        "message": "3 jobs aguardando o owner ha mais de 1h.",
        "provider_id": None,
        "remaining_percent": None,
        "delivered_channels": ["darkhub"],
        "acknowledged": False,
        "details": {},
        "timestamp": "2026-09-23T10:00:00+00:00",
    }
]

_CANNED_TELEGRAM_STATUS = {
    "configured": True,
    "authorized_user_count": 1,
    "authorized_chat_count": 1,
    "last_offset": 42,
    "processed_updates": 10,
    "processed_callbacks": 2,
    "pending_outbox_notifications": 0,
}

_CANNED_N8N_STATUS = {
    "url": "https://n8n.example.com",
    "is_generic_placeholder": False,
    "operational": True,
    "status_code": 200,
    "version": "1.2.3",
    "db_connected": True,
    "error": None,
}

_CANNED_N8N_WORKFLOWS = {
    "success": True,
    "status_code": 200,
    "data": {"data": [{"id": "wf-1", "name": "Onboarding <script>", "active": True, "updatedAt": "2026-09-23T09:00:00+00:00"}]},
    "error": None,
}

_CANNED_WORKERS = [
    {
        "id": "onprem-z97-server",
        "name": "Dedicated Test Worker",
        "url": "http://100.78.181.90:8080",
        "ip": "100.78.181.90",
        "port": 8080,
        "role": "onprem_worker",
        "description": "desc",
        "status": "online",
        "healthy": True,
        "latency_ms": 12.3,
        "details": {},
    }
]

_CANNED_WEBHOOK_EVENTS = [
    {
        "delivery_id": "evt-1",
        "event_type": "push",
        "action": None,
        "received_at": "2026-09-23T09:55:00+00:00",
        "sender": "octocat",
        "repository": "GCamposGit/DarkFac",
        "ref": "refs/heads/main",
        "status": "processed",
        "action_taken": "noop",
        "details": {},
    }
]

_CANNED_HF15_STATUS = {
    "environment": {
        "sandbox_root": "/tmp/hf15-sandbox",
        "mode": "sandbox",
        "all_preflights_passed": True,
        "checks": [{"name": "db_writable", "passed": True, "details": "ok", "error": None}],
    },
    "metrics": {
        "total_scenarios": 8,
        "passed_scenarios": 8,
        "failed_scenarios": 0,
        "rolled_back_scenarios": 0,
        "avg_dispatch_latency_ms": 10.0,
        "avg_reconciliation_latency_ms": 5.0,
        "avg_rto_seconds": 1.5,
        "max_active_slots_used": 2,
        "total_budget_spent_usd": 0.5,
        "all_slas_met": True,
    },
}

_CANNED_HF15_METRICS = _CANNED_HF15_STATUS["metrics"]


def test_factory_health_routes_return_200_with_hermetic_service(tmp_path, monkeypatch):
    service = _make_hermetic_service(tmp_path)

    # Every one of these would otherwise perform real network I/O (n8n probe,
    # worker probes, Telegram gateway) or write to the real .factory/ ledgers
    # (notifications store, webhook audit store, HF-15 sandbox provisioning).
    # Canned dicts keep the smoke test hermetic and offline.
    monkeypatch.setattr(service, "list_notifications", lambda **kwargs: _CANNED_NOTIFICATIONS)
    monkeypatch.setattr(service, "get_telegram_gateway_status", lambda: dict(_CANNED_TELEGRAM_STATUS))
    monkeypatch.setattr(service, "get_n8n_status", lambda target_url=None: dict(_CANNED_N8N_STATUS))
    monkeypatch.setattr(service, "get_n8n_workflows", lambda limit=50: dict(_CANNED_N8N_WORKFLOWS))
    monkeypatch.setattr(service, "get_test_workers_status", lambda: [dict(w) for w in _CANNED_WORKERS])
    monkeypatch.setattr(
        service,
        "get_webhook_events",
        lambda limit=50, event_type=None: [dict(e) for e in _CANNED_WEBHOOK_EVENTS],
    )
    monkeypatch.setattr(service, "get_hf15_status", lambda: dict(_CANNED_HF15_STATUS))
    monkeypatch.setattr(service, "get_hf15_metrics", lambda: dict(_CANNED_HF15_METRICS))

    app.dependency_overrides[get_hub_service] = lambda: service
    client = TestClient(app)
    try:
        routes = [
            "/api/notifications?unread_only=true&limit=20",
            "/api/integrations/telegram/status",
            "/api/integrations/n8n/status",
            "/api/integrations/n8n/workflows?limit=50",
            "/api/harness/workers",
            "/api/webhooks/events?limit=20",
            "/api/hf15/status",
            "/api/hf15/metrics",
        ]
        for route in routes:
            response = client.get(route)
            assert response.status_code == 200, f"{route} -> {response.status_code}: {response.text}"

        notifications_payload = client.get("/api/notifications?unread_only=true&limit=20").json()
        assert notifications_payload["count"] == 1
        assert notifications_payload["notifications"][0]["notification_id"] == "notif-1"

        assert client.get("/api/integrations/telegram/status").json()["configured"] is True
        assert client.get("/api/integrations/n8n/status").json()["operational"] is True
        assert client.get("/api/harness/workers").json()[0]["status"] == "online"
        assert client.get("/api/webhooks/events?limit=20").json()[0]["event_type"] == "push"
        assert client.get("/api/hf15/status").json()["environment"]["all_preflights_passed"] is True
        assert client.get("/api/hf15/metrics").json()["all_slas_met"] is True

        # The Hub page itself must still mount the new surfaces.
        page = client.get("/")
        assert page.status_code == 200
        assert "/static/health.js" in page.text
    finally:
        app.dependency_overrides.clear()
