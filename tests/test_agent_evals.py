"""Acceptance tests for DF-18 smoke/live agent evaluations."""

from __future__ import annotations

import json

import pytest

from core.execution.providers import MockModelProvider
from evals.graders import PatchApplicationError, apply_patch, grade_known_defect, grade_reference
from evals.runner import EvalMode, load_tasks, run_suite


def test_corpus_has_required_size_and_category_balance() -> None:
    tasks = load_tasks()
    assert len(tasks) == 24
    counts = {category.value: sum(task.category is category for task in tasks) for category in {task.category for task in tasks}}
    assert counts == {"bugfix": 8, "feature": 6, "refactor": 4, "security": 3, "recovery": 3}
    assert len({task.task_id for task in tasks}) == len(tasks)
    for task in tasks:
        assert task.fixture
        assert task.reference_patch.path in task.allowed_paths
        assert task.known_defect.path in task.allowed_paths
        assert task.acceptance_criteria
        assert task.resource_limits.timeout_seconds > 0


def test_smoke_accepts_reference_patches_and_rejects_known_defects() -> None:
    result = run_suite(load_tasks(), mode=EvalMode.SMOKE)

    assert result.passed
    assert result.task_count == 24
    assert result.passed_count == 24
    assert all(item.reference is not None and item.reference.passed for item in result.results)
    assert all(item.known_defect is not None and not item.known_defect.passed for item in result.results)


def test_real_mode_is_separate_and_uses_provider_output() -> None:
    task = load_tasks()[0]
    response = json.dumps(task.reference_patch.model_dump())
    provider = MockModelProvider(fixed_response=response)

    result = run_suite([task], mode=EvalMode.REAL, provider=provider, model="test-agent")

    assert result.mode is EvalMode.REAL
    assert result.passed
    assert result.results[0].reference is None
    assert result.results[0].known_defect is None
    assert result.results[0].candidate is not None
    assert result.results[0].candidate.passed
    assert provider.invocation_count == 1


def test_real_mode_requires_a_provider_and_malformed_output_fails_closed() -> None:
    task = load_tasks()[0]
    without_provider = run_suite([task], mode=EvalMode.REAL)
    assert not without_provider.passed
    assert "ModelProvider" in (without_provider.results[0].error or "")

    malformed = MockModelProvider(fixed_response="not-json")
    failed = run_suite([task], mode=EvalMode.REAL, provider=malformed)
    assert not failed.passed
    assert failed.results[0].candidate is None
    assert "JSON" in (failed.results[0].error or "") or "json" in (failed.results[0].error or "")


def test_grader_rejects_patch_outside_declared_allow_list(tmp_path) -> None:
    task = load_tasks()[0]
    tmp_path.joinpath("solution.py").write_text(task.fixture["solution.py"], encoding="utf-8")
    with pytest.raises(PatchApplicationError):
        apply_patch(task, task.reference_patch.model_copy(update={"path": "check.py"}), tmp_path)
