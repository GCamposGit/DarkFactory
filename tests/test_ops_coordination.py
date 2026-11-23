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


def _stalled_env(tmp_path, monkeypatch, holds=()):
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "fleet_dispatcher_state.json").write_text(json.dumps({
        "activity": "blocked", "free_hosts": ["notebook"],
        "recent_failures": [{"ticket": "USR-500", "exit_code": 75, "reason": "[RECUSA] USR-500 ja possui PR ativo #9"}],
    }), encoding="utf-8")
    monkeypatch.setattr(coordination, "project_root", lambda: tmp_path)
    monkeypatch.setattr(coordination, "_candidates", lambda: [SimpleNamespace(
        eligible=True, ticket=SimpleNamespace(id="USR-600", project_id="darkfac", status="planned", priority=1))])
    monkeypatch.setattr(coordination, "_in_flight", lambda: [])
    monkeypatch.setattr(coordination, "_route", lambda _t, _h, stage: ({"stage": stage, "harness": "codex", "model": "m"}, 1.0, None))
    monkeypatch.setattr(coordination.briefing, "render_briefing", lambda: None)
    monkeypatch.setattr("core.line.admission_store.AdmissionStore.ticket_holds", lambda self: list(holds))
    monkeypatch.setattr(coordination, "_is_terminal", lambda _ticket: False)


def test_green_draft_pr_with_refusal_loop_is_first_action(tmp_path, monkeypatch):
    _stalled_env(tmp_path, monkeypatch)
    prs = [{"number": 9, "title": "feat: x [USR-500]", "isDraft": True, "headRefName": "ticket/usr-500",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}]}]
    receipt = coordination.coordinate(harness="codex", runner=lambda argv: json.dumps(prs) if argv[0] == "gh" else "")

    pr = next(s for s in receipt.stalls if s["kind"] == "ticket_pr_open")
    assert (pr["pr"], pr["draft"], pr["ci"], pr["refusal_loop"]) == (9, True, "green", True)
    assert any(s["kind"] == "dispatcher_blocked_with_free_host" for s in receipt.stalls)
    assert receipt.status == "stalled"
    assert receipt.first_action.startswith("Revisar (outra familia), promover e mesclar o PR #9 do ticket USR-500")


def test_hold_already_delivered_is_detected_and_other_holds_stay_holds(tmp_path, monkeypatch):
    holds = [{"project_id": "darkfac", "ticket_id": "USR-447", "cause": "x"},
             {"project_id": "darkfac", "ticket_id": "USR-448", "cause": "ci"}]
    _stalled_env(tmp_path, monkeypatch, holds)

    def runner(argv):
        if argv[0] == "gh":
            return "[]"
        return "abc123\tfix: entrega [USR-447]\n" if "--grep=[USR-447]" in argv else ""

    receipt = coordination.coordinate(harness="codex", runner=runner)

    assert {"kind": "hold_already_delivered", "ticket_id": "USR-447", "sha": "abc123",
            "project_id": "darkfac"} in receipt.stalls
    assert {"kind": "hold", "ticket_id": "USR-448", "cause": "ci"} in receipt.stalls
    assert "complete USR-447 --sha abc123" in receipt.first_action


def test_unavailable_sources_do_not_break_the_receipt(tmp_path, monkeypatch):
    _stalled_env(tmp_path, monkeypatch, [{"project_id": "darkfac", "ticket_id": "USR-447", "cause": "x"}])

    def runner(_argv):
        raise OSError("sem rede")

    receipt = coordination.coordinate(harness="codex", runner=runner)

    assert {s["source"] for s in receipt.stalls if s["kind"] == "source_unavailable"} == {"gh_pr_list", "git_log"}
    assert receipt.route is not None
