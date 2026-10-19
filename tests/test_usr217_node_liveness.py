"""USR-217: one liveness truth for /api/harness/workers, /api/infra/metrics and infra cards."""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from core.infra import metrics as infra_metrics
from core.infra import node_liveness as nl
from core.infra.cards import build_infra_cards_report
from core.infra.inventory import build_default_inventory

DESKTOP = "onprem-z97-server"
BUSY_HEALTH = {"status": "busy", "busy": True, "active_runs": 1, "node_id": "desktop-g45ipem"}
OK_HEALTH = {"status": "ok", "busy": False, "active_runs": 0, "node_id": "desktop-g45ipem"}


def _fake_probe(payload: Optional[Dict[str, Any]]):
    def probe(url: str, timeout: float) -> Optional[Dict[str, Any]]:
        return payload

    return probe


def _patch_probe(monkeypatch: pytest.MonkeyPatch, payload: Optional[Dict[str, Any]]) -> None:
    monkeypatch.setattr(nl, "_default_prober", _fake_probe(payload))


@pytest.mark.parametrize(
    ("payload", "expected"),
    [(OK_HEALTH, "online"), (BUSY_HEALTH, "online_busy"), ({"status": "ok", "active_runs": 2}, "online_busy"), (None, "offline")],
)
def test_classify_health(payload: Optional[Dict[str, Any]], expected: str) -> None:
    assert nl.classify_health(payload) == expected


def test_id_mapping_comes_from_inventory() -> None:
    nodes = nl.worker_nodes()
    assert nodes[DESKTOP]["url"] == "http://100.78.181.90:8080"
    assert nodes["grokbox-test-worker"]["url"] == "http://100.77.130.5:8080"
    assert "predator-neo-16" in nodes


def test_liveness_fields_online_busy_offline() -> None:
    busy = nl.get_node_liveness(DESKTOP, prober=_fake_probe(BUSY_HEALTH))
    assert busy.status == "online_busy" and busy.is_live and busy.latency_ms is not None
    assert busy.last_contact_at is not None
    ok = nl.get_node_liveness(DESKTOP, prober=_fake_probe(OK_HEALTH))
    assert ok.status == "online" and ok.details == OK_HEALTH
    down = nl.get_node_liveness(DESKTOP, prober=_fake_probe(None))
    assert down.status == "offline" and not down.is_live
    assert down.latency_ms is None
    # last contact survives the outage
    assert down.last_contact_at == ok.last_contact_at


def test_prober_exception_is_offline() -> None:
    def boom(url: str, timeout: float) -> Optional[Dict[str, Any]]:
        raise RuntimeError("no route")

    assert nl.get_node_liveness(DESKTOP, prober=boom).status == "offline"


@pytest.mark.parametrize(
    ("payload", "workers_status", "metrics_live", "metrics_status"),
    [
        (BUSY_HEALTH, "online_busy", True, "online_busy"),
        (OK_HEALTH, "online", True, "online"),
        (None, "offline", False, "offline"),
    ],
)
def test_same_health_same_status_across_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    payload: Optional[Dict[str, Any]],
    workers_status: str,
    metrics_live: bool,
    metrics_status: str,
) -> None:
    from hub.backend.service import HubService

    _patch_probe(monkeypatch, payload)
    # hardware telemetry endpoint unavailable: liveness must not depend on it
    monkeypatch.setattr(
        infra_metrics.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("no hw"))
    )
    service = HubService.__new__(HubService)
    service.project_root = tmp_path
    workers = {w["node_id"]: w for w in service.get_test_workers_status()}
    desktop_worker = workers[DESKTOP]
    node = infra_metrics.collect_desktop_hardware_node()
    cards = {c.id: c for c in build_infra_cards_report(build_default_inventory(), probe_network_liveness=True).cards}

    assert desktop_worker["status"] == workers_status
    assert desktop_worker["healthy"] is metrics_live
    assert node.is_live is metrics_live
    assert node.status == metrics_status
    assert cards[DESKTOP].liveness_status == workers_status
    assert cards[DESKTOP].is_live_reachable is metrics_live
    expected_online = 0 if payload is None else len(nl.WORKER_NODE_IDS)
    assert nl.count_online(nl.list_worker_liveness(prober=_fake_probe(payload))) == expected_online


def test_busy_node_never_offline_in_metrics_even_with_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probe(monkeypatch, BUSY_HEALTH)
    monkeypatch.setattr(
        infra_metrics.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("no hw"))
    )
    node = infra_metrics.collect_desktop_hardware_node()
    assert node.is_live is True
    assert node.status != "offline"
