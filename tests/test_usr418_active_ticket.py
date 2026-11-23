"""USR-418: open PRs and canonical leases prevent a second implementation."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.demands import DemandsStore, UserTicket
from core.line import line_lock, owner_intake
from core.line.active_pull_request import (
    ActivePullRequest,
    PullRequestLookupError,
    classify_pull_request_disposition,
    find_active_pull_request,
)
import run_ticket
from core.workflow.control_contracts import TicketAlreadyActiveError
from core.workflow.control_store import SQLiteControlStore


def test_active_pr_lookup_is_ticket_exact_and_fails_closed() -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps([
            {"number": 91, "url": "https://github.com/org/repo/pull/91", "title": "USR-4184 follow-up",
             "headRefName": "ticket/usr-4184", "body": ""},
            {"number": 93, "url": "https://github.com/org/repo/pull/93", "title": "darkfac maintenance",
             "headRefName": "ticket/darkfac-maintenance", "body": ""},
            {"number": 94, "url": "https://github.com/org/repo/pull/94", "title": "USR-419 follow-up",
             "headRefName": "ticket/usr-419", "body": ""},
            {"number": 92, "url": "https://github.com/org/repo/pull/92", "title": "Parallel work",
             "headRefName": "ticket/usr-418", "body": "", "isDraft": True, "isCrossRepository": False},
        ]))

    registry = SimpleNamespace(get_project=lambda _project: SimpleNamespace(repo_url="https://github.com/org/repo.git"))
    result = find_active_pull_request("darkfac", "USR-418", project_registry=registry, run=fake_run)
    assert result == ActivePullRequest(
        92, "https://github.com/org/repo/pull/92", "Parallel work", "ticket/usr-418", draft=True
    )
    assert result is not None and result.cross_repository is False
    assert set(result.to_dict()) == {"number", "url", "title", "head_ref"}
    assert "org/repo" in calls[0]
    assert "number,url,title,headRefName,body,isDraft,isCrossRepository" in calls[0]

    def unavailable(*_: object, **__: object) -> None:
        raise OSError("offline")

    with pytest.raises(PullRequestLookupError):
        find_active_pull_request("darkfac", "USR-418", project_registry=registry, run=unavailable)

    missing_repo = SimpleNamespace(get_project=lambda _project: None)
    with pytest.raises(PullRequestLookupError, match="cannot be verified"):
        find_active_pull_request("darkfac", "USR-418", project_registry=missing_repo, run=fake_run)


def _checks_runner(rows: object, returncode: int = 0) -> object:
    def runner(_args: object, _cwd: Path) -> SimpleNamespace:
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(rows), stderr="")

    return runner


def _pr(head_ref: str = "ticket/usr-545", **kwargs: bool) -> ActivePullRequest:
    return ActivePullRequest(920, "https://github.com/org/repo/pull/920", "USR-545", head_ref, **kwargs)


@pytest.mark.parametrize(
    ("pr", "rows", "action"),
    [
        (_pr(draft=True), [{"bucket": "pass", "name": "pr-validation", "workflow": "DarkFac CI"}], "adopt_delivery"),
        (_pr("ticket/usr-545-20261123T105238Z"), [], "adopt_delivery"),
        (_pr(), [{"bucket": "fail", "name": "pr-validation", "workflow": "DarkFac CI"}], "fix"),
        (_pr(), [{"bucket": "pending", "name": "pr-validation"}], "wait"),
        (_pr("ticket/usr-5450"), [{"bucket": "pass", "name": "x"}], "wait"),
        (_pr("feature/other"), [{"bucket": "pass", "name": "x"}], "wait"),
        (_pr(cross_repository=True), [{"bucket": "pass", "name": "x"}], "wait"),
        (_pr(), "not-a-list", "wait"),
    ],
)
def test_pull_request_disposition_from_ci_state(pr: ActivePullRequest, rows: object, action: str) -> None:
    disposition = classify_pull_request_disposition(pr, "USR-545", runner=_checks_runner(rows))
    assert disposition.action == action
    assert disposition.pr_number == 920
    assert disposition.reason


def test_pull_request_disposition_fails_closed_without_leaking_secrets() -> None:
    def broken(_args: object, _cwd: Path) -> SimpleNamespace:
        raise OSError("boom ghp_secrettoken123")

    disposition = classify_pull_request_disposition(_pr(), "USR-545", runner=broken)
    assert (disposition.action, disposition.pr_number) == ("wait", 920)
    assert "ghp_" not in disposition.reason

    errored = SimpleNamespace(returncode=1, stdout="", stderr="HTTP 502 ghp_secrettoken123")
    disposition = classify_pull_request_disposition(_pr(), "USR-545", runner=lambda *_: errored)
    assert disposition.action == "wait"
    assert "ghp_" not in disposition.reason


def test_launcher_and_owner_intake_refuse_active_pr_before_creating_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ticket = UserTicket(
        id="USR-418", title="Active PR", problem_statement="Do not duplicate active implementation.",
        core_journey=["Detect the open PR"], non_goals=["Do not change PR"],
        acceptance_criteria=["Refuse duplicate admission"],
    )
    demands = DemandsStore(tmp_path / "demands.json")
    demands.save_ticket(ticket)
    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    existing = ActivePullRequest(779, "https://github.com/org/repo/pull/779", "USR-418", "ticket/usr-418")
    monkeypatch.setattr(owner_intake, "find_active_pull_request", lambda *_a, **_k: existing)

    result = owner_intake.submit_ticket_to_line(ticket.id, demands_store=demands, store=store, now=datetime.now(UTC))
    assert not result.ok
    assert result.state == "active_pull_request"
    assert result.run_id == "pr-779"
    assert "https://github.com/org/repo/pull/779" in result.message
    assert store.find_intake_runs("owner", "ticket:USR-418") == []
    existing = ActivePullRequest(779, "https://github.com/org/repo/pull/779", "USR-418", "ticket/usr-418")
    monkeypatch.setattr(run_ticket, "_stage_line_lock", lambda *_a: None)
    monkeypatch.setattr(run_ticket, "_stage_check_harness", lambda *_a: None)
    monkeypatch.setattr(run_ticket, "inspect_quotas", lambda: {})
    monkeypatch.setattr(run_ticket, "_stage_intake", lambda *_a: (ticket, 0))
    monkeypatch.setattr(run_ticket, "find_active_pull_request", lambda *_a, **_k: existing)
    monkeypatch.setattr(run_ticket, "_prepare_workspace", lambda *_a: pytest.fail("workspace must not be created"))

    result = run_ticket._run(["USR-418", "--harness", "codex", "--json"], [], contextlib.ExitStack())
    output = json.loads(capsys.readouterr().err)
    assert result == 2
    assert output["reason"] == "active_pull_request"
    assert output["existing_execution"]["url"] == existing.url


def test_owner_intake_reports_active_pr_before_opening_unavailable_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = UserTicket(
        id="USR-418", title="Active PR", problem_statement="Do not duplicate active implementation.",
        core_journey=["Detect the open PR"], non_goals=["Do not change PR"],
        acceptance_criteria=["Report existing PR even when the line store is unavailable"],
    )
    demands = DemandsStore(tmp_path / "demands.json")
    demands.save_ticket(ticket)
    existing = ActivePullRequest(779, "https://github.com/org/repo/pull/779", "USR-418", "ticket/usr-418")
    monkeypatch.setattr(owner_intake, "find_active_pull_request", lambda *_a, **_k: existing)
    monkeypatch.setattr(
        owner_intake, "open_line_store",
        lambda *_a, **_k: pytest.fail("active PR must be reported before opening the line store"),
    )

    result = owner_intake.submit_ticket_to_line(ticket.id, demands_store=demands)

    assert not result.ok
    assert result.state == "active_pull_request"
    assert result.run_id == "pr-779"
    assert existing.url in result.message


def test_canonical_owner_conflict_reports_existing_run_as_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    info = line_lock.LineLockInfo(
        pid=1234, ticket_id="USR-418", hostname="notebook", started_at=1_800_000_000,
        heartbeat_at=1_800_000_010,
    )

    class RefusingContext:
        def __enter__(self) -> None:
            raise line_lock.LineLockHeld(info)

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(line_lock, "guard", lambda *_a, **_k: RefusingContext())
    args = run_ticket.build_parser().parse_args(["USR-418", "--json"])
    result = run_ticket._stage_line_lock(args, contextlib.ExitStack(), None)
    output = json.loads(capsys.readouterr().err)
    assert result == 2
    assert output["reason"] == "canonical_ownership_conflict"
    assert output["existing_execution"]["ticket_id"] == "USR-418"
    assert output["existing_execution"]["pid"] == 1234


