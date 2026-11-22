from __future__ import annotations

import json
from types import SimpleNamespace

from core.ops import coordination


def test_cli_entries_share_coordinator_and_persist_receipt_before_briefing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    ticket = SimpleNamespace(id="USR-539", project_id="darkfac", status="planned", priority=20)
    monkeypatch.setattr(coordination, "project_root", lambda: tmp_path)
    monkeypatch.setattr(coordination, "_candidates", lambda: [SimpleNamespace(eligible=True, ticket=ticket)])
    monkeypatch.setattr(coordination, "_in_flight", lambda: [])
    monkeypatch.setattr(coordination, "_route", lambda _ticket, _harness, stage: ({"stage": stage, "harness": "codex", "model": "m"}, 1.0, "usage-event"))
    observed = []
    monkeypatch.setattr(coordination.briefing, "render_briefing", lambda: observed.append(
        json.loads((tmp_path / "coordination" / "last_receipt.json").read_text(encoding="utf-8"))
    ))

    assert coordination.main(["--harness", "codex", "--entry", "short-request"]) == 0
    assert json.loads(capsys.readouterr().out)["route"]["stage"] == "planning"
    assert coordination.main(["--harness", "codex", "--entry", "skill-file"]) == 0
    assert [receipt["route"]["stage"] for receipt in observed] == ["planning", "planning"]
    assert all(receipt["route_event_id"] == "usage-event" for receipt in observed)


def test_in_flight_run_blocks_new_ticket_and_recovers_durable_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "pending_deliveries.json").write_text(json.dumps([{
        "ticket_id": "USR-539-A", "run_id": "run-123", "status": "awaiting_deploy",
        "stage": "deploy", "evidence_refs": ["validation.json"],
    }]), encoding="utf-8")
    monkeypatch.setattr(coordination, "project_root", lambda: tmp_path)
    monkeypatch.setattr(coordination, "_candidates", lambda: [SimpleNamespace(
        eligible=True, ticket=SimpleNamespace(id="USR-539-B", project_id="darkfac", status="planned", priority=100)
    )])
    monkeypatch.setattr(coordination, "_in_flight", lambda: ["USR-539-A"])
    monkeypatch.setattr(coordination.briefing, "render_briefing", lambda: None)

    receipt = coordination.coordinate(harness="codex")

    assert receipt.status == "in_flight"
    assert receipt.ticket_id is None
    assert receipt.active_run == {
        "ticket_id": "USR-539-A", "run_id": "run-123", "stage": "deploy",
        "evidence_refs": ["validation.json"],
    }
    assert "run-123" in receipt.first_action
    assert receipt.route is None


def test_ticket_validation_status_routes_to_review_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    ticket = SimpleNamespace(id="USR-539", project_id="darkfac", status="validating", priority=20)
    monkeypatch.setattr(coordination, "project_root", lambda: tmp_path)
    monkeypatch.setattr(coordination, "_candidates", lambda: [SimpleNamespace(eligible=True, ticket=ticket)])
    monkeypatch.setattr(coordination, "_in_flight", lambda: [])
    selected = []
    monkeypatch.setattr(coordination, "_route", lambda _ticket, _harness, stage: (
        selected.append(stage) or {"stage": stage, "harness": "claude", "model": "review-model"}, 0.5, None
    ))
    monkeypatch.setattr(coordination.briefing, "render_briefing", lambda: None)

    receipt = coordination.coordinate(harness="codex")

    assert selected == ["review"]
    assert receipt.route["stage"] == "review"
    assert "etapa review" in receipt.first_action
