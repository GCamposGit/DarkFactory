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


def test_run_ticket_no_args_displays_quotas(capsys: pytest.CaptureFixture[str]) -> None:
    """Calling run_ticket with no arguments prints the quota summary and exits cleanly (0)."""
    exit_code = main([])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Telemetria de Cotas em Tempo Real" in captured.out
    assert "Nenhum ticket especificado" in captured.out


def test_run_ticket_dry_run_auto_picks_antigravity(capsys: pytest.CaptureFixture[str]) -> None:
    """With real quotas, dry-run on an existing ticket must select Antigravity automatically."""
    exit_code = main(["USR-01", "--dry-run"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "antigravity" in captured.out.lower()
    assert "[DRY RUN]" in captured.out


def test_run_ticket_blocks_critical_harness_without_override(capsys: pytest.CaptureFixture[str]) -> None:
    """Explicitly requesting a critical harness without override must fail closed with exit code 2."""
    fake_quotas = {
        "codex": {"provider": "openai", "headroom": 2.0, "is_critical": True, "status": "CRÍTICO (<= 15%)"},
        "antigravity": {"provider": "google", "headroom": 50.0, "is_critical": False, "status": "SAUDÁVEL"},
    }
    with patch("run_ticket.inspect_quotas", return_value=fake_quotas):
        exit_code = main(["USR-01", "--harness", "codex", "--dry-run"])
        assert exit_code == 2
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
