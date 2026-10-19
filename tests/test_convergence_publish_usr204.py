"""USR-204: snapshot de convergencia do Notebook chega ao /status da VPS (hermetico, sem rede)."""

from __future__ import annotations

import json
from pathlib import Path

from core.infra.convergence_publish import (
    collect_convergence_payload,
    ingest_convergence_snapshot,
    publish_convergence_snapshot,
)
from core.integrations.telegram_status import UNAVAILABLE, build_status_message, read_convergence_snapshot


def _status(state_dir: Path) -> str:
    return build_status_message(
        owner_actions=list,
        tickets=list,
        runs=list,
        convergence=lambda: read_convergence_snapshot(state_dir),
    )


def test_publish_then_ingest_shows_nodes_on_vps(tmp_path: Path) -> None:
    notebook, vps = tmp_path / "nb", tmp_path / "vps"
    notebook.mkdir()
    (notebook / "node_sync_divergence.json").write_text(json.dumps({"Notebook": 0, "Desktop": 2}))
    (notebook / "node_sync_pending.json").write_text(json.dumps({"node": "Desktop", "reason": "offline"}))
    sent: list[tuple] = []

    def post(url, payload, headers, timeout):
        sent.append((url, headers, timeout))
        ingest_convergence_snapshot(vps, payload["nodes"], payload["pending"])
        return 200

    assert publish_convergence_snapshot(notebook, telemetry_key="k", target_url="https://hub.test", post=post)
    assert sent[0][0] == "https://hub.test/api/node-sync/convergence"
    assert sent[0][1]["X-DarkFac-Telemetry-Key"] == "k" and sent[0][2] <= 10
    text = _status(vps)
    assert "Notebook sem divergencia" in text and "Desktop divergente (2 ciclos)" in text
    assert "Convergencia pendente: Desktop" in text


def test_ingest_without_pending_clears_old_pending(tmp_path: Path) -> None:
    ingest_convergence_snapshot(tmp_path, {"VPS": 0}, {"node": "VPS", "reason": "x"})
    ingest_convergence_snapshot(tmp_path, {"VPS": 0}, None)
    assert not (tmp_path / "node_sync_pending.json").exists()


def test_snapshot_absent_degrades_only_nodes_block(tmp_path: Path) -> None:
    assert collect_convergence_payload(tmp_path) is None
    assert publish_convergence_snapshot(tmp_path, telemetry_key="k", post=lambda *a: 200) is False
    text = _status(tmp_path)
    assert f"<b>Nos</b>: {UNAVAILABLE}" in text or "Nos" in text and UNAVAILABLE in text
    assert "<b>Acoes do owner</b>: nenhuma acao aberta" in text


def test_publish_never_raises_and_requires_key(tmp_path: Path) -> None:
    (tmp_path / "node_sync_divergence.json").write_text("{}")

    def boom(*_a):
        raise OSError("down")

    assert publish_convergence_snapshot(tmp_path, telemetry_key="k", post=boom) is False
    assert publish_convergence_snapshot(tmp_path, telemetry_key="", post=lambda *a: 200) is False


def test_hub_endpoint_rejects_bad_key_and_ingests(tmp_path: Path, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hub.backend.api import router

    monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", "secret")
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    body = {"client_node_id": "nb", "nodes": {"Notebook": 0}, "pending": None}
    assert client.post("/api/node-sync/convergence", json=body, headers={"X-DarkFac-Telemetry-Key": "no"}).status_code == 401
    ok = client.post("/api/node-sync/convergence", json=body, headers={"X-DarkFac-Telemetry-Key": "secret"})
    assert ok.status_code == 200
    assert json.loads((tmp_path / "node_sync_divergence.json").read_text()) == {"Notebook": 0}
