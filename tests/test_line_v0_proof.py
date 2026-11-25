import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from core.line.v0_proof import DeliveryEvidence, collect_v0_proof_evidence, evaluate_v0_proof

NOW = datetime(2026, 2, 1, tzinfo=UTC)


def _delivery(index: int, *, channel: str, project_id: str = "darkfac") -> DeliveryEvidence:
    return DeliveryEvidence(
        ticket_id=f"USR-{index}", project_id=project_id, channel=channel,
        run_id=f"run-{index}", merge_sha=f"{index:040x}", merge_on_default_branch=True,
        delivery_evidence="merged", smoke_passed=True, learning_pack_linked=True,
        occurred_at=(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)).isoformat(),
    )


def _five(**last: object) -> list[DeliveryEvidence]:
    deliveries = [
        _delivery(1, channel="telegram"), _delivery(2, channel="hub"),
        _delivery(3, channel="chat"), _delivery(4, channel="telegram"),
        _delivery(5, channel="hub", project_id="external-product"),
    ]
    for key, value in last.items():
        setattr(deliveries[-1], key, value)
    return deliveries


def test_v0_slots_preserve_delivery_identity_and_zero_interventions() -> None:
    report = evaluate_v0_proof(_five(), now=NOW)

    assert report.criterion_met is True
    assert [slot.ticket_id for slot in report.slots] == [f"USR-{i}" for i in range(1, 6)]
    assert all(slot.run_id and slot.project_id and slot.channel and slot.merge_sha for slot in report.slots)
    assert all(slot.learning_pack_ref and slot.owner_interventions_count == 0 for slot in report.slots)


def test_v0_exposes_intervention_kinds_and_does_not_approve_when_source_degraded() -> None:
    report = evaluate_v0_proof(
        _five(owner_decisions_count=1, intervention_types=["decision"]), now=NOW,
        degraded_sources=["owner_actions_read_failed"],
    )

    assert report.criterion_met is False
    assert "nao foi possivel comprovar a contagem de intervencoes humanas" in report.missing
    assert report.slots[-1].owner_interventions_count == 1
    assert report.slots[-1].owner_intervention_types == ["decision"]


def test_v0_merge_outside_default_branch_breaks_the_streak() -> None:
    report = evaluate_v0_proof(_five(merge_on_default_branch=False), now=NOW)

    assert report.criterion_met is False
    assert report.current_streak == 0


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _ticket(ticket_id: str, project_id: str, created: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id=ticket_id, project_id=project_id, created_at=created, tags=["channel:telegram"],
        status=SimpleNamespace(value="completed"), delivery_evidence="",
    )


def _emit_receipts(artifacts: Path, ticket_id: str, sha: str) -> None:
    run_dir = artifacts / ticket_id / ".darkfac" / "runs" / ticket_id
    run_dir.mkdir(parents=True)
    (run_dir / "merge.json").write_text(json.dumps({"pr_url": "u", "merge_sha": sha}), encoding="utf-8")
    (run_dir / "deploy_report.json").write_text(
        json.dumps({"sha": sha, "smoke_ok": True, "deployed": True}), encoding="utf-8"
    )


def test_collector_counts_external_delivery_from_engine_receipts_and_rejects_foreign_sha(tmp_path: Path) -> None:
    repo = tmp_path / "external"
    repo.mkdir()
    _git(repo, "init", "-b", "darkfac/m1-seo")
    (repo / "a.txt").write_text("a", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "m1")
    on_branch = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "side")
    (repo / "b.txt").write_text("b", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "side")
    off_branch = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "darkfac/m1-seo")

    artifacts = tmp_path / "artifacts"
    _emit_receipts(artifacts, "MDL-01", on_branch)
    _emit_receipts(artifacts, "MDL-02", off_branch)
    packs = tmp_path / "packs"
    packs.mkdir()
    (packs / "MDL-01.json").write_text("{}", encoding="utf-8")
    (packs / "MDL-02.json").write_text("{}", encoding="utf-8")

    created = datetime(2026, 1, 1, tzinfo=UTC)
    tickets = [_ticket("MDL-01", "mansaodolago", created), _ticket("MDL-02", "mansaodolago", created)]
    project = SimpleNamespace(
        default_branch="darkfac/m1-seo", resolve_path=lambda: repo, requires_commercial_acceptance=True
    )
    solo = collect_v0_proof_evidence(
        demands_store=SimpleNamespace(list_tickets=lambda: tickets[:1]),
        owner_actions_store=SimpleNamespace(list_actions=list),
        project_registry=SimpleNamespace(get_project=lambda _id: project),
        learning_packs_dir=packs, artifacts_dir=artifacts, now=NOW, required_streak=1,
    )
    assert [d.ticket_id for d in solo.qualified_deliveries] == ["MDL-01"]
    assert solo.qualified_deliveries[0].merge_sha == on_branch
    assert solo.projects_involved == ["mansaodolago"]
    foreign = collect_v0_proof_evidence(
        demands_store=SimpleNamespace(list_tickets=lambda: tickets[1:]),
        owner_actions_store=SimpleNamespace(list_actions=list),
        project_registry=SimpleNamespace(get_project=lambda _id: project),
        learning_packs_dir=packs, artifacts_dir=artifacts, now=NOW, required_streak=1,
    )
    assert foreign.current_streak == 0
