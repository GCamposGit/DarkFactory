"""Tests for the harness-agnostic operational turn guard (USR-431)."""

import json
import subprocess
from pathlib import Path
import pytest

from core.ops.turn_guard import (
    TurnGuardReport,
    check_learning_pack,
    detect_ticket,
    verify_turn,
    start_session,
    finish_session,
    main,
)


def test_detect_ticket_from_env(monkeypatch):
    monkeypatch.setenv("DARKFAC_TICKET_ID", "USR-999")
    assert detect_ticket(Path(".")) == "USR-999"


def test_detect_ticket_from_branch(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DARKFAC_TICKET_ID", raising=False)
    # Mock git rev-parse
    def mock_git(cwd, *args):
        if "rev-parse" in args:
            return "ticket/usr-431\n"
        return ""
    monkeypatch.setattr("core.ops.turn_guard._git", mock_git)
    assert detect_ticket(tmp_path) == "USR-431"


def test_check_learning_pack_no_code_changes(tmp_path: Path, monkeypatch):
    def mock_git(cwd, *args):
        if "diff" in args:
            return "docs/manual/operacao.md\n"
        if "status" in args:
            return ""
        return ""
    monkeypatch.setattr("core.ops.turn_guard._git", mock_git)
    assert check_learning_pack(tmp_path, ticket_id="USR-431") is None


def test_check_learning_pack_detects_missing(tmp_path: Path, monkeypatch):
    def mock_git(cwd, *args):
        if "diff" in args:
            return "core/ops/turn_guard.py\n"
        if "status" in args:
            return ""
        return ""
    monkeypatch.setattr("core.ops.turn_guard._git", mock_git)
    reason = check_learning_pack(tmp_path, ticket_id="USR-431")
    assert reason is not None
    assert "sem learning pack" in reason
    assert "USR-431" in reason


def test_check_learning_pack_passes_when_pack_present(tmp_path: Path, monkeypatch):
    def mock_git(cwd, *args):
        if "diff" in args:
            return "core/ops/turn_guard.py\n"
        if "status" in args:
            return ""
        return ""
    monkeypatch.setattr("core.ops.turn_guard._git", mock_git)
    pack_dir = tmp_path / ".factory" / "learning_packs"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "USR-431.json").write_text("{}", encoding="utf-8")

    assert check_learning_pack(tmp_path, ticket_id="USR-431") is None


def test_verify_turn_full_report(tmp_path: Path, monkeypatch):
    def mock_git(cwd, *args):
        if "diff" in args:
            return "docs/operacao/HANDOFF.md\n"
        if "status" in args:
            return ""
        return ""
    monkeypatch.setattr("core.ops.turn_guard._git", mock_git)
    handoff_path = tmp_path / "docs" / "operacao" / "HANDOFF.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text("# Handoff\nVersao: 8\n", encoding="utf-8")

    report = verify_turn(tmp_path, ticket_id="USR-431")
    assert isinstance(report, TurnGuardReport)
    assert report.ok is True
    assert report.learning_pack_ok is True
    assert report.handoff_ok is True


def test_session_lifecycle_agnostic(tmp_path: Path, monkeypatch):
    calls = []
    def mock_exec_cli(args):
        calls.append(args)
        if args[0] == "start":
            return "exec-test-123\n"
        return "ok\n"

    monkeypatch.setattr("core.ops.turn_guard._run_exec_cli", mock_exec_cli)
    monkeypatch.setattr("core.ops.turn_guard.state_dir", lambda: tmp_path)

    eid = start_session(harness="codex", stage="development", ticket_id="USR-431")
    assert eid == "exec-test-123"
    assert (tmp_path / "ses-codex.id").read_text(encoding="utf-8") == "exec-test-123"

    ok = finish_session(harness="codex", status="succeeded", evidence="PR #999")
    assert ok is True
    assert any("finish" in call for call in calls)


def test_turn_guard_cli_exit_code_zero(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("core.ops.turn_guard.detect_repo_root", lambda: tmp_path)
    monkeypatch.setattr("core.ops.turn_guard.check_learning_pack", lambda *args, **kwargs: None)
    handoff = tmp_path / "docs" / "operacao" / "HANDOFF.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("# Handoff\n", encoding="utf-8")

    code = main(["--check"])
    assert code == 0


def test_turn_guard_cli_exit_code_two_on_violation(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr("core.ops.turn_guard.detect_repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        "core.ops.turn_guard.check_learning_pack",
        lambda *args, **kwargs: "Sessao alterou codigo para USR-431 sem learning pack.",
    )

    code = main(["--check"])
    assert code == 2
    captured = capsys.readouterr()
    assert "sem learning pack" in captured.err
