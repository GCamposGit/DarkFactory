"""Tests for USR-73: live proof conditional completion in git autonomy engine."""

import json
import subprocess
from pathlib import Path
from typing import Any
import pytest

from core.git.autonomy import GitAutonomyManager, TicketCompletionReport
from core.demands.models import DemandOrigin, TAG_USER_DEMAND, UserTicket
from core.demands.store import DemandsStore
from core.roadmap.models import DeliveryStatus, LifecycleStage, PlanningHorizon, RoadmapItemType


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


@pytest.fixture
def test_remote_pair(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "--quiet", "-b", "main")

    local = tmp_path / "local"
    local.mkdir()
    _git(local, "init", "--quiet", "-b", "main")
    _git(local, "config", "user.name", "DarkFac Test")
    _git(local, "config", "user.email", "test@darkfac.internal")
    _git(local, "config", "commit.gpgsign", "false")
    (local / "README.md").write_text("# Project\n", encoding="utf-8")
    _git(local, "add", "README.md")
    _git(local, "commit", "--quiet", "-m", "chore: initial commit")
    _git(local, "remote", "add", "origin", str(bare))
    _git(local, "push", "--quiet", "-u", "origin", "main")
    return local, bare


class FakeSyncReport:
    def __init__(self, ok: bool, expected_sha: str = "a" * 40, reason: str = "") -> None:
        self.ok = ok
        self.expected_sha = expected_sha
        self.reason = reason

    def __str__(self) -> str:
        return f"FakeSyncReport(ok={self.ok}, reason={self.reason})"


def test_deploy_live_ticket_completed_when_live_verifier_succeeds(test_remote_pair: tuple[Path, Path]) -> None:
    local, _ = test_remote_pair
    store_file = local / ".factory" / "demands" / "demands.json"
    store = DemandsStore(store_file)

    ticket = UserTicket(
        id="USR-200",
        project_id="darkfac",
        title="Deploy live infrastructure change",
        origin=DemandOrigin.USER,
        status=DeliveryStatus.PLANNED,
        item_type=RoadmapItemType.FEATURE,
        lifecycle_stage=LifecycleStage.EXECUTION,
        horizon=PlanningHorizon.NOW,
        tags=[TAG_USER_DEMAND, "deploy-live"],
        problem_statement="Test deploy live problem",
        core_journey=["Test journey"],
        acceptance_criteria=["Live convergence verified on all nodes"],
    )
    store.save_ticket(ticket)

    # Add implementation change
    (local / "infra_impl.py").write_text("# implementation\n", encoding="utf-8")

    engine = GitAutonomyManager(local)

    # Successful verifier
    report = engine.complete_ticket(
        ticket_id="USR-200",
        cwd=local,
        auto_push=False,
        live_verifier=lambda sha: FakeSyncReport(ok=True, expected_sha=sha),
    )

    assert report.ok is True
    updated = store.get_ticket("USR-200")
    assert updated is not None
    assert updated.status == DeliveryStatus.COMPLETED
    assert ":live_converged" in (updated.delivery_evidence or "")


def test_deploy_live_ticket_reverts_to_planned_and_opens_defect_on_failure(test_remote_pair: tuple[Path, Path]) -> None:
    local, _ = test_remote_pair
    store_file = local / ".factory" / "demands" / "demands.json"
    store = DemandsStore(store_file)

    ticket = UserTicket(
        id="USR-201",
        project_id="darkfac",
        title="Deploy live change that will fail verification",
        origin=DemandOrigin.USER,
        status=DeliveryStatus.PLANNED,
        item_type=RoadmapItemType.FEATURE,
        lifecycle_stage=LifecycleStage.EXECUTION,
        horizon=PlanningHorizon.NOW,
        tags=[TAG_USER_DEMAND, "deploy-live"],
        problem_statement="Test deploy live problem",
        core_journey=["Test journey"],
        acceptance_criteria=["Live proof on all nodes"],
    )
    store.save_ticket(ticket)

    # Add implementation change
    (local / "infra_impl.py").write_text("# implementation\n", encoding="utf-8")

    engine = GitAutonomyManager(local)

    # Failing verifier
    report = engine.complete_ticket(
        ticket_id="USR-201",
        cwd=local,
        auto_push=False,
        live_verifier=lambda sha: FakeSyncReport(ok=False, reason="Desktop divergent"),
    )

    assert report.ok is False
    assert "Live convergence proof failed" in report.message

    # Ticket reverted to planned
    reverted = store.get_ticket("USR-201")
    assert reverted is not None
    assert reverted.status == DeliveryStatus.PLANNED
    assert reverted.delivery_evidence is None
    assert "[LIVE_CONVERGENCE_FAILURE]" in reverted.problem_statement

    # Defect ticket opened automatically
    all_tickets = store.list_tickets()
    defect = next((t for t in all_tickets if "USR-201" in t.title and "Defeito:" in t.title), None)
    assert defect is not None
    assert defect.status == DeliveryStatus.PLANNED
    assert "defect" in defect.tags
    assert "USR-201" in defect.dependencies


def test_deploy_live_ticket_reverts_on_verifier_exception(test_remote_pair: tuple[Path, Path]) -> None:
    local, _ = test_remote_pair
    store_file = local / ".factory" / "demands" / "demands.json"
    store = DemandsStore(store_file)

    ticket = UserTicket(
        id="USR-202",
        project_id="darkfac",
        title="Deploy live with network exception during probe",
        origin=DemandOrigin.USER,
        status=DeliveryStatus.PLANNED,
        item_type=RoadmapItemType.FEATURE,
        lifecycle_stage=LifecycleStage.EXECUTION,
        horizon=PlanningHorizon.NOW,
        tags=[TAG_USER_DEMAND, "deploy-live"],
        problem_statement="Initial statement",
        core_journey=["Journey"],
        acceptance_criteria=["Live verification"],
    )
    store.save_ticket(ticket)

    (local / "infra_impl.py").write_text("# implementation\n", encoding="utf-8")

    engine = GitAutonomyManager(local)

    def boom(sha: str) -> Any:
        raise ConnectionError("Desktop node unreachable")

    report = engine.complete_ticket(
        ticket_id="USR-202",
        cwd=local,
        auto_push=False,
        live_verifier=boom,
    )

    assert report.ok is False
    assert "ConnectionError" in report.message

    t = store.get_ticket("USR-202")
    assert t is not None
    assert t.status == DeliveryStatus.PLANNED
    assert t.delivery_evidence is None


def test_demands_store_rejects_completed_deploy_live_without_proof(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    store = DemandsStore(repo / ".factory" / "demands" / "demands.json")

    ticket = UserTicket(
        id="USR-203",
        project_id="darkfac",
        title="Cannot bypass live proof via store edit",
        origin=DemandOrigin.USER,
        status=DeliveryStatus.COMPLETED,
        delivery_evidence="plain_commit_sha_without_live_proof",
        item_type=RoadmapItemType.FEATURE,
        lifecycle_stage=LifecycleStage.EXECUTION,
        horizon=PlanningHorizon.NOW,
        tags=[TAG_USER_DEMAND, "deploy-live"],
        problem_statement="Problem",
        core_journey=["Journey"],
        acceptance_criteria=["Live proof"],
    )

    with pytest.raises(ValueError, match="requires live convergence proof in delivery_evidence"):
        store.save_ticket(ticket)
