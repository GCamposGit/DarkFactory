from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from core.ops import briefing


def test_render_briefing_keeps_sections_when_one_reader_fails(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(briefing, "_queue", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(briefing, "_owner_actions", lambda: ["OA-001 | open | exemplo"])
    monkeypatch.setattr(briefing, "_nodes", lambda: ["node-a | offline"])
    monkeypatch.setattr(briefing, "_pull_requests", lambda: ["Fallback: gh ausente"])
    monkeypatch.setattr(briefing, "_quotas", lambda: ["antigravity: desconhecida"])
    monkeypatch.setattr(briefing, "_handoff", lambda: ["Versao: 1"])

    output = briefing.render_briefing()

    assert "Fila do control store" in output and "RuntimeError" in output
    assert "OA-001" in output and "node-a" in output and "Fallback: gh ausente" in output
    assert "antigravity: desconhecida" in output and "Versao: 1" in output
    assert not any(ord(char) > 0xFFFF for char in output)


def test_pull_requests_reports_structured_fallback_when_gh_missing(monkeypatch) -> None:
    monkeypatch.setattr(briefing.shutil, "which", lambda _name: None)
    assert briefing._pull_requests() == ["Fallback: gh ausente; lista de PRs indisponivel."]


def test_pull_requests_uses_bounded_gh_query(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(briefing.shutil, "which", lambda _name: "gh")

    def run(*args, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(args[0], 0, '[{"number":7,"title":"PR teste","url":"https://example/pr/7"}]', "")

    monkeypatch.setattr(briefing.subprocess, "run", run)
    assert briefing._pull_requests() == ["#7 | PR teste | https://example/pr/7"]
    assert calls[0]["timeout"] == briefing.GH_TIMEOUT_SECONDS


def test_pull_requests_reports_fallback_on_auth_failure(monkeypatch) -> None:
    monkeypatch.setattr(briefing.shutil, "which", lambda _name: "gh")
    monkeypatch.setattr(
        briefing.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "not logged in to github.com"),
    )
    assert briefing._pull_requests() == [
        "Fallback: gh indisponivel ou sem autenticacao (not logged in to github.com)."
    ]


def test_quotas_read_snapshots_without_mutating_them(monkeypatch, tmp_path: Path) -> None:
    usage = tmp_path / "usage" / "providers"
    usage.mkdir(parents=True)
    target = usage / "google.json"
    raw = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "windows": [{"label": "semana", "remaining_percent": 42}],
        "probe_failure": {"category": "transport", "code": "rpc_transport_failed",
                          "message": "Falha de transporte para Language Server."},
    }
    target.write_text(json.dumps(raw), encoding="utf-8")
    before = target.read_bytes()
    monkeypatch.setattr(briefing, "state_root", lambda: tmp_path)

    lines = briefing._quotas()

    assert any(line.startswith("antigravity:") and "42% restante" in line
               and "transport/rpc_transport_failed" in line for line in lines)
    assert target.read_bytes() == before


def test_nodes_use_read_only_liveness_without_inventory_updates(monkeypatch) -> None:
    from core.infra import inventory, node_liveness

    class Node:
        def __init__(self, node_id: str) -> None:
            self.node_id = node_id
            self.status = "offline"

    node_ids = ("grokbox-test-worker", "onprem-z97-server")
    monkeypatch.setattr(
        node_liveness,
        "worker_nodes",
        lambda: {node_id: {"url": "http://127.0.0.1:8080"} for node_id in node_ids},
    )
    monkeypatch.setattr(node_liveness, "get_node_liveness", lambda node_id, **_kwargs: Node(node_id))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("inventory update called by read-only briefing")

    monkeypatch.setattr(inventory.InventoryManager, "update_node_status", forbidden)
    monkeypatch.setattr(inventory.InventoryManager, "save", forbidden)
    assert briefing._nodes() == [f"{node_id} | offline" for node_id in node_ids]


def test_handoff_is_versioned_and_has_operational_sections() -> None:
    handoff = (Path(__file__).resolve().parents[1] / "docs/operacao/HANDOFF.md").read_text(encoding="utf-8")
    for expected in ("Versao:", "## Estado", "## Decisoes", "## Pendencias", "## Proximo passo"):
        assert expected in handoff


def test_coordination_receipt_persists_route_and_cost(monkeypatch, tmp_path: Path) -> None:
    from types import SimpleNamespace
    from core.ops import coordination

    ticket = SimpleNamespace(id="USR-539", project_id="darkfac")
    monkeypatch.setattr(coordination, "project_root", lambda: tmp_path)
    monkeypatch.setattr(coordination, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(coordination, "_candidates", lambda: [
        SimpleNamespace(eligible=True, ticket=ticket),
    ])
    monkeypatch.setattr(coordination, "_in_flight", lambda: ["USR-500"])
    monkeypatch.setattr(coordination, "_route", lambda *_args: (
        {"stage": "development", "harness": "codex", "model": "route-alias"}, 1.25, "event-1",
    ))

    receipt = coordination.coordinate(harness="codex")

    saved = json.loads((tmp_path / "state/coordination/last_receipt.json").read_text(encoding="utf-8"))
    assert receipt.project_id == "darkfac"
    assert receipt.role == "coordinator" and receipt.in_flight == ["USR-500"]
    assert receipt.ticket_id == "USR-539" and receipt.first_action.startswith("Delegar")
    assert receipt.route_event_id == "event-1" and receipt.cost_available_usd == 1.25
    assert saved == receipt.model_dump(mode="json")


def test_coordination_idle_does_not_route_without_eligible_work(monkeypatch, tmp_path: Path) -> None:
    from core.ops import coordination

    monkeypatch.setattr(coordination, "project_root", lambda: tmp_path)
    monkeypatch.setattr(coordination, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(coordination, "_candidates", lambda: [])
    monkeypatch.setattr(coordination, "_in_flight", lambda: [])
    monkeypatch.setattr(coordination, "_route", lambda *_args: (_ for _ in ()).throw(AssertionError("route without work")))

    receipt = coordination.coordinate(harness="grok")

    assert receipt.status == "idle" and receipt.ticket_id is None
    assert receipt.route is None and "sem trabalho elegível" in receipt.first_action


def test_short_and_skill_file_entries_share_the_same_contract(monkeypatch, tmp_path: Path) -> None:
    from core.ops import coordination

    monkeypatch.setattr(coordination, "project_root", lambda: tmp_path)
    monkeypatch.setattr(coordination, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(coordination.briefing, "render_briefing", lambda: "briefing")
    monkeypatch.setattr(coordination, "_candidates", lambda: [])
    monkeypatch.setattr(coordination, "_in_flight", lambda: ["USR-100"])
    short = coordination.coordinate(harness="claude", entry="short-request")
    explicit = coordination.coordinate(harness="claude", entry="skill-file")

    assert short.model_dump(exclude={"created_at"}) == explicit.model_dump(exclude={"created_at"})
    assert short.status == "in_flight" and short.in_flight == ["USR-100"]
