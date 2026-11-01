"""Tests for the V0 proof engine and evidence collector (USR-279)."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.line import owner_intake
from core.line.bindings import LINE_STAGES
from core.line.v0_proof import (
    REQUIRED_STREAK,
    DeliveryEvidence,
    V0ProofReport,
    collect_v0_proof_evidence,
    evaluate_v0_proof,
    render_v0_proof_markdown,
    save_v0_proof_markdown,
    save_v0_proof_report,
)
from core.line.v0_proof import (
    main as v0_proof_main,
)
from core.owner_actions.models import (
    ActionKind,
    ActionPriority,
    ActionStep,
    OwnerAction,
)
from core.owner_actions.store import OwnerActionStore
from core.projects.registry import ProjectRegistry
from core.workflow.control_store import SQLiteControlStore

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _good_delivery(ticket_id: str, channel: str, project_id: str = "darkfac") -> DeliveryEvidence:
    return DeliveryEvidence(
        ticket_id=ticket_id,
        project_id=project_id,
        channel=channel,
        run_id=f"run-{ticket_id}",
        learning_pack_linked=True,
        manual_page_changed=True,
        merge_sha="a" * 40,
        smoke_passed=True,
    )


# ---------------------------------------------------------------------------
# evaluate_v0_proof: pure rule
# ---------------------------------------------------------------------------


def test_empty_history_has_zero_streak_and_is_not_met() -> None:
    report = evaluate_v0_proof([], now=NOW)
    assert isinstance(report, V0ProofReport)
    assert report.current_streak == 0
    assert report.criterion_met is False
    assert report.missing
    # USR-279 T3: the DarkHub /live panel always gets REQUIRED_STREAK slots, all empty here.
    assert len(report.slots) == REQUIRED_STREAK
    assert all(not slot.filled for slot in report.slots)
    assert [slot.index for slot in report.slots] == list(range(1, REQUIRED_STREAK + 1))


def test_five_qualified_deliveries_across_channels_and_a_real_product_meet_the_criterion() -> None:
    deliveries = [
        _good_delivery("USR-1", "telegram"),
        _good_delivery("USR-2", "hub"),
        _good_delivery("SEO-1", "chat", project_id="mansaodolago"),
        _good_delivery("USR-3", "telegram"),
        _good_delivery("USR-4", "hub"),
    ]
    report = evaluate_v0_proof(deliveries, now=NOW)
    assert report.current_streak == 5
    assert report.criterion_met is True
    assert report.missing == []
    assert set(report.channels_covered) == {"telegram", "hub", "chat"}
    assert "mansaodolago" in report.projects_involved
    # USR-279 T3: all 5 slots filled, oldest-first, carrying the evidence the panel displays.
    assert len(report.slots) == 5
    assert all(slot.filled for slot in report.slots)
    assert [slot.ticket_id for slot in report.slots] == ["USR-1", "USR-2", "SEO-1", "USR-3", "USR-4"]
    assert report.slots[2].project_id == "mansaodolago"
    assert report.slots[0].merge_sha == "a" * 40
    assert report.slots[0].smoke_passed is True
    assert report.slots[0].learning_pack_linked is True


def test_partial_streak_pads_leading_empty_slots_before_the_real_deliveries() -> None:
    deliveries = [_good_delivery("USR-1", "telegram"), _good_delivery("USR-2", "hub")]
    report = evaluate_v0_proof(deliveries, now=NOW)
    assert len(report.slots) == REQUIRED_STREAK
    assert [slot.filled for slot in report.slots] == [False, False, False, True, True]
    assert [slot.ticket_id for slot in report.slots[-2:]] == ["USR-1", "USR-2"]


def test_missing_item_disqualifies_and_resets_the_streak() -> None:
    bad = _good_delivery("USR-9", "telegram")
    bad.merge_sha = None  # no merge SHA: disqualifying
    deliveries = [
        _good_delivery("USR-1", "telegram"),
        _good_delivery("USR-2", "hub"),
        bad,
        _good_delivery("USR-3", "chat"),
        _good_delivery("USR-4", "telegram"),
    ]
    report = evaluate_v0_proof(deliveries, now=NOW)
    assert report.current_streak == 2
    assert report.qualified_deliveries[0].ticket_id == "USR-3"
    assert report.criterion_met is False


def test_in_progress_delivery_never_counts_as_green_even_with_other_fields_set() -> None:
    delivery = _good_delivery("USR-5", "telegram")
    delivery.in_progress = True
    report = evaluate_v0_proof([delivery], now=NOW)
    assert report.current_streak == 0
    assert report.qualified_deliveries == []


def test_streak_without_a_real_product_does_not_meet_the_criterion() -> None:
    deliveries = [_good_delivery(f"USR-{i}", ch) for i, ch in enumerate(["telegram", "hub", "chat", "telegram", "hub"])]
    report = evaluate_v0_proof(deliveries, now=NOW)
    assert report.current_streak == 5
    assert report.criterion_met is False
    assert any("produto real" in m for m in report.missing)


def test_streak_missing_a_channel_does_not_meet_the_criterion() -> None:
    deliveries = [_good_delivery(f"USR-{i}", "telegram", project_id="mansaodolago" if i == 0 else "darkfac") for i in range(5)]
    report = evaluate_v0_proof(deliveries, now=NOW)
    assert report.current_streak == 5
    assert report.criterion_met is False
    assert any("canal" in m for m in report.missing)


def test_more_than_one_owner_decision_disqualifies() -> None:
    delivery = _good_delivery("USR-6", "hub")
    delivery.owner_decisions_count = 2
    report = evaluate_v0_proof([delivery], now=NOW)
    assert report.qualified_deliveries == []


def test_any_owner_action_disqualifies() -> None:
    delivery = _good_delivery("USR-7", "chat")
    delivery.owner_actions_count = 1
    report = evaluate_v0_proof([delivery], now=NOW)
    assert report.qualified_deliveries == []


# ---------------------------------------------------------------------------
# collect_v0_proof_evidence: assembling evidence from the real stores
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    manual_dir = repo / "docs" / "manual"
    manual_dir.mkdir(parents=True)
    (manual_dir / "prova_v0.md").write_text("<!-- covers: x -->\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "docs(manual): prova_v0")
    return repo


def _seed_succeeded_run(store: SQLiteControlStore, run_id: str, pr_url: str, merge_sha: str) -> None:
    with store._connect() as conn:
        for stage in LINE_STAGES:
            if stage == "retrospective":
                continue
            if stage == "integration":
                refs = json.dumps([pr_url, merge_sha])
            elif stage == "build_deploy":
                refs = json.dumps([f"smoke:{run_id}:{merge_sha[:12]}"])
            else:
                refs = "[]"
            conn.execute(
                """
                INSERT OR REPLACE INTO jobs (
                    run_id, ticket_id, plan_version, stage, iteration, status,
                    role, required_capabilities, fencing_token, timeout_seconds,
                    retry_count, max_retries, actual_cost, output_refs, evidence_refs,
                    created_at, updated_at, ready_at
                ) VALUES (?, 'T', '1.0', ?, 0, 'succeeded', 'worker', '[]', 0, 1800, 0, 3, 0.0, ?, '[]',
                          '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')
                """,
                (run_id, stage, refs),
            )
        conn.commit()


def test_collector_assembles_a_qualifying_delivery_from_the_real_stores(tmp_path: Path, git_repo: Path) -> None:
    control = SQLiteControlStore(db_path=tmp_path / "control.db")
    demands = DemandsStore(path=tmp_path / "demands.json")
    actions = OwnerActionStore(tmp_path / "owner_actions.json", remote_ref=None)
    registry = ProjectRegistry(tmp_path / "projects.json")
    packs_dir = tmp_path / "learning_packs"
    packs_dir.mkdir()

    ticket = demands.save_ticket(
        UserTicket(
            id="USR-500",
            project_id="darkfac",
            title="Demanda real via Telegram",
            tags=["user-demand", "channel:telegram"],
            created_at=datetime(2026, 10, 30, tzinfo=UTC),
        )
    )
    sub = owner_intake.submit_ticket_to_line(ticket.id, demands_store=demands, store=control)
    assert sub.ok and sub.run_id

    merge_sha = _git(git_repo, "rev-parse", "HEAD")
    pr_url = "https://github.com/GCamposGit/DarkFactory/pull/9001"
    _seed_succeeded_run(control, sub.run_id, pr_url, merge_sha)
    (packs_dir / "USR-500.json").write_text("{}", encoding="utf-8")

    report = collect_v0_proof_evidence(
        store=control,
        demands_store=demands,
        owner_actions_store=actions,
        project_registry=registry,
        learning_packs_dir=packs_dir,
        repo_root=git_repo,
        now=NOW,
    )

    assert report.degraded_sources == []
    assert len(report.qualified_deliveries) == 1
    delivered = report.qualified_deliveries[0]
    assert delivered.ticket_id == "USR-500"
    assert delivered.qualifies is True, delivered.reasons
    assert delivered.channel == "telegram"


def test_collector_excludes_tickets_without_a_channel_tag(tmp_path: Path) -> None:
    control = SQLiteControlStore(db_path=tmp_path / "control.db")
    demands = DemandsStore(path=tmp_path / "demands.json")
    demands.save_ticket(
        UserTicket(id="USR-501", project_id="darkfac", title="Ticket interno sem canal", tags=["user-demand"])
    )

    report = collect_v0_proof_evidence(
        store=control,
        demands_store=demands,
        owner_actions_store=OwnerActionStore(tmp_path / "owner_actions.json", remote_ref=None),
        project_registry=ProjectRegistry(tmp_path / "projects.json"),
        learning_packs_dir=tmp_path / "learning_packs",
        now=NOW,
    )
    assert report.qualified_deliveries == []
    assert report.current_streak == 0


def test_collector_disqualifies_a_delivery_with_an_owner_action_outside_the_gates(tmp_path: Path, git_repo: Path) -> None:
    control = SQLiteControlStore(db_path=tmp_path / "control.db")
    demands = DemandsStore(path=tmp_path / "demands.json")
    actions = OwnerActionStore(tmp_path / "owner_actions.json", remote_ref=None)
    packs_dir = tmp_path / "learning_packs"
    packs_dir.mkdir()

    ticket = demands.save_ticket(
        UserTicket(
            id="USR-502",
            project_id="darkfac",
            title="Demanda via Hub com acao extra",
            tags=["user-demand", "channel:hub"],
            created_at=datetime(2026, 10, 30, tzinfo=UTC),
        )
    )
    run_created_at = datetime(2026, 10, 30, 0, tzinfo=UTC)
    sub = owner_intake.submit_ticket_to_line(ticket.id, demands_store=demands, store=control, now=run_created_at)
    merge_sha = _git(git_repo, "rev-parse", "HEAD")
    _seed_succeeded_run(control, sub.run_id, "https://github.com/x/y/pull/1", merge_sha)
    (packs_dir / "USR-502.json").write_text("{}", encoding="utf-8")

    actions.upsert(
        OwnerAction(
            id="OA-900",
            kind=ActionKind.ACTION,
            title="Configurar segredo manualmente",
            priority=ActionPriority.HIGH,
            blocks=["USR-502"],
            steps=[ActionStep(text="Abrir o portal e colar o token")],
            created_at=run_created_at,
        )
    )

    report = collect_v0_proof_evidence(
        store=control,
        demands_store=demands,
        owner_actions_store=actions,
        project_registry=ProjectRegistry(tmp_path / "projects.json"),
        learning_packs_dir=packs_dir,
        repo_root=git_repo,
        now=NOW,
    )
    assert report.qualified_deliveries == []


def test_collector_degrades_gracefully_when_a_source_is_unavailable(tmp_path: Path) -> None:
    class _BrokenDemandsStore:
        def list_tickets(self) -> list[UserTicket]:
            raise RuntimeError("postgres://user:secret@host/db is unreachable")

    report = collect_v0_proof_evidence(
        store=SQLiteControlStore(db_path=tmp_path / "control.db"),
        demands_store=_BrokenDemandsStore(),
        owner_actions_store=OwnerActionStore(tmp_path / "owner_actions.json", remote_ref=None),
        project_registry=ProjectRegistry(tmp_path / "projects.json"),
        now=NOW,
    )
    assert "demands_read_failed" in report.degraded_sources
    assert report.current_streak == 0
    assert all("secret" not in note for note in report.degraded_sources)


def test_save_v0_proof_report_is_idempotent(tmp_path: Path) -> None:
    report = evaluate_v0_proof([], now=NOW)
    out_dir = tmp_path / "reports"
    path1 = save_v0_proof_report(report, out_dir)
    mtime1 = path1.stat().st_mtime_ns
    path2 = save_v0_proof_report(report, out_dir)
    assert path1 == path2
    assert path1.read_text(encoding="utf-8") == report.model_dump_json(indent=2)
    # Same content: second write is a no-op (file left untouched).
    assert path2.stat().st_mtime_ns == mtime1


# ---------------------------------------------------------------------------
# Markdown report and CLI (--report): acceptance-probe integration (USR-279 T2)
# ---------------------------------------------------------------------------


def test_render_v0_proof_markdown_has_no_questions_and_states_what_is_missing() -> None:
    report = evaluate_v0_proof([], now=NOW)
    content = render_v0_proof_markdown(report)
    assert "?" not in content
    assert "EM ANDAMENTO" in content
    assert all(item in content for item in report.missing)


def test_render_v0_proof_markdown_reports_criterion_met() -> None:
    deliveries = [
        _good_delivery("USR-1", "telegram"),
        _good_delivery("USR-2", "hub"),
        _good_delivery("SEO-1", "chat", project_id="mansaodolago"),
        _good_delivery("USR-3", "telegram"),
        _good_delivery("USR-4", "hub"),
    ]
    report = evaluate_v0_proof(deliveries, now=NOW)
    content = render_v0_proof_markdown(report)
    assert "CUMPRIDO" in content
    assert "?" not in content
    assert "USR-4" in content


def test_save_v0_proof_markdown_is_idempotent(tmp_path: Path) -> None:
    report = evaluate_v0_proof([], now=NOW)
    out_dir = tmp_path / "reports"
    path1 = save_v0_proof_markdown(report, out_dir)
    mtime1 = path1.stat().st_mtime_ns
    path2 = save_v0_proof_markdown(report, out_dir)
    assert path1 == path2
    assert path2.stat().st_mtime_ns == mtime1
    assert path1.name == f"{NOW.date().isoformat()}.md"


def test_cli_report_flag_writes_json_and_md_and_exits_1_when_not_met(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = evaluate_v0_proof([], now=NOW)
    monkeypatch.setattr("core.line.v0_proof.collect_v0_proof_evidence", lambda: report)
    exit_code = v0_proof_main(["--report", "--out-dir", str(tmp_path)])
    assert exit_code == 1
    day = NOW.date().isoformat()
    assert (tmp_path / f"{day}.json").is_file()
    assert (tmp_path / f"{day}.md").is_file()


def test_cli_report_flag_exits_0_when_criterion_met(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deliveries = [
        _good_delivery("USR-1", "telegram"),
        _good_delivery("USR-2", "hub"),
        _good_delivery("SEO-1", "chat", project_id="mansaodolago"),
        _good_delivery("USR-3", "telegram"),
        _good_delivery("USR-4", "hub"),
    ]
    report = evaluate_v0_proof(deliveries, now=NOW)
    monkeypatch.setattr("core.line.v0_proof.collect_v0_proof_evidence", lambda: report)
    exit_code = v0_proof_main(["--report", "--out-dir", str(tmp_path)])
    assert exit_code == 0


def test_cli_without_report_flag_prints_status_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = evaluate_v0_proof([], now=NOW)
    monkeypatch.setattr("core.line.v0_proof.collect_v0_proof_evidence", lambda: report)
    exit_code = v0_proof_main(["--out-dir", str(tmp_path)])
    assert exit_code == 0
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# DarkHub /api/line/v0-proof + /live panel (USR-279 T3)
# ---------------------------------------------------------------------------


def test_api_v0_proof_returns_the_typed_report_with_five_slots(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from core.workflow.control_store import SQLiteControlStore
    from hub.backend.api import get_hub_service
    from hub.backend.main import app
    from tests.test_line_live import _make_service

    control_db = tmp_path / "control.db"
    SQLiteControlStore(db_path=control_db)  # creates the schema; no runs seeded
    service = _make_service(tmp_path, control_db)
    app.dependency_overrides[get_hub_service] = lambda: service
    try:
        response = TestClient(app).get("/api/line/v0-proof")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    report = V0ProofReport.model_validate(response.json())
    assert report.criterion_met is False
    assert len(report.slots) == REQUIRED_STREAK
    assert all(not slot.filled for slot in report.slots)


def test_api_v0_proof_degrades_to_200_when_the_control_store_is_unreachable() -> None:
    from fastapi.testclient import TestClient

    from hub.backend.api import get_hub_service
    from hub.backend.main import app

    class _BrokenService:
        def get_v0_proof(self) -> V0ProofReport:
            return evaluate_v0_proof([], now=NOW, degraded_sources=["control_store_unavailable"])

    app.dependency_overrides[get_hub_service] = lambda: _BrokenService()
    try:
        response = TestClient(app).get("/api/line/v0-proof")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    report = V0ProofReport.model_validate(response.json())
    assert report.degraded_sources == ["control_store_unavailable"]
    assert report.criterion_met is False


def test_hub_service_get_v0_proof_never_raises_when_the_control_store_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hub.backend.service import HubService

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "default_services.json").write_text("[]", encoding="utf-8")
    (data_dir / "default_prompts.json").write_text("[]", encoding="utf-8")
    service = HubService(
        data_dir=data_dir,
        usage_dir=tmp_path / "usage",
        roadmap_root=tmp_path,
        state_path=tmp_path / "state.json",
        orchestrator_path=tmp_path / "orchestrator.sqlite3",
        control_db_path=tmp_path / "missing" / "control.db",
        control_database_url="",
    )

    def _boom() -> None:
        raise RuntimeError("postgresql://darkfac:super-secret-pw@10.0.0.1/darkfac is unreachable")

    monkeypatch.setattr(service, "_line_control_store", _boom)
    monkeypatch.setattr(
        "core.line.v0_proof.collect_v0_proof_evidence",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    report = service.get_v0_proof()

    assert isinstance(report, V0ProofReport)
    assert report.current_streak == 0
    assert "super-secret-pw" not in report.model_dump_json()
    assert report.degraded_sources == ["v0_proof_collect_failed"]
