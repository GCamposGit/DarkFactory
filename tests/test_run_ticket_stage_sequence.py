"""run_ticket executes the canonical stage sequence (USR-375 S4).

One end-to-end contract over the three entry points (run_ticket CLI, the fleet dispatcher's launch
command, the line's bindings): a ticket whose plan, review verdict or deploy report is missing is not
delivered, and without plan or verdict it never reaches the merge. A small fix goes through without
the grill but still with a plan artifact.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import run_ticket
from core.demands.models import UserTicket
from core.git.autonomy import DeliveryReport, GitAutonomyManager, TicketCompletionReport
from core.git.ticket_workspace import TicketWorkspace
from core.line import bindings, fleet_dispatcher, routing, stage_integration, stage_planning, stage_review
from core.line import workspace as line_workspace
from core.line.agent_cli import AgentResult
from core.line.routing import _HARNESS_TO_PROVIDER
from core.line.stage_review import ReviewVerdict
from core.line.stages import VERDICT_FILE
from core.workflow.control_contracts import StageResult

REAL_TICKET_STAGES = True  # opt out of the conftest doubles: this module drives PLAN/REVIEW itself

_MISSING = ("plan", "verdict", "deploy_report")


def _ticket(complexity: str) -> UserTicket:
    return UserTicket(
        id="USR-990", project_id="darkfac", title="Ticket de sequencia", problem_statement="corrigir algo",
        acceptance_criteria=["faz o que deve"], estimated_complexity=complexity,
    )


def _quotas() -> dict[str, dict[str, Any]]:
    return {
        h: {"provider": p, "headroom": 60.0, "is_critical": False, "status": "SAUDAVEL"}
        for h, p in _HARNESS_TO_PROVIDER.items()
    }


@pytest.fixture
def line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Hermetic launcher: fake ledger/quotas/workspace/agent; records which stages ran."""
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    state = SimpleNamespace(ticket=_ticket("medium"), missing=None, calls=[], merged=MagicMock())
    state.sweep_calls = []
    state.launcher_checkout = tmp_path / "heartbeat-ops"
    state.launcher_checkout.mkdir()
    (state.launcher_checkout / "launcher-state.json").write_text("active", encoding="utf-8")

    class _Store:
        def __init__(self, _path: Path) -> None:
            pass

        def get_ticket(self, ticket_id: str) -> Any:
            return state.ticket if ticket_id == state.ticket.id else None

        def list_tickets(self) -> list[Any]:
            return [state.ticket]

    monkeypatch.setattr(run_ticket, "DemandsStore", _Store)
    monkeypatch.setattr(run_ticket, "inspect_quotas", _quotas)
    monkeypatch.setattr(routing, "default_cooldown_path", lambda: tmp_path / "cooldowns.json")
    monkeypatch.setattr(run_ticket, "time", SimpleNamespace(sleep=lambda *_a: None))
    monkeypatch.setattr(run_ticket, "check_optional_mcp_servers", lambda *a, **k: (True, []))
    monkeypatch.setattr(run_ticket, "rebase_on_origin_main", lambda cwd, **_k: (True, [], "ok"))
    monkeypatch.setattr(
        GitAutonomyManager, "sweep_stale",
        lambda self, *a, **k: (
            state.sweep_calls.append((self, a, k)), shutil.rmtree(state.launcher_checkout, ignore_errors=True)
        ),
    )
    monkeypatch.setattr(GitAutonomyManager, "changed_paths", lambda self, cwd: ["feature.py"])
    monkeypatch.setattr(run_ticket, "_resolve_project", lambda ticket: SimpleNamespace(id="darkfac"))
    wdir = tmp_path / "wt"
    wdir.mkdir()
    ws = TicketWorkspace(
        ticket_id="USR-990", path=wdir, branch="ticket/usr-990", main_root=tmp_path,
        base_ref="origin/main", base_sha="0" * 40,
    )
    monkeypatch.setattr(run_ticket, "_prepare_workspace", lambda args, ticket: ws)

    def agent(req: Any) -> AgentResult:
        state.calls.append("BUILD")
        return AgentResult(ok=True, text="done", harness=req.harness, duration_s=1.0, exit_code=0)

    monkeypatch.setattr(run_ticket, "run_agent", agent)

    def planning(project: Any, run_id: str, *, ws: Any, **_kw: Any) -> StageResult:
        state.calls.append("PLAN")
        if state.missing == "plan_route":  # no high-tier model: RouteWaiter outcome
            return StageResult(outcome="retry", cause_code="no_route_available not_before=2030-10-24T12:00:00+00:00")
        if state.missing != "plan":
            line_workspace.write_context(ws, "SPEC.md", "# SPEC\n\nobjetivo\n")
            line_workspace.write_context(ws, "tickets.json", json.dumps([{"id": "T1", "title": "Sub um"}]))
        return StageResult(outcome="success", output_refs=["test"])

    def review(self: Any, project: Any, run_id: str, *, ws: Any, **_kw: Any) -> StageResult:
        state.calls.append("REVIEW")
        if state.missing == "review_route":  # no other-family reviewer right now
            return StageResult(outcome="retry", cause_code="no_route_available not_before=2030-10-24T12:00:00+00:00")
        if state.missing != "verdict":
            line_workspace.write_context(ws, VERDICT_FILE, ReviewVerdict(verdict="approve").model_dump_json())
        return StageResult(outcome="success", output_refs=["test"])

    monkeypatch.setattr(stage_planning, "run_planning", planning)
    monkeypatch.setattr(stage_review.ReviewStage, "run", review)
    real_deploy = run_ticket._stage_deploy_smoke
    monkeypatch.setattr(
        run_ticket, "_stage_deploy_smoke",
        lambda *a, **k: None if state.missing == "deploy_report" else real_deploy(*a, **k),
    )

    def complete(self: Any, **kwargs: Any) -> TicketCompletionReport:
        state.merged(kwargs["ticket_id"])
        return TicketCompletionReport(
            ok=True, ticket_id=kwargs["ticket_id"], commit_sha="c" * 40,
            delivery=DeliveryReport(
                ok=True, action="merged", pr_url="https://github.com/example/repo/pull/42",
                merge_sha="d" * 40, branch="ticket/usr-990",
            ),
        )

    monkeypatch.setattr(GitAutonomyManager, "complete_ticket", complete)
    return state


def _via_cli(line: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> int:
    return run_ticket.main(["USR-990", "--harness", "codex", "--skip-validation"])


def _via_fleet(line: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> int:
    """The dispatcher's launch IS a run_ticket invocation: replay its argv in-process."""
    launched: list[list[str]] = []
    monkeypatch.setattr(fleet_dispatcher, "LOG_DIR", tmp_path / "fleet_logs")
    fake_popen = lambda cmd, **kw: launched.append(cmd) or MagicMock()  # noqa: E731
    monkeypatch.setattr(fleet_dispatcher, "subprocess", SimpleNamespace(Popen=fake_popen, STDOUT=-2))
    monkeypatch.setattr(fleet_dispatcher, "_create_operational_checkout", lambda *_args: tmp_path)
    monkeypatch.setenv("DARKFAC_FLEET_MODE", "0")
    fleet_dispatcher.default_launch(
        "USR-990", fleet_dispatcher.HostSpec(id="notebook", priority=1),
        {"id": "USR-990", "project_id": "darkfac", "title": "Stage sequence", "problem_statement": "test",
         "status": "planned"},
    )
    cmd = launched[0]
    assert Path(cmd[1]).name == "run_ticket.py" and cmd[2] == "USR-990"
    return run_ticket.main([*cmd[2:], "--harness", "codex", "--skip-validation"])


def _via_line(line: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> int:
    """The line's integration adapter refuses to merge without the plan and the verdict artifacts."""
    ws = line_workspace.RunWorkspace(
        project_id="darkfac", run_id="run-990", branch="df/run-990", path=tmp_path / "run", base_sha="0" * 40
    )
    line_workspace.write_context(ws, "DEMAND.md", "demanda\n")
    if line.missing != "plan":
        line_workspace.write_context(ws, "SPEC.md", "# SPEC\n")
        line_workspace.write_context(ws, "tickets.json", json.dumps([{"id": "T1", "title": "Sub um"}]))
    if line.missing != "verdict":
        line_workspace.write_context(ws, VERDICT_FILE, ReviewVerdict(verdict="approve").model_dump_json())
    monkeypatch.setattr(line_workspace, "checkout", lambda project, run_id: ws)

    class _Integration:
        def __init__(self, project: Any, **_kw: Any) -> None:
            pass

        def handle(self, context: Any) -> StageResult:
            line.merged("run-990")
            return StageResult(outcome="success", output_refs=["pr", "sha"])

    monkeypatch.setattr(stage_integration, "IntegrationStageHandler", _Integration)
    adapter = bindings.IntegrationStageAdapter(project_resolver=MagicMock())
    monkeypatch.setattr(bindings, "_resolve_project", lambda context, resolver: SimpleNamespace(id="darkfac"))
    context = SimpleNamespace(claim=SimpleNamespace(job_key=SimpleNamespace(run_id="run-990")))
    return 0 if adapter.handle(context).outcome == "success" else 1


_CASES = [
    *((entry, missing) for entry in ("cli", "fleet", "line") for missing in _MISSING
      if (entry, missing) != ("line", "deploy_report")),  # the line deploys after integration (build_deploy)
    ("cli", "nothing_small_fix"),
    ("cli", "plan_route"),
    ("fleet", "review_route"),
]


@pytest.mark.parametrize(("entry", "missing"), _CASES)
def test_the_ticket_reaches_merge_only_with_every_stage_artifact(
    entry: str, missing: str, line: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if missing == "nothing_small_fix":
        _assert_small_fix_goes_through_without_grill(line, capsys)
        return
    line.missing = missing
    if missing.endswith("_route"):
        _assert_waits_for_a_route_and_requeues(entry, line, monkeypatch, tmp_path, capsys)
        return
    captured = None
    if entry == "line":
        code = _via_line(line, monkeypatch, tmp_path)
    else:
        code = (_via_cli if entry == "cli" else _via_fleet)(line, monkeypatch, tmp_path)
        captured = capsys.readouterr()
        assert "[SUCESSO]" not in captured.out, captured.err

    assert code != 0
    if missing == "deploy_report":
        # Post-merge stage: the merge happened, but the run is not reported as delivered.
        line.merged.assert_called_once()
    else:
        line.merged.assert_not_called()
    if captured is not None:
        if missing == "plan":
            assert line.calls == ["PLAN"]  # BUILD never started without a plan
        else:
            assert line.calls[:3] == ["PLAN", "BUILD", "REVIEW"], captured.err


def _assert_waits_for_a_route_and_requeues(
    entry: str, line: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No eligible model WAITS: the fleet's requeue code (ticket stays planned), never a failure."""
    code = (_via_cli if entry == "cli" else _via_fleet)(line, monkeypatch, tmp_path)
    err = capsys.readouterr().err
    assert code in fleet_dispatcher.INFRA_EXIT_CODES and fleet_dispatcher.is_infra_failure(code, err), err
    assert "[AGUARDANDO]" in err
    line.merged.assert_not_called()
    if line.missing == "review_route":
        assert line.calls == ["PLAN", "BUILD", "REVIEW"]
        assert line.ticket.status.value == "planned"


def _assert_small_fix_goes_through_without_grill(line: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    line.ticket = _ticket("small")
    assert run_ticket.main(["USR-990", "--harness", "codex", "--skip-validation"]) == 0

    assert line.calls == ["BUILD", "REVIEW"]  # no planning agent, no grill
    rws = run_ticket.ticket_engine.run_workspace(line.ticket)
    context = line_workspace.context_dir(rws)
    assert not (context / "GRILL.md").exists()
    for name in ("DEMAND.md", "SPEC.md", "tickets.json", "progress.json", "validation.json",
                 VERDICT_FILE, "merge.json", "deploy_report.json"):
        assert (context / name).is_file(), name
    assert json.loads((context / "progress.json").read_text(encoding="utf-8"))["harness"] == "codex"
    deploy = json.loads((context / "deploy_report.json").read_text(encoding="utf-8"))
    assert deploy["smoke_ok"] is None and deploy["deployed"] is False  # smoke never claimed without running
    line.merged.assert_called_once()
    out = capsys.readouterr().out
    assert "[MERGE]" in out and "aguardando deploy" in out and "[SUCESSO]" not in out


def _context(line: SimpleNamespace) -> Path:
    return line_workspace.context_dir(run_ticket.ticket_engine.run_workspace(line.ticket))


def test_feature_without_research_artifact_never_reaches_merge(
    line: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """USR-376: RESEARCH is mandatory between PLAN and BUILD; a skip WITH its reason is an artifact."""
    monkeypatch.setattr(run_ticket.ticket_extras, "run_research", lambda *a, **k: SimpleNamespace(kind="none"))
    monkeypatch.setattr(run_ticket.ticket_extras, "static_checks_feedback", lambda *a, **k: None)
    assert run_ticket.main(["USR-990", "--harness", "codex", "--skip-validation"]) != 0
    assert "RESEARCH" in capsys.readouterr().err
    assert line.calls == ["PLAN"]  # BUILD never started
    line.merged.assert_not_called()


def test_research_without_web_search_is_skipped_with_reason_and_never_invents_sources(
    line: SimpleNamespace,
) -> None:
    assert run_ticket.main(["USR-990", "--harness", "codex", "--skip-validation"]) == 0
    research = json.loads((_context(line) / "research.json").read_text(encoding="utf-8"))
    assert research["kind"] == "research_skipped" and research["sources"] == []
    assert research["reason"] == "busca web indisponivel neste host"
    line.ticket = _ticket("small")
    assert run_ticket.main(["USR-990", "--harness", "codex", "--skip-validation"]) == 0
    research = json.loads((_context(line) / "research.json").read_text(encoding="utf-8"))
    assert research["kind"] == "research_skipped" and research["reason"].startswith("R3")  # policy skip


def test_success_reports_merge_and_pending_deploy_without_global_sweep(
    line: SimpleNamespace, capsys: pytest.CaptureFixture[str],
) -> None:
    """A successful merge persists complete evidence outside the removable checkout and never sweeps peers."""
    assert run_ticket.main(["USR-990", "--harness", "codex", "--skip-validation", "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    artifact_dir = run_ticket.ticket_engine.workspace.context_dir(run_ticket.ticket_engine.run_workspace(line.ticket))
    assert line.sweep_calls == []
    assert (line.launcher_checkout / "launcher-state.json").read_text(encoding="utf-8") == "active"
    assert (artifact_dir / "merge.json").is_file()
    assert (artifact_dir / "deploy_report.json").is_file()
    assert output["status"] == "merged_awaiting_deploy"
    assert output["pr_url"] == "https://github.com/example/repo/pull/42"
    assert output["merge_sha"] == "d" * 40
    assert output["deploy_status"] == "awaiting_deploy"
    assert Path(output["deploy_report"]) == artifact_dir / "deploy_report.json"


def test_learning_pack_is_recorded_and_indexed_after_the_merge(
    line: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from core.learning_pack.delivery import LearningPackArtifact

    seen: list[str] = []

    def build(_repo: Path, ticket_id: str, base: str, head: str = "HEAD") -> LearningPackArtifact:
        seen.append("merged" if line.merged.called else "before-merge")
        return LearningPackArtifact(
            ticket=ticket_id, base_ref=base, head_ref=head, layman="Troca simples.", dev="Mudou feature.py."
        )

    monkeypatch.setattr(run_ticket.ticket_extras, "build_learning", build)
    assert run_ticket.main(["USR-990", "--harness", "codex", "--skip-validation"]) == 0

    store = tmp_path / "state" / "learning_packs"
    index = json.loads((store / "packs_index.json").read_text(encoding="utf-8"))
    assert [entry["pack_id"] for entry in index] == ["lp-usr-990"]  # what the Hub and the daily digest read
    assert (store / index[0]["file_html"]).is_file() and (_context(line) / "learning_pack.json").is_file()
    assert seen == ["before-merge"]
    # a pack that cannot be generated never undoes a merged delivery
    monkeypatch.setattr(run_ticket.ticket_extras, "build_learning", lambda *a, **k: None)
    assert run_ticket.main(["USR-990", "--harness", "codex", "--skip-validation"]) == 0


def test_docs_sync_failure_goes_back_to_build_and_blocks_delivery_if_unfixed(
    line: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fixes: list[str] = []
    monkeypatch.setattr(run_ticket.ticket_extras, "docs_sync_feedback", lambda *_a, **_k: "docs-sync: falta motor.md")
    monkeypatch.setattr(run_ticket.ticket_extras, "static_checks_feedback", lambda *_a, **_k: None)
    monkeypatch.setattr(run_ticket, "_invoke_fix_agent", lambda wt, t, label, feedback: fixes.append(feedback))
    monkeypatch.setattr(run_ticket, "_commit_fix_agent_changes_safely", lambda *a: (True, None))

    assert run_ticket.main(["USR-990", "--harness", "codex", "--skip-validation"]) != 0

    assert fixes == ["docs-sync: falta motor.md"]  # the agent got the message once
    assert "DOCS" in capsys.readouterr().err
    assert "REVIEW" not in line.calls
    line.merged.assert_not_called()


def test_ratchet_failure_goes_back_to_agent_once_with_combined_output(
    line: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fixes: list[str] = []
    monkeypatch.setattr(run_ticket.ticket_extras, "docs_sync_feedback", lambda *_a, **_k: "docs-sync: falta motor.md")
    monkeypatch.setattr(run_ticket.ticket_extras, "static_checks_feedback", lambda *_a, **_k: "complexity_ratchet reprovou")
    monkeypatch.setattr(run_ticket, "_invoke_fix_agent", lambda wt, t, label, feedback: fixes.append(feedback))
    monkeypatch.setattr(run_ticket, "_commit_fix_agent_changes_safely", lambda *a: (True, None))

    assert run_ticket.main(["USR-990", "--harness", "codex", "--skip-validation"]) != 0

    assert len(fixes) == 1 and "falta motor.md" in fixes[0] and "complexity_ratchet reprovou" in fixes[0]
    assert "REVIEW" not in line.calls
