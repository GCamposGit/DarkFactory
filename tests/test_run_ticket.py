"""Tests for run_ticket.py (Skill 19-run-ticket)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import run_ticket
from run_ticket import check_explicit_override, format_quota_report, inspect_quotas, main


def test_explicit_override_detection() -> None:
    """Explicit override must only be granted when force flag is set or prompt contains authorized keywords."""
    # Negative cases (no override)
    assert not check_explicit_override(None, force_flag=False)
    assert not check_explicit_override("", force_flag=False)
    assert not check_explicit_override("Implementar a feature USR-59 com testes", force_flag=False)
    assert not check_explicit_override("Por favor desenvolva este ticket", force_flag=False)

    # Positive cases via flag
    assert check_explicit_override(None, force_flag=True)
    assert check_explicit_override("qualquer prompt", force_flag=True)

    # Positive cases via prompt keywords
    assert check_explicit_override("por favor forçar execucao local", force_flag=False)
    assert check_explicit_override("forcar o desenvolvimento no codex", force_flag=False)
    assert check_explicit_override("force execution on current harness", force_flag=False)
    assert check_explicit_override("pode ignorar cota e implementar", force_flag=False)
    assert check_explicit_override("ignorar limite de tokens", force_flag=False)
    assert check_explicit_override("prossiga mesmo com cota critica", force_flag=False)
    assert check_explicit_override("allow-critical-quota", force_flag=False)


def test_format_quota_report() -> None:
    """Format quota report generates a human-readable table."""
    fake_quotas = {
        "antigravity": {"provider": "google", "headroom": 55.0, "is_critical": False, "status": "SAUDÁVEL"},
        "codex": {"provider": "openai", "headroom": 2.0, "is_critical": True, "status": "CRÍTICO (<= 15%)"},
        "claude": {"provider": "anthropic", "headroom": 3.0, "is_critical": True, "status": "CRÍTICO (<= 15%)"},
        "grok": {"provider": "xai", "headroom": 2.8, "is_critical": True, "status": "CRÍTICO (<= 15%)"},
    }
    report = format_quota_report(fake_quotas)
    assert "Telemetria de Cotas em Tempo Real" in report
    assert "antigravity" in report
    assert "SAUDÁVEL" in report
    assert "CRÍTICO" in report


def test_run_ticket_no_args_displays_quotas(capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    """Calling run_ticket with no arguments prints the quota summary and exits cleanly (0)."""
    monkeypatch.setattr(run_ticket, "inspect_quotas", lambda: {})
    exit_code = main([])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Telemetria de Cotas em Tempo Real" in captured.out
    assert "Nenhum ticket especificado" in captured.out


def test_run_ticket_dry_run_auto_picks_a_write_capable_route(capsys: pytest.CaptureFixture[str]) -> None:
    """Dry-run reports the healthy write-capable route selected by the quota router (Antigravity cannot write)."""
    fake_quotas = {
        "claude": {"provider": "anthropic", "headroom": 50.0, "is_critical": False, "status": "SAUDÁVEL"},
    }
    with patch("run_ticket.inspect_quotas", return_value=fake_quotas), \
         patch("run_ticket.pick", return_value=("claude", "sonnet")) as picked:
        exit_code = main(["USR-01", "--dry-run"])
    assert exit_code == 0
    assert picked.call_args.kwargs["mode"] == "write"
    captured = capsys.readouterr()
    assert "claude" in captured.out.lower()
    assert "[DRY RUN]" in captured.out


def test_run_ticket_blocks_critical_harness_without_override(capsys: pytest.CaptureFixture[str]) -> None:
    """Explicitly requesting a critical harness without override must fail closed with exit code 2."""
    fake_quotas = {
        "codex": {"provider": "openai", "headroom": 2.0, "is_critical": True, "status": "CRÍTICO (<= 15%)"},
        "antigravity": {"provider": "google", "headroom": 50.0, "is_critical": False, "status": "SAUDÁVEL"},
    }
    with patch("run_ticket.inspect_quotas", return_value=fake_quotas), \
         patch("run_ticket.pick", return_value=("antigravity", None)):
        exit_code = main(["USR-01", "--harness", "codex", "--dry-run"])
        assert exit_code == 75
        captured = capsys.readouterr()
        assert "BLOQUEIO DE SEGURANÇA - FAIL-CLOSED" in captured.err
        assert "codex" in captured.err
        assert "Recomendação do Roteador" in captured.err


def test_run_ticket_allows_critical_harness_with_explicit_prompt_override(capsys: pytest.CaptureFixture[str]) -> None:
    """Requesting a critical harness WITH explicit prompt override must proceed and print a warning."""
    fake_quotas = {
        "codex": {"provider": "openai", "headroom": 2.0, "is_critical": True, "status": "CRÍTICO (<= 15%)"},
        "antigravity": {"provider": "google", "headroom": 50.0, "is_critical": False, "status": "SAUDÁVEL"},
    }
    with patch("run_ticket.inspect_quotas", return_value=fake_quotas):
        exit_code = main(["USR-01", "--harness", "codex", "--prompt", "por favor forçar execução mesmo sem cota", "--dry-run"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Override explícito do usuário ativo" in captured.out
        assert "codex" in captured.out


def test_skills_synchronization_has_19_run_ticket() -> None:
    """Skill 19-run-ticket must be present in both .agents/skills and .claude/skills."""
    agent_skill = Path(__file__).resolve().parents[1] / ".agents" / "skills" / "19-run-ticket" / "SKILL.md"
    claude_skill = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "19-run-ticket" / "SKILL.md"

    assert agent_skill.is_file(), f"Missing agent skill at {agent_skill}"
    assert claude_skill.is_file(), f"Missing claude skill at {claude_skill}"
    assert "19 - Run Ticket" in agent_skill.read_text(encoding="utf-8")
    assert agent_skill.read_text(encoding="utf-8") == claude_skill.read_text(encoding="utf-8")


def test_queue_only_registers_ticket_without_running_agent(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--create --queue-only files the ticket in the queue and never invokes an agent."""
    queued = run_ticket.UserTicket(id="USR-99", project_id="darkfac", title="Defeito X", problem_statement="p")
    with patch("run_ticket._queue_ticket_isolated", return_value=queued) as queue,          patch("run_ticket.run_agent") as agent:
        exit_code = main(["--create", "--queue-only", "--title", "Defeito X", "--problem", "p"])
    assert exit_code == 0
    queue.assert_called_once()
    agent.assert_not_called()
    assert "USR-99" in capsys.readouterr().out

    # USR-343: the real registration lands in the control store; no worktree/PR path is touched.
    from core.demands import queue_store

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("DARKFAC_HF02_DATABASE_URL", raising=False)
    queue_store._store.cache_clear()
    args = run_ticket.build_parser().parse_args(["--create", "--queue-only", "--title", "Defeito Y", "--project", "produto", "--priority", "8"])
    with patch.object(run_ticket.DemandsStore, "next_ticket_id", return_value="USR-998"),             patch("core.git.ticket_workspace.create_workspace") as workspace:
        ticket = run_ticket._queue_ticket_isolated(args)
    workspace.assert_not_called()
    assert ticket is not None and ticket.id == "USR-998"
    assert ticket.project_id == "produto" and ticket.priority == 8
    queued_rows = queue_store.list_queue_tickets()
    assert [t.id for t in queued_rows] == ["USR-998"]
    assert queued_rows[0].project_id == "produto" and queued_rows[0].priority == 8
    queue_store._store.cache_clear()


def test_queue_only_fails_when_control_store_is_unavailable(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("run_ticket._queue_ticket_isolated", return_value=None):
        exit_code = main(["--create", "--queue-only", "--title", "Defeito X"])
    assert exit_code == 1
    assert "fila" in capsys.readouterr().err


def test_build_ticket_uses_next_sequential_id(tmp_path: Path) -> None:
    from core.demands.store import DemandsStore

    store = DemandsStore(tmp_path / "demands.json")
    store.save_ticket(run_ticket.UserTicket(id="USR-07", project_id="darkfac", title="alpha ticket", problem_statement="problema suficientemente longo"))
    args = run_ticket.build_parser().parse_args(["--create", "--title", "beta ticket", "--problem", "problema suficientemente longo", "--criteria", "c1"])
    ticket = run_ticket._build_ticket(args, store)
    assert ticket.id == "USR-08"
    assert ticket.acceptance_criteria == ["c1"]
@pytest.mark.parametrize(("cause", "stage", "expected"), [
    ("route_wait", "preflight", 75), ("context", "plan", 75),
    ("agent_timeout", "agent", 1), ("gate_timeout", "gate", 75),
    ("gate_remote_unavailable", "gate", 75), ("gate_dirty_worktree", "gate", 75),
    ("gate_infra_no_tests", "gate", 75), ("base_red_suspected", "gate", 75),
    ("gate_insufficient_ram", "gate", 75), ("network", "merge", 75),
    ("gate_failed", "gate", 1), ("review_changes_required", "review", 1),
    ("ci_red", "ci", 1), ("agent_no_changes", "agent", 1),
])
def test_all_launcher_stage_failures_write_outcome_and_charge_once(tmp_path, monkeypatch, cause, stage, expected):
    import run_ticket
    from core.line import run_outcome
    from core.line.admission_store import AdmissionStore
    from core.line.local_progress import ProgressPublisher
    from core.paths import state_root

    def failed(_argv, box):
        progress = ProgressPublisher(ticket_id="USR-99", project_id="darkfac", title="Failure", sink=None)
        box.append(progress)
        run_outcome.bind_attempt("USR-99", progress=progress)
        progress.phase(stage, "failed", "AssertionError: actual diagnostic", cause=cause)
        print("Quota summary: headroom 80%")
        return 1

    monkeypatch.setattr(run_ticket, "_main", failed)
    assert run_ticket.main(["USR-99"]) == expected
    path = run_outcome.outcome_path("USR-99")
    outcome = run_outcome.RunOutcome.model_validate_json(path.read_text(encoding="utf-8"))
    assert outcome.cause == cause and outcome.stage == stage
    assert outcome.detail == "AssertionError: actual diagnostic"
    assert outcome.exit_code == expected
    store = AdmissionStore(state_root() / "admission.sqlite3")
    store.record_attempt("darkfac", "USR-99", cause, run_id=outcome.run_id)
    budget = store.budget("darkfac", "USR-99")
    assert budget["candidate_failures"] == int(expected != 75)


def test_early_host_permission_error_has_outcome_and_infra_exit(tmp_path, monkeypatch):
    import run_ticket
    from core.line import run_outcome

    def denied(_argv, _box):
        run_outcome.bind_attempt("USR-99")
        raise PermissionError("host ACL denied")

    monkeypatch.setattr(run_ticket, "_main", denied)
    assert run_ticket.main(["USR-99"]) == 75
    outcome = run_outcome.RunOutcome.model_validate_json(run_outcome.outcome_path("USR-99").read_text(encoding="utf-8"))
    assert outcome.cause == "host_permission"
    assert outcome.detail == "host ACL denied"


def test_ci_exhaustion_in_two_runs_survives_context_deletion_and_counts_twice(tmp_path, monkeypatch):
    import run_ticket
    from core.line import run_outcome
    from core.line.admission_store import AdmissionStore
    from core.paths import state_root
    from types import SimpleNamespace

    ticket = SimpleNamespace(id="USR-99", project_id="darkfac")
    fixes = []
    monkeypatch.setattr(run_ticket, "_invoke_fix_agent", lambda *args: fixes.append(args))
    monkeypatch.setattr(run_ticket, "_commit_fix_agent_changes_safely", lambda *args: (True, None))
    progress = SimpleNamespace(phase=lambda *args: None)
    report = SimpleNamespace(delivery=SimpleNamespace(message="FAILED tests/test_real.py::test_behavior"))
    _, first = run_ticket._retry_red_ci(None, ticket, tmp_path, report, False, progress)
    assert first == "ci_fix_requires_revalidation"
    context = tmp_path / "context"
    context.mkdir()
    (context / "ci_recovery.json").write_text("{}", encoding="utf-8")
    (context / "ci_recovery.json").unlink()
    context.rmdir()

    class Store:
        def __init__(self, *args):
            self.status = None
        def requeue_ticket(self, *args, **kwargs):
            self.status = "planned"
        def get_ticket(self, *args):
            return SimpleNamespace(status=self.status)
    monkeypatch.setattr(run_ticket, "DemandsStore", Store)

    @run_outcome.tracked
    def exhausted_run():
        run_outcome.bind_attempt(ticket.id, ticket.project_id)
        _, reason = run_ticket._retry_red_ci(None, ticket, tmp_path, report, False, progress)
        assert reason.startswith("ci_recovery_budget_exhausted")
        assert run_ticket._reopen_after_ci_budget(ticket, tmp_path, reason)
        return {"exit_code": 1, "cause": "ci_red", "error": reason}

    for index in range(2):
        assert exhausted_run()["exit_code"] == 1
        assert AdmissionStore(state_root() / "admission.sqlite3").budget("darkfac", ticket.id)["candidate_failures"] == index + 1
    assert len(fixes) == 1


def test_ci_fix_without_changes_counts_candidate_instead_of_waiting_for_owner(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from core.line import run_outcome
    from core.line.admission_store import AdmissionStore
    from core.paths import state_root

    ticket = SimpleNamespace(id="USR-99", project_id="darkfac")
    requeues = []
    class Store:
        def __init__(self, *args):
            self.status = "planned"
        def requeue_ticket(self, *args, **kwargs):
            requeues.append(args)
        def get_ticket(self, *args):
            return SimpleNamespace(status=self.status)
    monkeypatch.setattr(run_ticket, "DemandsStore", Store)
    monkeypatch.setattr(run_ticket, "_park_waiting_human", lambda *args: pytest.fail("owner before third candidate failure"))
    progress = SimpleNamespace(phase=lambda *args, **kwargs: None, set_outcome=lambda *args: None)
    @run_outcome.tracked
    def failed_ci():
        run_outcome.bind_attempt(ticket.id)
        return run_ticket._ci_red_twice_result(ticket, "ci_fix_no_changes: FAILED behavior", tmp_path,
                                              "branch", "resume", progress, False, True)
    assert failed_ci()["exit_code"] == 1
    assert requeues == [("USR-99",)]
    assert AdmissionStore(state_root() / "admission.sqlite3").budget("darkfac", ticket.id)["candidate_failures"] == 1
