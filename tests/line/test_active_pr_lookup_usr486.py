"""USR-486: classify a failed gh pr list instead of blaming GitHub for every outage."""

from __future__ import annotations

import contextlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import run_ticket
from core.demands import DemandsStore, UserTicket
from core.line import bindings, owner_intake
from core.line.active_pull_request import (
    GH_AUTH_LOGIN_COMMAND,
    GH_AUTH_STATUS_COMMAND,
    GH_INSTALL_COMMAND,
    PullRequestLookupError,
    find_active_pull_request,
)
from core.workflow.control_store import SQLiteControlStore

LEGACY_LOOKUP_MESSAGE = "GitHub PR lookup failed; retry when GitHub is available"


def _registry(url: str = "https://github.com/org/repo.git") -> SimpleNamespace:
    return SimpleNamespace(get_project=lambda _project: SimpleNamespace(repo_url=url))


def _ticket() -> UserTicket:
    return UserTicket(
        id="USR-486",
        title="Classify gh failures",
        problem_statement="A missing gh binary must not look like a GitHub outage.",
        core_journey=["Refuse with the real cause"],
        non_goals=["Do not open a second implementation"],
        acceptance_criteria=["Name gh_missing, gh_unauthenticated, gh_timeout and github_error"],
    )


def _raise_lookup(cause: str, fix_command: str) -> PullRequestLookupError:
    registry = _registry()

    def runner(*_args: object, **_kwargs: object) -> None:
        if cause == "gh_missing":
            raise FileNotFoundError("gh")
        if cause == "gh_timeout":
            raise subprocess.TimeoutExpired(["gh", "pr", "list"], 20)
        stderr = "not logged in" if cause == "gh_unauthenticated" else "HTTP 502"
        return SimpleNamespace(returncode=1, stdout="", stderr=stderr)  # type: ignore[return-value]

    with pytest.raises(PullRequestLookupError) as caught:
        find_active_pull_request("darkfac", "USR-486", project_registry=registry, run=runner)
    error = caught.value
    assert error.cause == cause
    assert error.fix_command == fix_command
    return error


def test_file_not_found_is_gh_missing() -> None:
    error = _raise_lookup("gh_missing", GH_INSTALL_COMMAND)
    assert "winget install" in error.fix_command
    assert "GitHub.cli" in error.fix_command


def test_not_logged_in_is_gh_unauthenticated() -> None:
    error = _raise_lookup("gh_unauthenticated", GH_AUTH_LOGIN_COMMAND)
    assert error.fix_command == "gh auth login"


def test_timeout_is_gh_timeout() -> None:
    error = _raise_lookup("gh_timeout", GH_AUTH_STATUS_COMMAND)
    assert error.fix_command == "gh auth status"


def test_http_502_is_github_error() -> None:
    error = _raise_lookup("github_error", "gh pr list --state open --repo org/repo")
    assert error.cause == "github_error"
    assert "502" in str(error)
    assert "gh pr list --state open --repo" in (error.fix_command or "")
    assert "org/repo" in (error.fix_command or "")


def test_other_oserror_stays_fail_closed_without_a_new_cause() -> None:
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise OSError("offline")

    with pytest.raises(PullRequestLookupError, match="retry when GitHub is available") as caught:
        find_active_pull_request("darkfac", "USR-486", project_registry=_registry(), run=unavailable)
    assert caught.value.cause is None
    assert caught.value.fix_command is None


def test_launcher_json_refuses_classified_lookup_without_worktree(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    ticket = _ticket()
    error = _raise_lookup("gh_missing", GH_INSTALL_COMMAND)

    def lookup(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(run_ticket, "_stage_line_lock", lambda *_args: None)
    monkeypatch.setattr(run_ticket, "_stage_check_harness", lambda *_args: None)
    monkeypatch.setattr(run_ticket, "inspect_quotas", lambda: {})
    monkeypatch.setattr(run_ticket, "_stage_intake", lambda *_args: (ticket, 0))
    monkeypatch.setattr(run_ticket, "find_active_pull_request", lookup)
    monkeypatch.setattr(run_ticket, "_prepare_workspace", lambda *_args: pytest.fail("workspace must not be created"))

    result = run_ticket._run(["USR-486", "--harness", "codex", "--json"], [], contextlib.ExitStack())
    output = json.loads(capsys.readouterr().err)

    assert result == 2
    assert output["reason"] == "pull_request_lookup_unavailable"
    assert output["cause"] == "gh_missing"
    assert output["fix_command"] == GH_INSTALL_COMMAND
    assert output["ticket_id"] == "USR-486"


def test_owner_intake_returns_cause_and_command_without_opening_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = _ticket()
    demands = DemandsStore(tmp_path / "demands.json")
    demands.save_ticket(ticket)
    error = _raise_lookup("gh_unauthenticated", GH_AUTH_LOGIN_COMMAND)

    def lookup(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(owner_intake, "find_active_pull_request", lookup)
    monkeypatch.setattr(
        owner_intake,
        "open_line_store",
        lambda *_args, **_kwargs: pytest.fail("line store must stay closed"),
    )

    result = owner_intake.submit_ticket_to_line(ticket.id, demands_store=demands)

    assert not result.ok
    assert result.state == "gh_unauthenticated"
    assert GH_AUTH_LOGIN_COMMAND in result.message
    assert result.run_id is None


def test_development_uses_lookup_cause_and_keeps_legacy_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket_id = "USR-486"
    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    reservation = store.reserve_cycle(
        project_id="darkfac", ticket_id=ticket_id, run_id="current-run", host_id="desktop",
        now=datetime.now(UTC), lease_duration_sec=600,
    )
    classified = _raise_lookup("gh_missing", GH_INSTALL_COMMAND)

    def classified_lookup(*_args: object, **_kwargs: object) -> None:
        raise classified

    def legacy_lookup(*_args: object, **_kwargs: object) -> None:
        raise PullRequestLookupError(LEGACY_LOOKUP_MESSAGE)

    class DevelopmentStage:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(self, *_args: object) -> object:
            pytest.fail("development must not run when the active PR cannot be verified")

    monkeypatch.setattr(bindings.stage_build, "DevelopmentStage", DevelopmentStage)
    handler = bindings.DevelopmentStageHandler(
        project_resolver=lambda _project_id: SimpleNamespace(
            id="darkfac", repo_url="https://github.com/org/repo.git",
        ),
        store=store,
    )
    context = SimpleNamespace(
        project_id="darkfac",
        claim=SimpleNamespace(job_key=SimpleNamespace(ticket_id=ticket_id, run_id=reservation.run_id)),
    )

    monkeypatch.setattr(bindings, "find_active_pull_request", classified_lookup)
    classified_result = handler.handle(context)  # type: ignore[arg-type]
    assert classified_result.outcome == "failed"
    assert classified_result.cause_code == "gh_missing"
    assert GH_INSTALL_COMMAND in classified_result.evidence_refs

    monkeypatch.setattr(bindings, "find_active_pull_request", legacy_lookup)
    legacy_result = handler.handle(context)  # type: ignore[arg-type]
    assert legacy_result.outcome == "failed"
    assert legacy_result.cause_code == "active_pr_lookup_failed"
    assert legacy_result.evidence_refs == [LEGACY_LOOKUP_MESSAGE]
