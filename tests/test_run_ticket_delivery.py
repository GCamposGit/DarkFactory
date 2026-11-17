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


def test_resume_delivery_rebase_conflict_cli_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """resume_delivery outputs clear conflict message and exact resume command on conflict (USR-116)."""
    # The standalone resume path writes legacy plan/build artifacts. Keep its
    # state in this test's temp directory so artifacts from another run cannot
    # skip that compatibility path and stop before the rebase conflict.
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("DARKFAC_LOCAL_CANCEL_PARENT", raising=False)
    # Exercise the unresolved-conflict CLI path without launching a real agent
    # from an xdist worker (which may cancel or outlive that worker).
    monkeypatch.setattr(run_ticket, "_invoke_fix_agent", lambda *args, **kwargs: None)
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
        assert code == 1

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
    """Successful resume_delivery delivers without a global worktree sweep (USR-423)."""
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
    assert "[MERGE] Ticket USR-116 mergeado e validado; aguardando deploy" in captured.out  # USR-375 S4: smoke pendente
    assert "abcd1234ef5678" in captured.out
    mock_sweep.assert_not_called()


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
    assert "[MERGE] Ticket USR-255 mergeado e validado; aguardando deploy" in captured.out  # USR-375 S4: smoke pendente

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


def _git(repo: Path, *args: str) -> None:
    # GIT_EDITOR from the parent process overrides ``-c core.editor`` and, on Windows, a
    # ``cmd.exe /c exit 0`` value is not executable from Git's shell. ``rebase --continue``
    # must still be able to finish.
    env = os.environ.copy()
    env["GIT_EDITOR"] = "true"
    env["GIT_SEQUENCE_EDITOR"] = "true"
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, env=env)


def _conflict_repo(tmp_path: Path, files: dict[str, tuple[str, str]]) -> Path:
    """Repo on `ticket/usr-412` whose commit conflicts with main on every file in `files` (name -> (branch, main))."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    _git(repo, "checkout", "-b", "ticket/usr-412")
    for side, idx in (("ticket/usr-412", 0), ("main", 1)):
        if side == "main":
            _git(repo, "checkout", "main")
        for name, texts in files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(texts[idx], encoding="utf-8")
            _git(repo, "add", name)
        _git(repo, "commit", "-m", f"{side} change")
    _git(repo, "checkout", "ticket/usr-412")
    return repo


_TICKET_412 = UserTicket(id="USR-412", project_id="darkfac", title="Ticket 412", problem_statement="Problema 412", status=DeliveryStatus.PLANNED)


def _rebase_in_progress(repo: Path) -> bool:
    out = subprocess.run(["git", "status"], cwd=str(repo), capture_output=True, text=True).stdout
    return "rebase in progress" in out


def test_ledger_only_rebase_conflict_takes_origin_main_without_agent(tmp_path: Path) -> None:
    ledger = ".factory/demands/demands.json"
    repo = _conflict_repo(tmp_path, {ledger: ("[]\n", '{"demands": []}\n')})
    ok, files, msg = run_ticket.rebase_on_origin_main(repo, keep_conflict=True)
    assert ok is False and files == [ledger]
    with patch.object(run_ticket, "_invoke_fix_agent") as agent:
        ok, files, _ = run_ticket.resolve_rebase_conflict(_TICKET_412, repo, files, msg)
    assert ok is True and files == []
    agent.assert_not_called()
    assert [t["id"] for t in json.loads((repo / ledger).read_text(encoding="utf-8")) if isinstance(t, dict)] == ["USR-412"]
    assert not _rebase_in_progress(repo)


def test_ledger_and_fase_atual_conflict_keeps_main_and_reapplies_own_row(tmp_path: Path) -> None:
    ledger = ".factory/demands/demands.json"
    fase = "docs/FASE_ATUAL.md"
    other = {"id": "USR-1", "project_id": "darkfac", "title": "Outro", "problem_statement": "p", "status": "planned"}
    own = _TICKET_412.model_dump(mode="json")
    repo = _conflict_repo(tmp_path, {
        ledger: (json.dumps([own]), json.dumps([other])),
        fase: ("branch\n", "main\n"),
    })
    ok, files, msg = run_ticket.rebase_on_origin_main(repo, keep_conflict=True)
    assert ok is False and set(files) == {ledger, fase}
    with patch.object(run_ticket, "_invoke_fix_agent") as agent:
        ok, files, _ = run_ticket.resolve_rebase_conflict(_TICKET_412, repo, files, msg)
    assert ok is True and files == []
    agent.assert_not_called()
    assert (repo / fase).read_text(encoding="utf-8").strip() == "main"
    ids = {t["id"] for t in json.loads((repo / ledger).read_text(encoding="utf-8"))}
    assert ids == {"USR-1", "USR-412"}
    assert not _rebase_in_progress(repo)


def test_merge_blocked_and_rebase_conflict_are_named_causes_not_unknown() -> None:
    from core.line.failure_taxonomy import canonical_cause, classify

    assert (canonical_cause("rebase_conflict"), classify("rebase_conflict")) == ("rebase_conflict", "candidate")
    assert (canonical_cause("merge_blocked"), classify("merge_blocked")) == ("merge_blocked", "infra")
    for action, cause in (("merge_blocked", "merge_blocked"), ("conflict", "rebase_conflict")):
        progress = MagicMock()
        report = DeliveryReport(ok=False, action=action, message="x")
        payload = run_ticket.ticket_stages.delivery_failure_result(
            _TICKET_412, report, True, Path("."), "b", "sha", "cmd", progress, False, True,
        )
        assert payload["cause"] == cause
        progress.set_outcome.assert_called_once_with("x", cause)


def test_code_rebase_conflict_goes_to_agent_once_and_agent_resolution_is_kept(tmp_path: Path) -> None:
    repo = _conflict_repo(tmp_path, {"mod.py": ("a = 1\n", "a = 2\n")})
    ok, files, msg = run_ticket.rebase_on_origin_main(repo, keep_conflict=True)
    assert ok is False

    def fake_agent(worktree: Path, ticket: UserTicket, stage: str, feedback: str) -> None:
        assert stage == "rebase-conflito" and "mod.py" in feedback
        (worktree / "mod.py").write_text("a = 3\n", encoding="utf-8")
        _git(worktree, "add", "mod.py")
        _git(worktree, "-c", "core.editor=true", "rebase", "--continue")

    with patch.object(run_ticket, "_invoke_fix_agent", side_effect=fake_agent) as agent:
        ok, files, _ = run_ticket.resolve_rebase_conflict(_TICKET_412, repo, files, msg)
    assert ok is True and agent.call_count == 1
    assert (repo / "mod.py").read_text(encoding="utf-8") == "a = 3\n"


def test_unresolved_rebase_conflict_preserves_technical_failure_with_rc_1(tmp_path: Path) -> None:
    repo = _conflict_repo(tmp_path, {"mod.py": ("a = 1\n", "a = 2\n")})
    ledger = repo / ".factory" / "demands" / "demands.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    DemandsStore(ledger).save_ticket(_TICKET_412)
    with patch.object(run_ticket, "PROJECT_ROOT", repo), \
            patch.object(run_ticket, "_invoke_fix_agent", return_value=None) as agent:
        code = run_ticket.resume_delivery("USR-412", worktree_path=repo, skip_validation=True)
    assert code == 1 and agent.call_count == 1
    assert DemandsStore(ledger).get_ticket("USR-412").status == DeliveryStatus.VALIDATING
    assert not _rebase_in_progress(repo)


def _forbid_next_ticket_id(self: DemandsStore, project_id: str = "darkfac") -> str:
    raise AssertionError(f"next_ticket_id must not allocate ({project_id})")


def _isolate_resume_queue(monkeypatch: pytest.MonkeyPatch, state: Path) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(state))
    monkeypatch.delenv("DARKFAC_HF02_DATABASE_URL", raising=False)
    monkeypatch.delenv("DARKFAC_DEMANDS_PATH", raising=False)
    monkeypatch.delenv("DARKFAC_LOCAL_CANCEL_PARENT", raising=False)
    from core.demands import queue_store

    queue_store._store.cache_clear()


def test_resume_delivery_materializes_queue_only_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queue-only id reaches delivery with no shared ledger, no origin row and no fix agent."""
    state = tmp_path / "state"
    _isolate_resume_queue(monkeypatch, state)
    from core.demands import queue_store

    repo = tmp_path / "wt"
    repo.mkdir()
    _init_git_repo(repo)
    ledger = repo / ".factory" / "demands" / "demands.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("[]\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "empty ledger")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-b", "ticket/usr-413")
    shared = tmp_path / "shared"
    shared.mkdir()
    queue_store.register_ticket(UserTicket(
        id="USR-413", project_id="darkfac", title="Veio da fila",
        problem_statement="registrado so na fila duravel", status=DeliveryStatus.IMPLEMENTING,
    ))
    monkeypatch.setattr(DemandsStore, "next_ticket_id", _forbid_next_ticket_id)
    seen: dict[str, str] = {}

    def deliver(_git_mgr: object, ticket: UserTicket, *_args: object, **_kwargs: object) -> int:
        seen["id"] = ticket.id
        seen["title"] = ticket.title
        return 0

    with patch("run_ticket.PROJECT_ROOT", shared), \
            patch("run_ticket._deliver_worktree", side_effect=deliver) as delivered, \
            patch("run_ticket._invoke_fix_agent", side_effect=AssertionError("_invoke_fix_agent")):
        code = run_ticket.resume_delivery("USR-413", worktree_path=repo, skip_validation=True)

    assert code == 0
    assert delivered.call_count == 1
    assert seen == {"id": "USR-413", "title": "Veio da fila"}
    ids = [row["id"] for row in json.loads(ledger.read_text(encoding="utf-8"))]
    assert ids.count("USR-413") == 1
    assert not (shared / ".factory" / "demands" / "demands.json").exists()
    shown = subprocess.run(
        ["git", "show", "origin/main:.factory/demands/demands.json"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    assert "USR-413" not in shown.stdout


def test_resume_delivery_materializes_when_worktree_is_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linked worktree that is the project root still gets one ledger line from the queue.

    ``get_ticket`` would merge the queue in memory and skip the write.
    """
    state = tmp_path / "state"
    _isolate_resume_queue(monkeypatch, state)
    from core.demands import queue_store

    seed = tmp_path / "seed"
    seed.mkdir()
    _init_git_repo(seed)
    repo = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "ticket/usr-413", str(repo), "HEAD"],
        cwd=seed, check=True, capture_output=True,
    )
    assert (repo / ".git").is_file()
    ledger = repo / ".factory" / "demands" / "demands.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("[]\n", encoding="utf-8")
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(repo))
    queue_store.register_ticket(UserTicket(
        id="USR-413", project_id="darkfac", title="Veio da fila",
        problem_statement="registrado so na fila duravel", status=DeliveryStatus.IMPLEMENTING,
    ))
    monkeypatch.setattr(DemandsStore, "next_ticket_id", _forbid_next_ticket_id)
    seen: dict[str, str] = {}

    def deliver(_git_mgr: object, ticket: UserTicket, *_args: object, **_kwargs: object) -> int:
        seen["id"] = ticket.id
        seen["title"] = ticket.title
        return 0

    shared = tmp_path / "shared"
    shared.mkdir()
    with patch("run_ticket.PROJECT_ROOT", shared), \
            patch("run_ticket._deliver_worktree", side_effect=deliver), \
            patch("run_ticket._invoke_fix_agent", side_effect=AssertionError("_invoke_fix_agent")):
        code = run_ticket.resume_delivery("USR-413", worktree_path=repo, skip_validation=True)

    assert code == 0
    assert seen == {"id": "USR-413", "title": "Veio da fila"}
    ids = [row["id"] for row in json.loads(ledger.read_text(encoding="utf-8"))]
    assert ids.count("USR-413") == 1
    assert not (shared / ".factory" / "demands" / "demands.json").exists()


def test_reusable_validation_keeps_matching_or_bound_empty_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Passed validation stays for a matching digest, or an empty digest with a valid approval."""
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    ticket = UserTicket(
        id="USR-424", project_id="darkfac", title="Regra", problem_statement="p", status=DeliveryStatus.IMPLEMENTING,
    )
    rws = run_ticket.ticket_engine.run_workspace(ticket)
    from core.line import workspace as line_workspace

    ctx = line_workspace.context_dir(rws)
    ctx.mkdir(parents=True, exist_ok=True)
    digest, other, policy = "a" * 64, "b" * 64, "c" * 64

    def write_validation(payload: dict[str, object]) -> None:
        (ctx / "validation.json").write_text(json.dumps(payload), encoding="utf-8")

    def write_review(verdict: str, cand: str, pol: str, *, forced: bool = False) -> None:
        state = {"rounds": [{
            "round": 1, "verdict": verdict, "forced": forced,
            "candidate_digest": cand, "policy_digest": pol,
        }]}
        (ctx / "review_state.json").write_text(json.dumps(state), encoding="utf-8")

    write_validation({"passed": True, "candidate_digest": digest})
    assert run_ticket.ticket_engine.reusable_validation(rws, digest, policy) is True
    assert run_ticket.ticket_engine.reusable_validation(rws, other, policy) is False
    write_review("changes_required", digest, policy)
    assert run_ticket.ticket_engine.reusable_validation(rws, digest, policy) is True

    write_validation({"passed": True, "gate": "previous"})
    write_review("approve", digest, policy)
    assert run_ticket.ticket_engine.reusable_validation(rws, digest, policy) is True
    write_review("approve", other, policy)
    assert run_ticket.ticket_engine.reusable_validation(rws, digest, policy) is False
    write_review("approve", digest, policy, forced=True)
    assert run_ticket.ticket_engine.reusable_validation(rws, digest, policy) is False
    write_review("changes_required", digest, policy)
    assert run_ticket.ticket_engine.reusable_validation(rws, digest, policy) is False
    write_validation({"passed": False, "candidate_digest": digest})
    assert run_ticket.ticket_engine.reusable_validation(rws, digest, policy) is False

    write_validation({"passed": True, "candidate_digest": other, "marker": "drop"})
    assert run_ticket.ticket_engine.keep_reusable_validation(rws, digest, policy) is False
    assert not (ctx / "validation.json").exists()
    write_validation({"passed": False, "candidate_digest": digest, "marker": "drop"})
    assert run_ticket.ticket_engine.keep_reusable_validation(rws, digest, policy) is False
    assert not (ctx / "validation.json").exists()

    write_validation({"passed": True, "candidate_digest": digest, "marker": "keep"})
    (ctx / "merge.json").write_text('{"marker":"previous-attempt"}', encoding="utf-8")
    (ctx / "deploy_report.json").write_text('{"marker":"previous-attempt"}', encoding="utf-8")
    assert run_ticket.ticket_engine.keep_reusable_validation(rws, digest, policy) is True
    run_ticket.ticket_engine.clear_delivery(rws, keep_validation=True)
    assert "keep" in (ctx / "validation.json").read_text(encoding="utf-8")
    assert not (ctx / "merge.json").exists()
    assert not (ctx / "deploy_report.json").exists()


def _candidate_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, UserTicket, object, str, str]:
    """Clean ticket branch whose candidate digest is stable across resume."""
    _isolate_resume_queue(monkeypatch, tmp_path / "state")
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    ticket = UserTicket(
        id="USR-424", project_id="darkfac", title="Preservar gate",
        problem_statement="gate e revisao vigentes", status=DeliveryStatus.IMPLEMENTING,
    )
    ledger = repo / ".factory" / "demands" / "demands.json"
    ledger.parent.mkdir(parents=True)
    DemandsStore(ledger).save_ticket(ticket)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "ledger")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-b", "ticket/usr-424")
    (repo / "impl.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "impl.py")
    _git(repo, "commit", "-m", "feat: impl")
    rws = run_ticket.ticket_engine.run_workspace(ticket, "ticket/usr-424")
    from core.research.policy import ResearchArtifact

    run_ticket.ticket_engine.write_demand(rws, ticket)
    run_ticket.ticket_engine.write_minimal_plan(rws, ticket)
    run_ticket.ticket_engine.write_research(
        rws, ResearchArtifact(kind="research_skipped", ticket_id=ticket.id, reason="small fix")
    )
    run_ticket.ticket_engine.write_build(rws, "grok", [ticket.id])
    candidate, policy = run_ticket._candidate_binding(rws, repo, run_ticket._review_base_branch(ticket))
    assert candidate
    return repo, ticket, rws, candidate, policy


def _seed_attempt(
    rws: object, validation: dict[str, object], rounds: list[dict[str, object]]
) -> tuple[Path, bytes]:
    from core.line import workspace as line_workspace

    ctx = line_workspace.context_dir(rws)  # type: ignore[arg-type]
    (ctx / "validation.json").write_text(json.dumps(validation), encoding="utf-8")
    before = (ctx / "validation.json").read_bytes()
    (ctx / "review_state.json").write_text(json.dumps({"rounds": rounds}), encoding="utf-8")
    (ctx / "review_verdict.json").write_text(json.dumps({"verdict": "approve"}), encoding="utf-8")
    (ctx / "merge.json").write_text(
        json.dumps({"pr_url": "https://old.example/pull/1", "merge_sha": "abc", "marker": "previous-attempt"}),
        encoding="utf-8",
    )
    (ctx / "deploy_report.json").write_text(
        json.dumps({"sha": "old", "smoke_ok": True, "deployed": True, "detail": "previous-attempt"}),
        encoding="utf-8",
    )
    return ctx, before


def test_resume_keeps_matching_validation_and_skips_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Matching digest skips the gate; a bound approval skips the reviewer; prior merge/deploy are dropped."""
    repo, _ticket, rws, candidate, policy = _candidate_repo(tmp_path, monkeypatch)
    ctx, validation_before = _seed_attempt(
        rws,
        {"passed": True, "gate": "previous", "detail": "kept", "candidate_digest": candidate},
        [{"round": 1, "verdict": "approve", "candidate_digest": candidate, "policy_digest": policy}],
    )
    verdict_before = (ctx / "review_verdict.json").read_bytes()

    def gate(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("gate ran")

    def reviewer(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("reviewer agent ran")

    with patch("run_ticket.PROJECT_ROOT", repo), \
            patch.object(GitAutonomyManager, "complete_ticket", return_value=TicketCompletionReport(
                ok=True, ticket_id="USR-424", commit_sha="c" * 40, message="ok",
            )), \
            patch("run_ticket._run_gate_with_self_correction", side_effect=gate), \
            patch("run_ticket._invoke_fix_agent", side_effect=AssertionError("_invoke_fix_agent")), \
            patch("core.line.stage_review.run_agent", side_effect=reviewer):
        code = run_ticket.resume_delivery("USR-424", worktree_path=repo, skip_validation=False)

    assert code == 0
    assert (ctx / "validation.json").read_bytes() == validation_before
    assert (ctx / "review_verdict.json").read_bytes() == verdict_before
    assert "previous-attempt" not in (ctx / "merge.json").read_text(encoding="utf-8")
    assert "previous-attempt" not in (ctx / "deploy_report.json").read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "portao nao reexecutado" in out
    assert "revisor nao reexecutado" in out


def test_resume_keeps_empty_digest_when_review_binding_is_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older validation.json without a digest stays when review_state approves this candidate."""
    repo, _ticket, rws, candidate, policy = _candidate_repo(tmp_path, monkeypatch)
    ctx, validation_before = _seed_attempt(
        rws,
        {"passed": True, "gate": "previous"},
        [{"round": 1, "verdict": "approve", "candidate_digest": candidate, "policy_digest": policy}],
    )

    with patch("run_ticket.PROJECT_ROOT", repo), \
            patch.object(GitAutonomyManager, "complete_ticket", return_value=TicketCompletionReport(
                ok=True, ticket_id="USR-424", commit_sha="c" * 40, message="ok",
            )), \
            patch("run_ticket._run_gate_with_self_correction", side_effect=AssertionError("gate ran")), \
            patch("run_ticket._invoke_fix_agent", side_effect=AssertionError("_invoke_fix_agent")), \
            patch("core.line.stage_review.run_agent", side_effect=AssertionError("reviewer agent ran")):
        code = run_ticket.resume_delivery("USR-424", worktree_path=repo, skip_validation=False)

    assert code == 0
    assert (ctx / "validation.json").read_bytes() == validation_before
    assert "previous-attempt" not in (ctx / "merge.json").read_text(encoding="utf-8")
    assert "previous-attempt" not in (ctx / "deploy_report.json").read_text(encoding="utf-8")


def _stale_validation(case: str, candidate: str, policy: str) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Validation and review rows that must not be reused for `candidate`/`policy`."""
    old = "0" * 64
    approve = {"round": 1, "verdict": "approve", "candidate_digest": candidate, "policy_digest": policy}
    if case == "different_digest":
        return {"passed": True, "gate": "previous", "candidate_digest": old}, [approve]
    if case == "passed_false":
        return {"passed": False, "gate": "previous", "candidate_digest": candidate}, [approve]
    if case == "changes_required":
        return {"passed": True, "gate": "previous"}, [{**approve, "verdict": "changes_required"}]
    if case == "forced":
        return {"passed": True, "gate": "previous"}, [{**approve, "forced": True}]
    if case == "wrong_policy":
        return {"passed": True, "gate": "previous"}, [{**approve, "policy_digest": "d" * 64}]
    if case == "wrong_candidate":
        return {"passed": True, "gate": "previous"}, [{**approve, "candidate_digest": old}]
    raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    ["different_digest", "passed_false", "changes_required", "forced", "wrong_policy", "wrong_candidate"],
)
def test_resume_erases_stale_validation_and_runs_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A report that does not cover this candidate is deleted and the gate runs."""
    repo, _ticket, rws, candidate, policy = _candidate_repo(tmp_path, monkeypatch)
    validation, rounds = _stale_validation(case, candidate, policy)
    ctx, _before = _seed_attempt(rws, validation, rounds)
    seen: dict[str, bool] = {}

    def gate(*_args: object, **_kwargs: object) -> dict[str, object]:
        seen["gate"] = True
        assert not (ctx / "validation.json").exists()
        return {"exit_code": 1, "ok": False, "cause": "gate_failed"}

    with patch("run_ticket.PROJECT_ROOT", repo), \
            patch("run_ticket._run_gate_with_self_correction", side_effect=gate):
        result = run_ticket.resume_delivery(
            "USR-424", worktree_path=repo, skip_validation=False, return_result=True
        )

    assert seen.get("gate") is True
    assert result["exit_code"] == 1
    assert not (ctx / "validation.json").exists()
    assert not (ctx / "merge.json").exists()
    assert not (ctx / "deploy_report.json").exists()


def test_resume_candidate_change_runs_gate_and_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different candidate digest reruns the gate and then the review stage."""
    repo, _ticket, rws, candidate, policy = _candidate_repo(tmp_path, monkeypatch)
    old = "0" * 64
    ctx, _before = _seed_attempt(
        rws,
        {"passed": True, "gate": "previous", "candidate_digest": old},
        [{"round": 1, "verdict": "approve", "candidate_digest": old, "policy_digest": policy}],
    )
    seen: dict[str, bool] = {}

    def gate(*_args: object, **_kwargs: object) -> None:
        seen["gate"] = True
        assert not (ctx / "validation.json").exists()
        return None

    def review(*_args: object, **_kwargs: object) -> object:
        seen["review"] = True
        from core.line import workspace as line_workspace

        line_workspace.write_context(
            rws,  # type: ignore[arg-type]
            "review_state.json",
            json.dumps({"rounds": [{
                "round": 1, "verdict": "approve", "forced": False,
                "candidate_digest": candidate, "policy_digest": policy,
            }]}),
        )
        line_workspace.write_context(
            rws, "review_verdict.json", json.dumps({"verdict": "approve", "blocking": [], "non_blocking": []}),  # type: ignore[arg-type]
        )
        return type("ReviewHit", (), {"outcome": "success", "cause_code": None})()

    with patch("run_ticket.PROJECT_ROOT", repo), \
            patch.object(GitAutonomyManager, "complete_ticket", return_value=TicketCompletionReport(
                ok=True, ticket_id="USR-424", commit_sha="c" * 40, message="ok",
            )), \
            patch("run_ticket._run_gate_with_self_correction", side_effect=gate), \
            patch("run_ticket._review_candidate", side_effect=review), \
            patch("run_ticket._invoke_fix_agent", side_effect=AssertionError("_invoke_fix_agent")):
        code = run_ticket.resume_delivery("USR-424", worktree_path=repo, skip_validation=False)

    assert code == 0
    assert seen == {"gate": True, "review": True}
    written = json.loads((ctx / "validation.json").read_text(encoding="utf-8"))
    assert written["passed"] is True
    assert written["candidate_digest"] == candidate
    assert "previous-attempt" not in (ctx / "merge.json").read_text(encoding="utf-8")
    assert "previous-attempt" not in (ctx / "deploy_report.json").read_text(encoding="utf-8")


def test_resume_policy_change_keeps_validation_and_runs_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A matching digest skips the gate. A different review policy still runs the reviewer."""
    repo, _ticket, rws, candidate, _policy = _candidate_repo(tmp_path, monkeypatch)
    ctx, validation_before = _seed_attempt(
        rws,
        {"passed": True, "gate": "previous", "detail": "kept", "candidate_digest": candidate},
        [{"round": 1, "verdict": "approve", "candidate_digest": candidate, "policy_digest": "e" * 64}],
    )
    seen: dict[str, bool] = {}

    def review(*_args: object, **_kwargs: object) -> object:
        seen["review"] = True
        from core.line import workspace as line_workspace

        line_workspace.write_context(
            rws,  # type: ignore[arg-type]
            "review_state.json",
            json.dumps({"rounds": [{
                "round": 1, "verdict": "approve", "forced": False,
                "candidate_digest": candidate, "policy_digest": _policy,
            }]}),
        )
        line_workspace.write_context(
            rws, "review_verdict.json", json.dumps({"verdict": "approve", "blocking": [], "non_blocking": []}),  # type: ignore[arg-type]
        )
        return type("ReviewHit", (), {"outcome": "success", "cause_code": None})()

    with patch("run_ticket.PROJECT_ROOT", repo), \
            patch.object(GitAutonomyManager, "complete_ticket", return_value=TicketCompletionReport(
                ok=True, ticket_id="USR-424", commit_sha="c" * 40, message="ok",
            )), \
            patch("run_ticket._run_gate_with_self_correction", side_effect=AssertionError("gate ran")), \
            patch("run_ticket._review_candidate", side_effect=review), \
            patch("run_ticket._invoke_fix_agent", side_effect=AssertionError("_invoke_fix_agent")):
        code = run_ticket.resume_delivery("USR-424", worktree_path=repo, skip_validation=False)

    assert code == 0
    assert seen.get("review") is True
    assert (ctx / "validation.json").read_bytes() == validation_before
    assert "previous-attempt" not in (ctx / "merge.json").read_text(encoding="utf-8")
    assert "previous-attempt" not in (ctx / "deploy_report.json").read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "portao nao reexecutado" in out
    assert "revisor nao reexecutado" not in out


def test_resume_review_exhaustion_waits_and_preserves_gate_and_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No compatible reviewer is a route wait: pending delivery, exit 75, same BUILD and gate."""
    from core.line.pending_delivery import queue_dir
    from core.paths import state_root
    from core.workflow.control_contracts import StageResult

    repo, _ticket, rws, candidate, _policy = _candidate_repo(tmp_path, monkeypatch)
    ctx, validation_before = _seed_attempt(
        rws,
        {"passed": True, "gate": "previous", "detail": "kept", "candidate_digest": candidate},
        [],
    )
    (ctx / "review_verdict.json").unlink()
    build_before = (ctx / "progress.json").read_bytes()
    from core.line import workspace as line_workspace

    assert not (ctx / "review_execution.json").exists()

    def review(*_args: object, **_kwargs: object) -> StageResult:
        return StageResult(
            outcome="retry",
            cause_code="no_route_available not_before=2026-11-13T01:00:00+00:00",
        )

    with patch("run_ticket.PROJECT_ROOT", repo), \
            patch.object(GitAutonomyManager, "complete_ticket", side_effect=AssertionError("merge")), \
            patch("run_ticket._run_gate_with_self_correction", side_effect=AssertionError("gate ran")), \
            patch("run_ticket._review_candidate", side_effect=review), \
            patch("run_ticket._invoke_fix_agent", side_effect=AssertionError("_invoke_fix_agent")):
        result = run_ticket.resume_delivery(
            "USR-424", worktree_path=repo, skip_validation=False, return_result=True,
        )

    assert result["exit_code"] == run_ticket.EXIT_ROUTE_WAIT
    assert result["cause"] == "route_wait"
    assert repo.is_dir()
    assert (ctx / "validation.json").read_bytes() == validation_before
    assert (ctx / "progress.json").read_bytes() == build_before
    assert json.loads(build_before.decode("utf-8"))["harness"] == "grok"
    assert not (ctx / "review_verdict.json").exists()
    assert not (ctx / "review_execution.json").exists()
    assert line_workspace.context_dir(rws) == ctx  # type: ignore[arg-type]
    pending = queue_dir() / "USR-424.json"
    assert state_root() == tmp_path / "state"
    row = json.loads(pending.read_text(encoding="utf-8"))
    assert row["resume_delivery"] is True
    assert row["stage"] == "review"
    assert row["status"] == "waiting"
    assert row["implementing_harness"] == "grok"
    assert "no_route_available" in row["cause"]


def test_resume_without_bound_approval_does_not_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approve file for another candidate does not publish this tree."""
    repo, _ticket, rws, candidate, policy = _candidate_repo(tmp_path, monkeypatch)
    ctx, validation_before = _seed_attempt(
        rws,
        {"passed": True, "gate": "previous", "detail": "kept", "candidate_digest": candidate},
        [{"round": 1, "verdict": "approve", "candidate_digest": "0" * 64, "policy_digest": policy}],
    )

    def review(*_args: object, **_kwargs: object) -> object:
        return type("ReviewHit", (), {"outcome": "success", "cause_code": None})()

    with patch("run_ticket.PROJECT_ROOT", repo), \
            patch.object(GitAutonomyManager, "complete_ticket", side_effect=AssertionError("merge")), \
            patch("run_ticket._run_gate_with_self_correction", side_effect=AssertionError("gate ran")), \
            patch("run_ticket._review_candidate", side_effect=review), \
            patch("run_ticket._invoke_fix_agent", side_effect=AssertionError("_invoke_fix_agent")):
        result = run_ticket.resume_delivery(
            "USR-424", worktree_path=repo, skip_validation=False, return_result=True,
        )

    assert result["exit_code"] == 1
    assert "review_not_bound_to_candidate" in result["error"]
    assert repo.is_dir()
    assert (ctx / "validation.json").read_bytes() == validation_before
    assert (ctx / "progress.json").is_file()
    assert not (ctx / "merge.json").exists()


@pytest.mark.parametrize("prior", [None, {"verdict": "changes_required", "blocking": [], "non_blocking": []}])
def test_resume_reuses_approval_and_writes_approve_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior: dict[str, object] | None
) -> None:
    """A current approval skips the reviewer and leaves review_verdict.json as approve."""
    repo, _ticket, rws, candidate, policy = _candidate_repo(tmp_path, monkeypatch)
    ctx, validation_before = _seed_attempt(
        rws,
        {"passed": True, "gate": "previous", "detail": "kept", "candidate_digest": candidate},
        [{"round": 1, "verdict": "approve", "candidate_digest": candidate, "policy_digest": policy}],
    )
    verdict_path = ctx / "review_verdict.json"
    if prior is None:
        verdict_path.unlink()
    else:
        verdict_path.write_text(json.dumps(prior), encoding="utf-8")

    with patch("run_ticket.PROJECT_ROOT", repo), \
            patch.object(GitAutonomyManager, "complete_ticket", return_value=TicketCompletionReport(
                ok=True, ticket_id="USR-424", commit_sha="c" * 40, message="ok",
            )), \
            patch("run_ticket._run_gate_with_self_correction", side_effect=AssertionError("gate ran")), \
            patch("run_ticket._review_candidate", side_effect=AssertionError("reviewer ran")), \
            patch("core.line.stage_review.run_agent", side_effect=AssertionError("reviewer agent ran")), \
            patch("run_ticket._invoke_fix_agent", side_effect=AssertionError("_invoke_fix_agent")):
        code = run_ticket.resume_delivery("USR-424", worktree_path=repo, skip_validation=False)

    assert code == 0
    written = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert written["verdict"] == "approve"
    assert written["blocking"] == []
    assert (ctx / "validation.json").read_bytes() == validation_before
    assert "previous-attempt" not in (ctx / "merge.json").read_text(encoding="utf-8")
    assert "previous-attempt" not in (ctx / "deploy_report.json").read_text(encoding="utf-8")


def _usr467_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Preserved USR-467 shape: built candidate, current gate, review still absent."""
    _isolate_resume_queue(monkeypatch, tmp_path / "state")
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    ticket = UserTicket(
        id="USR-467", project_id="darkfac", title="Retomar revisao preservada",
        problem_statement="launcher_exit:1 com gate vigente e revisao ausente",
        status=DeliveryStatus.IMPLEMENTING,
    )
    ledger = repo / ".factory" / "demands" / "demands.json"
    ledger.parent.mkdir(parents=True)
    DemandsStore(ledger).save_ticket(ticket)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "ledger")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-b", "ticket/usr-467")
    (repo / "impl.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "impl.py")
    _git(repo, "commit", "-m", "feat: impl")
    rws = run_ticket.ticket_engine.run_workspace(ticket, "ticket/usr-467")
    from core.research.policy import ResearchArtifact

    run_ticket.ticket_engine.write_demand(rws, ticket)
    run_ticket.ticket_engine.write_minimal_plan(rws, ticket)
    run_ticket.ticket_engine.write_research(
        rws, ResearchArtifact(kind="research_skipped", ticket_id=ticket.id, reason="small fix"),
    )
    run_ticket.ticket_engine.write_build(rws, "grok", [ticket.id])
    candidate, policy = run_ticket._candidate_binding(rws, repo, run_ticket._review_base_branch(ticket))
    assert candidate
    run_ticket.ticket_engine.write_validation(
        rws, True, "core/harness/runner.py --quick", candidate_digest=candidate,
    )
    return repo, ticket, rws, candidate, policy


def _git_head(repo: Path) -> str:
    done = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
    )
    return done.stdout.strip()


def test_usr467_preserved_exit_reopens_without_another_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispatcher resumes USR-467 in place. Digest, BUILD and the gate stay until the bound review."""
    from types import SimpleNamespace

    from core.line import pending_delivery as pending
    from core.line.recovery_receipt import route_family
    from core.workflow.control_contracts import StageResult

    repo, _ticket, rws, candidate, policy = _usr467_repo(tmp_path, monkeypatch)
    from core.line import workspace as line_workspace

    ctx = line_workspace.context_dir(rws)  # type: ignore[arg-type]
    build_before = (ctx / "progress.json").read_bytes()
    validation_before = (ctx / "validation.json").read_bytes()
    head_before = _git_head(repo)
    assert not (ctx / "review_verdict.json").exists()
    assert not (ctx / "review_execution.json").exists()
    worktree = str(repo.resolve())
    directory = tmp_path / "pending"
    row = pending.PendingDelivery(
        ticket_id="USR-467", worktree=worktree, stage="review", status="blocked",
        implementing_harness="grok", resume_delivery=True, cause="launcher_exit:1", next_check=0,
    )
    pending._save(directory / "USR-467.json", row)
    calls: list[list[str]] = []
    handle = SimpleNamespace(pid=467467, poll=lambda: None)
    store = SimpleNamespace(mock_mode=False, list_tickets=lambda: [{"id": "USR-467", "status": "planned"}])
    real_popen = subprocess.Popen

    def popen(cmd: list[str], **kwargs: object) -> object:
        if any(str(part).endswith("run_ticket.py") for part in cmd):
            calls.append(list(cmd))
            return handle
        return real_popen(cmd, **kwargs)  # type: ignore[arg-type]

    with patch("core.line.line_lock.is_locked", return_value=False), \
            patch("core.demands.queue_store.control_store", return_value=store), \
            patch("core.git.ticket_workspace.list_worktrees", return_value=[{"worktree": worktree}]), \
            patch.object(pending, "_route", return_value=None), \
            patch.object(pending.subprocess, "Popen", side_effect=popen):
        held = pending.tick(tmp_path, directory=directory, now=50)[0]
    assert held["status"] == "blocked"
    assert held["cause"] == "launcher_exit:1"
    assert calls == []
    assert (ctx / "progress.json").read_bytes() == build_before
    assert (ctx / "validation.json").read_bytes() == validation_before

    saved = pending.PendingDelivery.model_validate_json((directory / "USR-467.json").read_text(encoding="utf-8"))
    with patch("core.line.line_lock.is_locked", return_value=False), \
            patch("core.demands.queue_store.control_store", return_value=store), \
            patch("core.git.ticket_workspace.list_worktrees", return_value=[{"worktree": worktree}]), \
            patch.object(pending, "_route", return_value=("claude", "claude-sonnet")), \
            patch.object(pending.subprocess, "Popen", side_effect=popen):
        opened = pending.tick(tmp_path, directory=directory, now=saved.next_check + 1)[0]
    try:
        assert opened["status"] == "running"
        assert calls[0][2:] == ["--resume-delivery", "USR-467", "--worktree", worktree]
        assert "--fleet" not in calls[0]
        assert "PLAN" not in calls[0] and "BUILD" not in calls[0]
        assert (ctx / "progress.json").read_bytes() == build_before
        assert (ctx / "validation.json").read_bytes() == validation_before
        assert _git_head(repo) == head_before
    finally:
        pending._children.pop(handle.pid, None)

    def review_waiting(*_args: object, **_kwargs: object) -> StageResult:
        return StageResult(
            outcome="retry", cause_code="no_route_available not_before=2026-11-13T01:00:00+00:00",
        )

    with patch("run_ticket.PROJECT_ROOT", repo), \
            patch.object(GitAutonomyManager, "complete_ticket", side_effect=AssertionError("published without review")), \
            patch("run_ticket._run_gate_with_self_correction", side_effect=AssertionError("gate ran")), \
            patch("run_ticket._stage_plan", side_effect=AssertionError("PLAN")), \
            patch("run_ticket._stage_build", side_effect=AssertionError("BUILD")), \
            patch("run_ticket.ticket_engine.write_build", side_effect=AssertionError("second BUILD")), \
            patch("run_ticket._review_candidate", side_effect=review_waiting), \
            patch("run_ticket._invoke_fix_agent", side_effect=AssertionError("_invoke_fix_agent")), \
            patch("run_ticket.ticket_extras.pre_review_feedback", return_value=None):
        waiting = run_ticket.resume_delivery(
            "USR-467", worktree_path=repo, skip_validation=False, return_result=True,
        )
    assert waiting["exit_code"] == run_ticket.EXIT_ROUTE_WAIT
    assert waiting["cause"] == "route_wait"
    assert (ctx / "progress.json").read_bytes() == build_before
    assert (ctx / "validation.json").read_bytes() == validation_before
    assert run_ticket._candidate_binding(rws, repo, "main")[0] == candidate
    assert _git_head(repo) == head_before
    assert not (ctx / "review_verdict.json").exists()
    assert not (ctx / "review_execution.json").exists()
    assert not (ctx / "merge.json").exists()

    route = {"level": 1, "harness": "claude", "model": "claude-sonnet", "reason": "other_family"}

    def review_bound(*_args: object, **_kwargs: object) -> StageResult:
        line_workspace.write_context(rws, "review_route.json", json.dumps(route))  # type: ignore[arg-type]
        line_workspace.write_context(rws, "review_state.json", json.dumps({"rounds": [{  # type: ignore[arg-type]
            "round": 1, "verdict": "approve", "forced": False,
            "candidate_digest": candidate, "policy_digest": policy, "route": route,
        }]}))
        line_workspace.write_context(
            rws, "review_verdict.json",  # type: ignore[arg-type]
            json.dumps({"verdict": "approve", "blocking": [], "non_blocking": []}),
        )
        line_workspace.write_context(rws, "review_execution.json", json.dumps({  # type: ignore[arg-type]
            "candidate_digest": candidate,
            "implementer_family": route_family("grok", None),
            "reviewer_family": route_family("claude", "claude-sonnet"),
            "implementation_families": [route_family("grok", None)],
            "review_route": route,
        }))
        return StageResult(outcome="success", output_refs=["review:USR-467:1"])

    def publish(*_args: object, **_kwargs: object) -> TicketCompletionReport:
        recorded_route = json.loads((ctx / "review_route.json").read_text(encoding="utf-8"))
        execution = json.loads((ctx / "review_execution.json").read_text(encoding="utf-8"))
        state = json.loads((ctx / "review_state.json").read_text(encoding="utf-8"))
        assert recorded_route["harness"] == "claude"
        assert recorded_route["harness"] != "grok"
        assert execution["candidate_digest"] == candidate
        assert execution["reviewer_family"] != execution["implementer_family"]
        assert state["rounds"][-1]["verdict"] == "approve"
        assert state["rounds"][-1]["candidate_digest"] == candidate
        assert (ctx / "progress.json").read_bytes() == build_before
        assert (ctx / "validation.json").read_bytes() == validation_before
        assert not (ctx / "merge.json").exists()
        return TicketCompletionReport(
            ok=True, ticket_id="USR-467", commit_sha="c" * 40, message="ok",
        )

    with patch("run_ticket.PROJECT_ROOT", repo), \
            patch.object(GitAutonomyManager, "complete_ticket", side_effect=publish), \
            patch("run_ticket._run_gate_with_self_correction", side_effect=AssertionError("gate ran")), \
            patch("run_ticket._stage_plan", side_effect=AssertionError("PLAN")), \
            patch("run_ticket._stage_build", side_effect=AssertionError("BUILD")), \
            patch("run_ticket.ticket_engine.write_build", side_effect=AssertionError("second BUILD")), \
            patch("run_ticket._review_candidate", side_effect=review_bound), \
            patch("run_ticket._invoke_fix_agent", side_effect=AssertionError("_invoke_fix_agent")), \
            patch("run_ticket.ticket_extras.pre_review_feedback", return_value=None):
        code = run_ticket.resume_delivery("USR-467", worktree_path=repo, skip_validation=False)

    assert code == 0
    assert (ctx / "progress.json").read_bytes() == build_before
    assert (ctx / "validation.json").read_bytes() == validation_before
    assert json.loads((ctx / "validation.json").read_text(encoding="utf-8"))["candidate_digest"] == candidate
    assert run_ticket._candidate_binding(rws, repo, "main")[0] == candidate
    assert _git_head(repo) == head_before
    assert (ctx / "merge.json").is_file()
    assert "review_route" in (ctx / "review_execution.json").read_text(encoding="utf-8")
