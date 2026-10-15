"""USR-264: a transient ``git show :path`` failure must not abort delivery with a traceback."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Sequence

import pytest

from core.demands.models import DemandOrigin, LifecycleStage, PlanningHorizon, RoadmapItemType, UserTicket
from core.demands.store import DemandsStore
from core.git import autonomy
from core.git.autonomy import STAGED_INSPECT_FAILED, GitAutonomyManager
from core.roadmap.models import DeliveryStatus


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, encoding="utf-8", check=True
    )


@pytest.fixture
def ticket_repo(tmp_path: Path) -> Path:
    """A repository with a ledger holding a planned ticket and an implementation change."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.name", "DarkFac Test")
    _git(repo, "config", "user.email", "test@darkfac.internal")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# Project\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--quiet", "-m", "chore: initial commit")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    store = DemandsStore(repo / ".factory" / "demands" / "demands.json")
    store.save_ticket(
        UserTicket(
            id="USR-264",
            project_id="darkfac",
            title="Retry staged inspect",
            origin=DemandOrigin.USER,
            status=DeliveryStatus.PLANNED,
            item_type=RoadmapItemType.FEATURE,
            lifecycle_stage=LifecycleStage.EXECUTION,
            horizon=PlanningHorizon.NOW,
            tags=["bug"],
            problem_statement="Transient git show failure",
            acceptance_criteria=["Retries"],
        )
    )
    _git(repo, "add", ".factory/demands/demands.json")
    _git(repo, "commit", "--quiet", "-m", "chore: ledger")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "engine.py").write_text("VALUE = 1\n", encoding="utf-8")
    return repo


def _flaky_show(monkeypatch: pytest.MonkeyPatch, failures: int) -> list[Sequence[str]]:
    """Make the first ``failures`` index reads fail; every other git call is real."""
    real_run_git = autonomy._run_git
    shows: list[Sequence[str]] = []

    def fake_run_git(args: Sequence[str], cwd: Path = autonomy.PROJECT_ROOT, timeout_s: float = 60.0,
                     check: bool = False) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "show" and str(args[1]).startswith(":"):
            shows.append(list(args))
            if len(shows) <= failures:
                return subprocess.CompletedProcess(
                    ["git", *args], 128, "", "fatal: unable to read index.lock contention"
                )
        return real_run_git(args, cwd=cwd, timeout_s=timeout_s, check=check)

    monkeypatch.setattr(autonomy, "_run_git", fake_run_git)
    return shows


def test_transient_index_read_failure_is_retried_and_commits(
    ticket_repo: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    shows = _flaky_show(monkeypatch, failures=2)
    pauses: list[float] = []
    mgr = GitAutonomyManager(ticket_repo, staged_show_sleep_fn=pauses.append)

    report = mgr.complete_ticket("USR-264", cwd=ticket_repo, auto_push=False)

    assert report.ok, report.message
    assert pauses == [autonomy._STAGED_SHOW_PAUSE_S, autonomy._STAGED_SHOW_PAUSE_S]
    assert len(shows) >= 3
    assert "exit 128" in caplog.text or "fatal: unable to read index.lock" in caplog.text
    assert _git(ticket_repo, "status", "--porcelain").stdout.strip() == ""
    ticket = DemandsStore(ticket_repo / ".factory" / "demands" / "demands.json").get_ticket("USR-264")
    assert ticket is not None and ticket.status == DeliveryStatus.COMPLETED


def test_persistent_index_read_failure_ends_with_explicit_cause(
    ticket_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _flaky_show(monkeypatch, failures=10_000)
    pauses: list[float] = []
    phases: list[tuple[str, str, str, str | None]] = []
    mgr = GitAutonomyManager(
        ticket_repo,
        staged_show_sleep_fn=pauses.append,
        on_phase=lambda phase, status, message, cause: phases.append((phase, status, message, cause)),
    )

    report = mgr.complete_ticket("USR-264", cwd=ticket_repo, auto_push=False)

    assert not report.ok
    assert report.delivery is not None
    assert report.delivery.action == STAGED_INSPECT_FAILED
    assert STAGED_INSPECT_FAILED in report.message
    assert "engine.py" in report.message
    assert "fatal: unable to read index.lock contention" in report.message
    assert len(pauses) == autonomy._STAGED_SHOW_ATTEMPTS - 1
    assert ("commit", "failed") in [(p, s) for p, s, _, _ in phases]
    assert any(cause == STAGED_INSPECT_FAILED for _, _, _, cause in phases)
