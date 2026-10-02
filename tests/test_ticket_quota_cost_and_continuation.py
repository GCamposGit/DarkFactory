"""Tests for USR-113: ticket size estimation, continuation prompt, and quota cost recording."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import run_ticket
from core.demands.models import UserTicket
from core.usage.ticket_cost import (
    format_ticket_quota_summary,
    read_ticket_quota_history,
    record_ticket_quota_cost,
)


def test_estimate_ticket_size_normal() -> None:
    ticket = UserTicket(
        id="USR-10",
        project_id="darkfac",
        title="Pequeno ajuste",
        problem_statement="Problema simples",
        acceptance_criteria=["Critério 1", "Critério 2"],
        estimated_complexity="small",
    )
    size, reasons = run_ticket.estimate_ticket_size(ticket)
    assert size == "small"
    assert reasons == []


def test_estimate_ticket_size_large_by_complexity() -> None:
    ticket = UserTicket(
        id="USR-11",
        project_id="darkfac",
        title="Refatoração profunda",
        problem_statement="Problema normal",
        acceptance_criteria=["Critério 1"],
        estimated_complexity="large",
    )
    size, reasons = run_ticket.estimate_ticket_size(ticket)
    assert size == "large"
    assert any("complexidade" in r for r in reasons)


def test_estimate_ticket_size_large_by_many_criteria() -> None:
    ticket = UserTicket(
        id="USR-12",
        project_id="darkfac",
        title="Feature com muitos requisitos",
        problem_statement="Problema normal",
        acceptance_criteria=["C1", "C2", "C3", "C4", "C5", "C6"],
        estimated_complexity="medium",
    )
    size, reasons = run_ticket.estimate_ticket_size(ticket)
    assert size == "large"
    assert any("critérios de aceite" in r for r in reasons)


def test_estimate_ticket_size_large_by_problem_length() -> None:
    ticket = UserTicket(
        id="USR-13",
        project_id="darkfac",
        title="Problema muito detalhado",
        problem_statement="X" * 1050,
        acceptance_criteria=["C1"],
        estimated_complexity="medium",
    )
    size, reasons = run_ticket.estimate_ticket_size(ticket)
    assert size == "large"
    assert any("descrição do problema extensa" in r for r in reasons)


def test_estimate_ticket_size_large_by_tag() -> None:
    ticket = UserTicket(
        id="USR-14",
        project_id="darkfac",
        title="Epic ticket",
        problem_statement="Problema normal",
        acceptance_criteria=["C1"],
        tags=["user-demand", "epic"],
        estimated_complexity="medium",
    )
    size, reasons = run_ticket.estimate_ticket_size(ticket)
    assert size == "large"
    assert any("epic" in r for r in reasons)


def test_record_ticket_quota_cost_persists_and_reads(tmp_path: Path) -> None:
    record = record_ticket_quota_cost(
        ticket_id="USR-94",
        harness="codex",
        before_headroom=44.0,
        after_headroom=16.0,
        duration_s=2880.0,
        reports_dir=tmp_path,
    )
    assert record["delta_headroom"] == 28.0
    assert record["ticket_id"] == "USR-94"
    assert record["harness"] == "codex"

    summary = format_ticket_quota_summary(record)
    assert "USR-94" in summary
    assert "codex" in summary
    assert "44.0%" in summary
    assert "16.0%" in summary
    assert "+28.0%" in summary

    history = read_ticket_quota_history(reports_dir=tmp_path)
    assert len(history) == 1
    assert history[0]["ticket_id"] == "USR-94"


def test_dry_run_reports_estimated_size_and_reasons(capsys: pytest.CaptureFixture[str]) -> None:
    fake_ticket = UserTicket(
        id="USR-94",
        project_id="darkfac",
        title="Refatoração grande",
        problem_statement="Problema extenso " * 60,
        acceptance_criteria=["C1", "C2", "C3", "C4", "C5"],
        estimated_complexity="large",
    )
    fake_quotas = {
        "claude": {"provider": "anthropic", "headroom": 50.0, "is_critical": False, "status": "SAUDÁVEL"},
    }
    with patch("run_ticket.inspect_quotas", return_value=fake_quotas), \
         patch("run_ticket.DemandsStore.get_ticket", return_value=fake_ticket), \
         patch("run_ticket.pick", return_value=("claude", "sonnet")):
        exit_code = run_ticket.main(["USR-94", "--dry-run", "--json"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["estimated_size"] == "large"
        assert len(data["size_reasons"]) >= 2


def test_large_ticket_prints_warning_box_before_spending_quota(capsys: pytest.CaptureFixture[str]) -> None:
    fake_ticket = UserTicket(
        id="USR-94",
        project_id="darkfac",
        title="Demanda volumosa",
        problem_statement="Texto curto",
        acceptance_criteria=["C1", "C2", "C3", "C4", "C5", "C6"],
        estimated_complexity="large",
    )
    fake_quotas = {
        "claude": {"provider": "anthropic", "headroom": 50.0, "is_critical": False, "status": "SAUDÁVEL"},
    }
    with patch("run_ticket.inspect_quotas", return_value=fake_quotas), \
         patch("run_ticket.DemandsStore.get_ticket", return_value=fake_ticket), \
         patch("run_ticket.pick", return_value=("claude", "sonnet")):
        exit_code = run_ticket.main(["USR-94", "--dry-run"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "AVISO: TICKET ESTIMADO COMO GRANDE" in captured.err
        assert "USR-94" in captured.err
        assert "timeout do agente" in captured.err


def test_continuation_prompt_injected_when_workspace_has_changes(tmp_path: Path) -> None:
    fake_ticket = UserTicket(
        id="USR-94",
        project_id="darkfac",
        title="Continuação do ticket",
        problem_statement="Problema normal",
        acceptance_criteria=["C1"],
    )
    fake_workspace = MagicMock()
    fake_workspace.path = tmp_path
    fake_workspace.branch = "ticket/usr-94"
    fake_workspace.base_ref = "origin/main"
    fake_workspace.base_sha = "a" * 40

    # Cria arquivo simulando trabalho prévio na worktree
    (tmp_path / "core").mkdir(parents=True, exist_ok=True)
    foo_file = tmp_path / "core" / "foo.py"
    foo_file.write_text("# preexisting implementation", encoding="utf-8")

    fake_report = MagicMock()
    fake_report.ok = True
    fake_report.result = MagicMock()
    fake_report.result.text = "All tests pass"
    fake_report.result.duration_s = 45.0
    fake_report.route = ("claude", "sonnet")
    fake_report.total_duration_s = 45.0

    captured_requests: list[Any] = []

    def fake_run_with_retry(build_req, route, **kwargs):
        req = build_req("claude", "sonnet")
        captured_requests.append(req)
        return fake_report

    with patch("run_ticket.inspect_quotas", return_value={"claude": {"headroom": 50.0, "is_critical": False}}), \
         patch("run_ticket.DemandsStore.get_ticket", return_value=fake_ticket), \
         patch("run_ticket._prepare_workspace", return_value=fake_workspace), \
         patch("run_ticket.agent_retry.run_with_retry", side_effect=fake_run_with_retry), \
         patch("core.git.autonomy.GitAutonomyManager.changed_paths", return_value=["core/foo.py"]), \
         patch("run_ticket.subprocess.run", return_value=MagicMock(returncode=0)), \
         patch("core.git.autonomy.GitAutonomyManager.complete_ticket", return_value=MagicMock(ok=True, commit_sha="b" * 40, sync_result=None)):
        exit_code = run_ticket.main(["USR-94", "--harness", "claude", "--json"])
        assert exit_code == 0
        assert len(captured_requests) == 1
        prompt = captured_requests[0].prompt
        assert "AVISO DE CONTINUAÇÃO" in prompt
        assert "core/foo.py" in prompt
        assert "NÃO recomece a implementação do zero" in prompt

