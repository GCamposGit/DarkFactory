"""Tests for deterministic retrospective stage and learning cycle closure (USR-91).

No network, no real git remote. Tests the learning loop:
1. Retrospective generates deterministic lessons from cause codes, iterations, and cost.
2. Lessons are appended to .darkfac/LESSONS.md and recorded in core.learning ledger.
3. Planning stage of the next run reads .darkfac/LESSONS.md and includes lessons in prompt.
4. Failures during retrospective never alter the run outcome (outcome remains success).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from core.learning.models import MistakeCategory
from core.learning.tracker import ContinuousLearningTracker
from core.line import bindings, retrospective, stage_grill, stage_planning, workspace as ws_mod
from core.line.retrospective import (
    RetrospectiveLesson,
    append_lessons_to_markdown_file,
    generate_lessons_from_summary,
    record_lessons_in_learning_tracker,
    record_retrospective_lessons,
)
from core.projects.models import ProjectDescriptor
from core.workflow.control_contracts import Claim, JobKey, StageContext, StageResult
from core.workflow.control_store import SQLiteControlStore
from tests.line.conftest import copy_bare_origin


def _create_ctx(stage: str = "retrospective", *, ticket_id: str = "acme", run_id: str = "run-retro-1") -> StageContext:
    jk = JobKey(run_id=run_id, ticket_id=ticket_id, plan_version="1.0", stage=stage, iteration=0)
    claim = Claim(
        job_key=jk,
        lease_id="lease-retro",
        owner="worker-1",
        fencing_token=1,
        expires_at=datetime.now(timezone.utc).isoformat(),
    )
    return StageContext(
        claim=claim,
        plan_ref="plan://line",
        plan_digest="a" * 64,
        config_version="1.0",
        environment_ref="env",
        identity="worker-1",
        route_ref="route",
        memory_version="1.0",
    )


# ---------------------------------------------------------------------------
# 1. Lesson generation tests
# ---------------------------------------------------------------------------


def test_generate_lessons_clean_run_yields_zero_lessons() -> None:
    summary = {
        "run_id": "run-clean",
        "final_outcome": "completed",
        "total_cost_usd": 0.05,
        "stages": {
            "development": {"max_iteration": 1, "last_status": "success"},
            "validation": {"max_iteration": 1, "last_status": "success"},
        },
    }
    lessons = generate_lessons_from_summary(summary, status={"jobs": []})
    assert lessons == []


def test_generate_lessons_from_cause_codes() -> None:
    summary = {
        "run_id": "run-fail",
        "final_outcome": "failed",
        "total_cost_usd": 0.12,
        "stages": {"development": {"max_iteration": 1, "last_status": "failed"}},
    }
    status = {
        "jobs": [
            {"stage": "development", "cause_code": "validate_exhausted"},
            {"stage": "integration", "cause_code": "base_red_detected"},
            {"stage": "review", "cause_code": "changes_required_by_reviewer"},
        ]
    }
    lessons = generate_lessons_from_summary(summary, status=status)
    assert 1 <= len(lessons) <= 3
    rules = [l.rule for l in lessons]
    assert any("validacao" in r.lower() or "testes" in r.lower() for r in rules)
    assert any("base_red" in r.lower() for r in rules)


def test_generate_lessons_from_high_iterations_and_cost() -> None:
    summary = {
        "run_id": "run-high-iter",
        "final_outcome": "completed",
        "total_cost_usd": 1.45,
        "stages": {
            "development": {"max_iteration": 3, "last_status": "success"},
        },
    }
    lessons = generate_lessons_from_summary(summary, status={"jobs": []})
    assert len(lessons) == 2
    cause_codes = [l.cause_code for l in lessons]
    assert "development_high_iterations" in cause_codes
    assert "high_cost" in cause_codes


# ---------------------------------------------------------------------------
# 2. Markdown rendering & file append tests
# ---------------------------------------------------------------------------


def test_append_lessons_to_markdown_file_and_read_lessons(tmp_path: Path) -> None:
    lessons_file = tmp_path / ".darkfac" / "LESSONS.md"
    lessons = [
        RetrospectiveLesson(
            rule="Planejar validacoes locais deterministicas.",
            category=MistakeCategory.TEST_REGRESSION,
            cause_code="validate_exhausted",
            stage="development",
            run_id="run-1",
        ),
        RetrospectiveLesson(
            rule="Evitar diffs longos no review.",
            category=MistakeCategory.ASSUMPTION_ERROR,
            cause_code="review_exhausted",
            stage="review",
            run_id="run-1",
        ),
    ]

    append_lessons_to_markdown_file(lessons_file, lessons)
    assert lessons_file.exists()

    content = stage_grill.read_lessons(tmp_path)
    assert "Planejar validacoes locais deterministicas." in content
    assert "Evitar diffs longos no review." in content

    # Second run appends more lessons without clobbering existing ones
    more_lessons = [
        RetrospectiveLesson(
            rule="Verificar saude de cota pre-execucao.",
            category=MistakeCategory.TOOL_MISUSE,
            cause_code="quota_critical",
            stage="line",
            run_id="run-2",
        )
    ]
    append_lessons_to_markdown_file(lessons_file, more_lessons)

    updated_content = stage_grill.read_lessons(tmp_path)
    assert "Planejar validacoes locais deterministicas." in updated_content
    assert "Verificar saude de cota pre-execucao." in updated_content


# ---------------------------------------------------------------------------
# 3. Learning ledger integration tests
# ---------------------------------------------------------------------------


def test_record_lessons_in_learning_tracker(tmp_path: Path) -> None:
    ledger_file = tmp_path / "learning" / "learning_ledger.json"
    tracker = ContinuousLearningTracker(ledger_file=ledger_file, session_id="test-session")

    lessons = [
        RetrospectiveLesson(
            rule="Decompor tickets para evitar timeout.",
            category=MistakeCategory.TIMEOUT,
            cause_code="timeout",
            stage="development",
            run_id="run-timeout-1",
        )
    ]

    record_lessons_in_learning_tracker(lessons, "run-timeout-1", tracker=tracker)

    assert len(tracker.ledger.mistakes) == 1
    mistake = tracker.ledger.mistakes[0]
    assert mistake.category == MistakeCategory.TIMEOUT
    assert mistake.preventative_rule == "Decompor tickets para evitar timeout."


# ---------------------------------------------------------------------------
# 4. RetrospectiveStageHandler integration & learning cycle closure
# ---------------------------------------------------------------------------


def test_retrospective_stage_handler_records_lessons_and_never_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "workspaces"))
    origin_dir = copy_bare_origin(tmp_path / "origin.git")
    project = ProjectDescriptor(id="acme", name="Acme", repo_url=str(origin_dir))

    store = SQLiteControlStore(":memory:")
    # Seed a failed run with validate_exhausted in control store
    run_id = "run-retro-test"
    now_iso = "2026-10-05T00:00:00Z"
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, project_id, demand_id, demand_version, runtime_owner, mode, status, "
            "plan_digest, config_version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, "acme", "dem-1", "1.0", "hf05_sqlite", "autonomous", "failed", "d", "1.0", now_iso, now_iso),
        )
        conn.execute(
            "INSERT INTO jobs (run_id, ticket_id, plan_version, stage, iteration, status, role, required_capabilities, "
            "fencing_token, timeout_seconds, retry_count, max_retries, cause_code, actual_cost, output_refs, evidence_refs, created_at, updated_at) "
            "VALUES (?, ?, '1.0', 'development', 2, 'failed', 'developer', '[\"git\"]', 1, 300, 0, 3, 'validate_exhausted', 0.25, '[]', '[]', '2026-10-05T00:01:00Z', '2026-10-05T00:04:00Z')",
            (run_id, "acme"),
        )

    handler = bindings.RetrospectiveStageHandler(
        store=store,
        project_resolver=lambda pid: project if pid == "acme" else None,
    )

    ctx = _create_ctx("retrospective", ticket_id="acme", run_id=run_id)
    result = handler.handle(ctx)

    # Outcome is always success
    assert result.outcome == "success"
    assert result.output_refs == [f"retrospective:{run_id}"]

    # Workspace worktree checkout now contains .darkfac/LESSONS.md
    ws = ws_mod.checkout(project, run_id)
    lessons_text = stage_grill.read_lessons(ws.path)
    assert "Garantir que os testes e comandos de validacao passem localmente" in lessons_text

    # Next run's planning sees the lesson in rendered prompt
    rendered_prompt = stage_grill.render_prompt(
        stage_grill.load_prompt("planning.md"),
        demand="Demanda seguinte",
        grill="Grill do seguinte",
        commands="setup: echo 1",
        lessons=lessons_text,
    )
    assert "## Licoes recentes da fabrica" in rendered_prompt
    assert "Garantir que os testes e comandos de validacao passem localmente" in rendered_prompt


def test_retrospective_stage_handler_safe_on_project_error() -> None:
    store = SQLiteControlStore(":memory:")
    # Unknown project will cause resolver to raise UnknownProjectError or return None
    handler = bindings.RetrospectiveStageHandler(
        store=store,
        project_resolver=lambda pid: None,
    )

    ctx = _create_ctx("retrospective", ticket_id="unknown_proj", run_id="run-unknown")
    result = handler.handle(ctx)

    # Must never block or fail the run
    assert result.outcome == "success"
    assert result.output_refs == ["retrospective:run-unknown"]
