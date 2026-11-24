from __future__ import annotations

import json
from pathlib import Path
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


def _quiet_runner(argv):
    return "[]" if argv and argv[0] == "gh" else ""


def _deferral_record(sha, *, node, reason, attempts, exhausted):
    return {
        "target_sha": sha,
        "pending_node": node,
        "reason": reason,
        "recovery_attempts": attempts,
        "recovery_exhausted": exhausted,
    }


def test_detect_stalls_emits_one_exhausted_deploy_per_record(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    runs = tmp_path / "runs"
    runs.mkdir()
    notebook = "07e55b6" + "a" * 33
    desktop = "d" * 40
    waiting = "e" * 40
    (runs / "deploy_deferrals.json").write_text(json.dumps({
        notebook: _deferral_record(notebook, node="notebook", reason="ledger blocks checkout", attempts=6, exhausted=True),
        "ignored": "not-a-record",
        waiting: _deferral_record(waiting, node="desktop", reason="still backing off", attempts=2, exhausted=False),
        desktop: _deferral_record(desktop, node="desktop", reason="dirty worktree", attempts=6, exhausted=True),
    }), encoding="utf-8")
    monkeypatch.setattr("core.line.admission_store.AdmissionStore.ticket_holds", lambda self: [])

    stalls = coordination.detect_stalls(_quiet_runner)

    exhausted = [stall for stall in stalls if stall["kind"] == "deploy_convergence_exhausted"]
    assert exhausted == [
        {
            "kind": "deploy_convergence_exhausted",
            "sha": notebook,
            "pending_node": "notebook",
            "reason": "ledger blocks checkout",
            "recovery_attempts": 6,
        },
        {
            "kind": "deploy_convergence_exhausted",
            "sha": desktop,
            "pending_node": "desktop",
            "reason": "dirty worktree",
            "recovery_attempts": 6,
        },
    ]
    action = coordination._stall_action(exhausted)
    assert action.startswith("Stall deploy_convergence_exhausted: sha " + notebook)
    assert "[NODE SYNC]" in action
    assert "limpar a causa" in action
    assert "python C:\\dev\\DarkFac\\scripts\\dokploy_redeploy.py" in action
    assert action.index("[NODE SYNC]") < action.index("limpar a causa") < action.index("dokploy_redeploy.py")


def test_missing_deploy_deferrals_emit_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    (tmp_path / "runs").mkdir()
    monkeypatch.setattr("core.line.admission_store.AdmissionStore.ticket_holds", lambda self: [])

    stalls = coordination.detect_stalls(_quiet_runner)

    assert [stall["kind"] for stall in stalls] == []


def test_invalid_or_unreadable_deploy_deferrals_keep_the_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    runs = tmp_path / "runs"
    runs.mkdir()
    path = runs / "deploy_deferrals.json"
    path.write_text("{", encoding="utf-8")
    ticket = SimpleNamespace(id="USR-600", project_id="darkfac", status="planned", priority=1)
    monkeypatch.setattr(coordination, "project_root", lambda: tmp_path)
    monkeypatch.setattr(coordination, "_candidates", lambda: [SimpleNamespace(eligible=True, ticket=ticket)])
    monkeypatch.setattr(coordination, "_in_flight", lambda: [])
    monkeypatch.setattr(coordination, "_route", lambda _t, _h, stage: ({"stage": stage, "harness": "codex", "model": "m"}, 1.0, None))
    monkeypatch.setattr(coordination.briefing, "render_briefing", lambda: None)
    monkeypatch.setattr("core.line.admission_store.AdmissionStore.ticket_holds", lambda self: [])

    invalid = coordination.coordinate(harness="codex", runner=_quiet_runner)
    assert any(stall["source"] == "deploy_deferrals" and stall["kind"] == "source_unavailable" for stall in invalid.stalls)
    assert invalid.status == "ready"
    assert invalid.route is not None
    assert invalid.ticket_id == "USR-600"

    path.write_text("[]", encoding="utf-8")
    not_object = coordination.coordinate(harness="codex", runner=_quiet_runner)
    assert any(stall["source"] == "deploy_deferrals" for stall in not_object.stalls)
    assert not_object.route is not None

    real_read = Path.read_text

    def denied(self, *args, **kwargs):
        if self.name == "deploy_deferrals.json":
            raise PermissionError("denied")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    path.write_text("{}", encoding="utf-8")
    unreadable = coordination.coordinate(harness="codex", runner=_quiet_runner)
    unavailable = next(stall for stall in unreadable.stalls if stall["source"] == "deploy_deferrals")
    assert unavailable["kind"] == "source_unavailable"
    assert unavailable["error"] == "PermissionError"
    assert unreadable.status == "ready"
    assert unreadable.route is not None


def test_exhausted_deploy_during_delivery_wait_stays_in_flight_and_leads(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    runs = tmp_path / "runs"
    runs.mkdir()
    sha = "07e55b6" + "a" * 33
    (runs / "deploy_deferrals.json").write_text(json.dumps({
        sha: _deferral_record(sha, node="notebook", reason="ledger blocks checkout", attempts=6, exhausted=True),
    }), encoding="utf-8")
    (runs / "pending_deliveries.json").write_text(json.dumps([{
        "ticket_id": "USR-546", "run_id": "run-546", "status": "awaiting_deploy",
        "stage": "deploy", "evidence_refs": ["validation.json"],
    }]), encoding="utf-8")
    (runs / "fleet_dispatcher_state.json").write_text(json.dumps({
        "activity": "idle", "delivery_wait": True, "free_hosts": ["notebook"],
    }), encoding="utf-8")
    monkeypatch.setattr(coordination, "project_root", lambda: tmp_path)
    monkeypatch.setattr(coordination, "_candidates", lambda: [SimpleNamespace(
        eligible=True, ticket=SimpleNamespace(id="USR-600", project_id="darkfac", status="planned", priority=100),
    )])
    monkeypatch.setattr(coordination, "_in_flight", lambda: ["USR-546"])
    monkeypatch.setattr(coordination, "_route", lambda _t, _h, stage: ({"stage": stage, "harness": "codex", "model": "m"}, 1.0, None))
    monkeypatch.setattr(coordination.briefing, "render_briefing", lambda: None)
    monkeypatch.setattr("core.line.admission_store.AdmissionStore.ticket_holds", lambda self: [])

    receipt = coordination.coordinate(harness="codex", runner=_quiet_runner)

    assert receipt.status == "in_flight"
    assert receipt.ticket_id is None
    assert receipt.route is None
    assert receipt.in_flight == ["USR-546"]
    assert receipt.active_run == {
        "ticket_id": "USR-546", "run_id": "run-546", "stage": "deploy",
        "evidence_refs": ["validation.json"],
    }
    stall = next(item for item in receipt.stalls if item["kind"] == "deploy_convergence_exhausted")
    assert stall == {
        "kind": "deploy_convergence_exhausted",
        "sha": sha,
        "pending_node": "notebook",
        "reason": "ledger blocks checkout",
        "recovery_attempts": 6,
    }
    assert receipt.first_action == coordination._stall_action([stall])
    assert "USR-600" not in receipt.first_action
    assert "[NODE SYNC]" in receipt.first_action
    assert "python C:\\dev\\DarkFac\\scripts\\dokploy_redeploy.py" in receipt.first_action


def test_exhausted_deploy_stalls_an_idle_receipt_and_spares_other_in_flight_work(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    runs = tmp_path / "runs"
    runs.mkdir()
    sha = "c" * 40
    (runs / "deploy_deferrals.json").write_text(json.dumps({
        sha: _deferral_record(sha, node="desktop", reason="checkout dirty", attempts=6, exhausted=True),
    }), encoding="utf-8")
    monkeypatch.setattr(coordination, "project_root", lambda: tmp_path)
    monkeypatch.setattr(coordination, "_candidates", lambda: [SimpleNamespace(
        eligible=True, ticket=SimpleNamespace(id="USR-600", project_id="darkfac", status="planned", priority=1),
    )])
    monkeypatch.setattr(coordination, "_in_flight", lambda: [])
    monkeypatch.setattr(coordination, "_route", lambda _t, _h, stage: ({"stage": stage, "harness": "codex", "model": "m"}, 1.0, None))
    monkeypatch.setattr(coordination.briefing, "render_briefing", lambda: None)
    monkeypatch.setattr("core.line.admission_store.AdmissionStore.ticket_holds", lambda self: [])

    idle = coordination.coordinate(harness="codex", runner=_quiet_runner)

    assert idle.status == "stalled"
    assert idle.first_action.startswith("Stall deploy_convergence_exhausted")
    assert "dokploy_redeploy.py" in idle.first_action
    assert "Delegar USR-600" not in idle.first_action

    (runs / "pending_deliveries.json").write_text(json.dumps([{
        "ticket_id": "USR-546", "run_id": "run-dev", "status": "awaiting_deploy",
        "stage": "development", "evidence_refs": [],
    }]), encoding="utf-8")
    monkeypatch.setattr(coordination, "_in_flight", lambda: ["USR-546"])
    developing = coordination.coordinate(harness="codex", runner=_quiet_runner)
    assert developing.status == "in_flight"
    assert developing.ticket_id is None
    assert developing.active_run["stage"] == "development"
    assert "run-dev" in developing.first_action
    assert "dokploy_redeploy.py" not in developing.first_action
    assert any(item["kind"] == "deploy_convergence_exhausted" for item in developing.stalls)
