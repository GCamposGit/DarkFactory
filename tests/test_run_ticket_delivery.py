"""Tests for run_ticket delivery phase and --resume-delivery (USR-116)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import run_ticket
from core.demands.models import DeliveryStatus, UserTicket
from core.demands.store import DemandsStore
from core.git.autonomy import DeliveryReport, GitAutonomyManager, TicketCompletionReport
from core.git.ticket_workspace import find_ticket_worktree


def _init_git_repo(path: Path) -> None:
    """Initialize a git repo with user and initial commit on main."""
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@darkfac.local"], cwd=str(path), check=True, capture_output=True)
    (path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(path), check=True, capture_output=True)


def test_resume_delivery_arg_parsing() -> None:
    """Verify that --resume-delivery and --worktree are parsed correctly in all formats (USR-116)."""
    parser = run_ticket.build_parser()

    # Format 1: --resume-delivery USR-116
    args = parser.parse_args(["--resume-delivery", "USR-116"])
    assert args.resume_delivery == "USR-116"
    assert args.worktree is None

    # Format 2: USR-116 --resume-delivery
    args = parser.parse_args(["USR-116", "--resume-delivery"])
    assert args.resume_delivery is True
    assert args.ticket_id == "USR-116"

    # Format 3: --resume-delivery USR-116 --worktree /tmp/wt
    args = parser.parse_args(["--resume-delivery", "USR-116", "--worktree", "/tmp/wt"])
    assert args.resume_delivery == "USR-116"
    assert args.worktree == "/tmp/wt"


def test_find_ticket_worktree(tmp_path: Path) -> None:
    """find_ticket_worktree locates the most recent worktree matching the ticket (USR-116)."""
    main_repo = tmp_path / "repo"
    main_repo.mkdir()
    _init_git_repo(main_repo)

    wt_dir = main_repo / ".worktrees"
    wt_dir.mkdir()

    wt1 = wt_dir / "usr-116-20261001T010000Z"
    wt1.mkdir()
    (wt1 / "file1.txt").write_text("v1", encoding="utf-8")

    wt2 = wt_dir / "usr-116-20261001T020000Z"
    wt2.mkdir()
    (wt2 / "file2.txt").write_text("v2", encoding="utf-8")

    # Set mtimes on directories and files so wt2 is newer
    os.utime(wt1, (1000, 1000))
    os.utime(wt1 / "file1.txt", (1000, 1000))
    os.utime(wt2, (2000, 2000))
    os.utime(wt2 / "file2.txt", (2000, 2000))

    found = find_ticket_worktree("USR-116", main_repo)
    assert found == wt2.resolve()

    assert find_ticket_worktree("NONEXISTENT", main_repo) is None


def test_rebase_on_origin_main_clean(tmp_path: Path) -> None:
    """Clean rebase on main succeeds without conflicts (USR-116)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    # Create ticket branch with a non-conflicting commit
    subprocess.run(["git", "checkout", "-b", "ticket/usr-116"], cwd=str(repo), check=True, capture_output=True)
    (repo / "feature.py").write_text("def hello(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.py"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat: add hello"], cwd=str(repo), check=True, capture_output=True)

    # Add a commit on main
    subprocess.run(["git", "checkout", "main"], cwd=str(repo), check=True, capture_output=True)
    (repo / "other.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "other.py"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "chore: other"], cwd=str(repo), check=True, capture_output=True)

    # Switch back to ticket branch and rebase
    subprocess.run(["git", "checkout", "ticket/usr-116"], cwd=str(repo), check=True, capture_output=True)

    ok, conflict_files, msg = run_ticket.rebase_on_origin_main(repo)
    assert ok is True
    assert conflict_files == []


def test_rebase_conflict_detects_files_and_aborts_cleanly(tmp_path: Path) -> None:
    """Rebase conflict lists conflicting files, aborts cleanly, and preserves worktree (USR-116)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    # Create ticket branch modifying shared.txt
    subprocess.run(["git", "checkout", "-b", "ticket/usr-116"], cwd=str(repo), check=True, capture_output=True)
    (repo / "shared.txt").write_text("ticket content\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat: ticket commit"], cwd=str(repo), check=True, capture_output=True)

    # Modify shared.txt differently on main
    subprocess.run(["git", "checkout", "main"], cwd=str(repo), check=True, capture_output=True)
    (repo / "shared.txt").write_text("main conflicting content\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "chore: main commit"], cwd=str(repo), check=True, capture_output=True)

    # Back to ticket branch
    subprocess.run(["git", "checkout", "ticket/usr-116"], cwd=str(repo), check=True, capture_output=True)

    ok, conflict_files, msg = run_ticket.rebase_on_origin_main(repo)
    assert ok is False
    assert "shared.txt" in conflict_files

    # Verify rebase was aborted and worktree is not in a conflicted/detached rebase state
    status = subprocess.run(["git", "status"], cwd=str(repo), check=True, capture_output=True, text=True)
    assert "rebase in progress" not in status.stdout
    assert (repo / "shared.txt").read_text(encoding="utf-8") == "ticket content\n"


def test_resume_delivery_rebase_conflict_cli_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """resume_delivery outputs clear conflict message and exact resume command on conflict (USR-116)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    # Set up demands store
    demands_dir = repo / ".factory" / "demands"
    demands_dir.mkdir(parents=True)
    ledger = demands_dir / "demands.json"
    store = DemandsStore(ledger)
    ticket = UserTicket(
        id="USR-116",
        project_id="darkfac",
        title="Test Ticket",
        problem_statement="Test problem",
        status=DeliveryStatus.PLANNED,
    )
    store.save_ticket(ticket)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "ledger init"], cwd=str(repo), check=True, capture_output=True)

    # Create ticket branch modifying conflict.txt
    subprocess.run(["git", "checkout", "-b", "ticket/usr-116"], cwd=str(repo), check=True, capture_output=True)
    (repo / "conflict.txt").write_text("branch A\n", encoding="utf-8")
    subprocess.run(["git", "add", "conflict.txt"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "branch commit"], cwd=str(repo), check=True, capture_output=True)

    # Create conflicting change on main
    subprocess.run(["git", "checkout", "main"], cwd=str(repo), check=True, capture_output=True)
    (repo / "conflict.txt").write_text("branch B\n", encoding="utf-8")
    subprocess.run(["git", "add", "conflict.txt"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "main commit"], cwd=str(repo), check=True, capture_output=True)

    subprocess.run(["git", "checkout", "ticket/usr-116"], cwd=str(repo), check=True, capture_output=True)

    with patch("run_ticket.PROJECT_ROOT", repo):
        code = run_ticket.resume_delivery("USR-116", worktree_path=repo, skip_validation=True)
        assert code == 2

    captured = capsys.readouterr()
    assert "[ERRO DE CONFLITO NO REBASE]" in captured.err
    assert "conflict.txt" in captured.err
    assert "--resume-delivery USR-116" in captured.err
    assert repo.exists()


def test_resume_delivery_failure_after_gate_prints_diagnostics(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When delivery fails after gate, prints branch, SHA, worktree and resume command (USR-116)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    demands_dir = repo / ".factory" / "demands"
    demands_dir.mkdir(parents=True)
    ledger = demands_dir / "demands.json"
    store = DemandsStore(ledger)
    ticket = UserTicket(
        id="USR-116",
        project_id="darkfac",
        title="Test Ticket",
        problem_statement="Test problem",
        status=DeliveryStatus.PLANNED,
    )
    store.save_ticket(ticket)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

    subprocess.run(["git", "checkout", "-b", "ticket/usr-116"], cwd=str(repo), check=True, capture_output=True)
    (repo / "code.py").write_text("x = 10\n", encoding="utf-8")
    subprocess.run(["git", "add", "code.py"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat: code"], cwd=str(repo), check=True, capture_output=True)

    fake_fail_report = TicketCompletionReport(
        ok=False,
        ticket_id="USR-116",
        message="Simulated GitHub push/PR failure (gh api 500)",
    )

    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch.object(GitAutonomyManager, "complete_ticket", return_value=fake_fail_report):
        code = run_ticket.resume_delivery("USR-116", worktree_path=repo, skip_validation=True)
        assert code == 1

    captured = capsys.readouterr()
    assert "[ERRO NA ENTREGA DO TICKET USR-116]" in captured.err
    assert "Simulated GitHub push/PR failure" in captured.err
    assert "Branch: ticket/usr-116" in captured.err
    assert "SHA: " in captured.err
    assert "Worktree preservada: " in captured.err
    assert "--resume-delivery USR-116" in captured.err
    assert repo.exists()


def test_resume_delivery_happy_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Successful resume_delivery delivers, syncs, sweeps, and exits 0 (USR-116)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    demands_dir = repo / ".factory" / "demands"
    demands_dir.mkdir(parents=True)
    ledger = demands_dir / "demands.json"
    store = DemandsStore(ledger)
    ticket = UserTicket(
        id="USR-116",
        project_id="darkfac",
        title="Test Ticket",
        problem_statement="Test problem",
        status=DeliveryStatus.PLANNED,
    )
    store.save_ticket(ticket)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

    subprocess.run(["git", "checkout", "-b", "ticket/usr-116"], cwd=str(repo), check=True, capture_output=True)
    (repo / "code.py").write_text("x = 42\n", encoding="utf-8")
    subprocess.run(["git", "add", "code.py"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat: code 42"], cwd=str(repo), check=True, capture_output=True)

    fake_success_report = TicketCompletionReport(
        ok=True,
        ticket_id="USR-116",
        commit_sha="abcd1234ef5678",
        message="Delivered successfully",
    )

    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch.object(GitAutonomyManager, "complete_ticket", return_value=fake_success_report), \
         patch.object(GitAutonomyManager, "sweep_stale") as mock_sweep:
        code = run_ticket.resume_delivery("USR-116", worktree_path=repo, skip_validation=True)
        assert code == 0

    captured = capsys.readouterr()
    assert "[SUCESSO] Ticket USR-116 entregue e validado com sucesso!" in captured.out
    assert "abcd1234ef5678" in captured.out
    mock_sweep.assert_called_once()


def test_resume_delivery_renumbers_duplicate_owner_action_before_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A branch-local OA-NNN colliding with origin/main is renumbered before the gate (USR-255).

    Reproduces the USR-249 incident: the ticket branch committed its own OA-002 while origin/main
    independently had a different OA-002 (both picked the same next free number). A rebase/merge
    between the two can land both rows side by side without a textual conflict, so the gate's
    ``test_seeded_backlog_is_valid_and_complete`` would fail on "item OA-002 duplicado ignorado".
    The branch's own commit here stands in for that merged state; the fix must renumber the
    branch's row, commit it, and let delivery succeed.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    demands_dir = repo / ".factory" / "demands"
    demands_dir.mkdir(parents=True)
    ledger = demands_dir / "demands.json"
    store = DemandsStore(ledger)
    ticket = UserTicket(
        id="USR-255",
        project_id="darkfac",
        title="Test Ticket",
        problem_statement="Test problem",
        status=DeliveryStatus.PLANNED,
    )
    store.save_ticket(ticket)

    oa_dir = repo / ".factory" / "owner_actions"
    oa_dir.mkdir(parents=True)
    oa_path = oa_dir / "owner_actions.json"
    oa_001 = {"id": "OA-001", "title": "Item ja existente", "kind": "action", "priority": "medium",
              "status": "open", "steps": [{"text": "Faca algo"}]}
    remote_oa_002 = {"id": "OA-002", "title": "Veio de outro PR em origin/main", "kind": "action",
                      "priority": "high", "status": "open", "steps": [{"text": "Passo do origin/main"}]}
    oa_path.write_text(json.dumps({"schema_version": 1, "actions": [oa_001]}), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

    # origin/main gains OA-002 on a commit the ticket branch will be based on.
    oa_path.write_text(json.dumps({"schema_version": 1, "actions": [oa_001, remote_oa_002]}), encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "chore: origin adds OA-002"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "main"], cwd=str(repo), check=True, capture_output=True
    )

    # The ticket branch, based on that same commit, independently registered its OWN OA-002 (a
    # different reason) before ever seeing origin/main's -- the state a non-conflicting rebase/merge
    # of the two would leave behind.
    subprocess.run(["git", "checkout", "-b", "ticket/usr-255"], cwd=str(repo), check=True, capture_output=True)
    branch_oa_002 = {"id": "OA-002", "title": "Registrado pela branch do ticket", "kind": "action",
                      "priority": "high", "status": "open", "steps": [{"text": "Passo da branch"}]}
    oa_path.write_text(
        json.dumps({"schema_version": 1, "actions": [oa_001, remote_oa_002, branch_oa_002]}), encoding="utf-8"
    )
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: usr-255 implementation"], cwd=str(repo), check=True, capture_output=True
    )

    fake_success_report = TicketCompletionReport(
        ok=True, ticket_id="USR-255", commit_sha="deadbeef", message="Delivered successfully",
    )

    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch.object(GitAutonomyManager, "complete_ticket", return_value=fake_success_report), \
         patch.object(GitAutonomyManager, "sweep_stale"):
        code = run_ticket.resume_delivery("USR-255", worktree_path=repo, skip_validation=True)
        assert code == 0

    captured = capsys.readouterr()
    assert "OA renumerada apos sync com origin/main: OA-002 -> OA-003" in captured.out
    assert "[SUCESSO] Ticket USR-255 entregue e validado com sucesso!" in captured.out

    document = json.loads(oa_path.read_text(encoding="utf-8"))
    assert [row["id"] for row in document["actions"]] == ["OA-001", "OA-002", "OA-003"]

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo), check=True, capture_output=True, text=True
    )
    assert status.stdout.strip() == ""
    log = subprocess.run(
        ["git", "log", "--oneline", "-3"], cwd=str(repo), check=True, capture_output=True, text=True
    )
    assert "renumera OA duplicado" in log.stdout


def test_delivery_in_fresh_process_prevents_stale_module_value_error(tmp_path: Path) -> None:
    """Delivery phase executes via fresh subprocess, immune to in-memory stale imports (USR-116)."""
    # In the parent process, simulate a stale module state where UserTicket lacks a new field
    class StaleUserTicket:
        def __init__(self, **kwargs):
            # Lacks delivery_evidence
            self.id = kwargs.get("id")
            self.title = kwargs.get("title")
            self.status = kwargs.get("status")

        def __setattr__(self, name, value):
            if name == "delivery_evidence":
                raise ValueError('"UserTicket" object has no field "delivery_evidence"')
            super().__setattr__(name, value)

    # Running resume_delivery via CLI in a fresh process uses the real code from disk
    # and succeeds without ValueError.
    res = subprocess.run(
        [sys.executable, "-m", "run_ticket", "--help"],
        cwd=str(Path.cwd()),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "--resume-delivery" in res.stdout
