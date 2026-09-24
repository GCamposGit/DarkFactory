"""Tests for USR-13: Execution of tests with specialized sub-agents and distilled reporting.

Reachability Contract:
python -m pytest tests/test_implementar_execu_o_de_te.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.harness.test_subagent import (
    DistilledTestReport,
    ExecutionTier,
    FailedTestItem,
    HarnessRunnerSpec,
    HarnessType,
    TestExecutionInstruction,
    TestScope,
    TestSubagentEngine,
    generate_subagent_prompt,
    get_harness_test_runner_spec,
)
from hub.backend.main import app


def test_test_subagent_models_contract():
    """Verify that Pydantic models enforce strict typing and defaults."""
    instr = TestExecutionInstruction(
        target="tests/test_foo.py",
        scope=TestScope.FILE,
        timeout_seconds=60,
        fail_fast=True,
    )
    assert instr.target == "tests/test_foo.py"
    assert instr.scope == TestScope.FILE
    assert instr.timeout_seconds == 60
    assert instr.fail_fast is True

    failure = FailedTestItem(
        test_id="tests/test_foo.py::test_bar",
        file="tests/test_foo.py",
        line=42,
        error_type="AssertionError",
        error_message="assert 1 == 2",
        snippet="> assert 1 == 2\nE AssertionError",
    )
    assert failure.test_id == "tests/test_foo.py::test_bar"
    assert failure.line == 42

    report = DistilledTestReport(
        verdict="FAILED",
        success=False,
        exit_code=1,
        duration_seconds=2.5,
        total_discovered=10,
        passed_count=9,
        failed_count=1,
        skipped_count=0,
        failures=[failure],
        concise_summary="1 failed, 9 passed in 2.50s",
        agent_feedback="Fix assertion in tests/test_foo.py:42: assert 1 == 2",
    )
    assert report.verdict == "FAILED"
    assert report.failed_count == 1
    assert len(report.failures) == 1
    assert "tests/test_foo.py:42" in report.agent_feedback


def test_parse_pytest_success_output():
    """Verify that successful pytest output is correctly parsed into a clean summary."""
    sample_output = """
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.1.1, pluggy-1.6.0
cachedir: .pytest_cache
rootdir: C:\\dev\\DarkFac
collecting ... collected 15 items

tests/test_one.py::test_a PASSED [  6%]
tests/test_one.py::test_b PASSED [ 13%]
tests/test_two.py::test_c PASSED [100%]

======================== 15 passed, 1 skipped in 1.45s ========================
"""
    engine = TestSubagentEngine()
    report = engine.parse_output(sample_output, exit_code=0, duration_seconds=1.45)

    assert report.success is True
    assert report.verdict == "PASSED"
    assert report.passed_count == 15
    assert report.skipped_count == 1
    assert report.failed_count == 0
    assert report.total_discovered == 16
    assert len(report.failures) == 0
    assert "15 passed" in report.concise_summary
    assert "All tests passed successfully" in report.agent_feedback


def test_parse_pytest_failure_output():
    """Verify that failing pytest output isolates relevant errors without log bloat."""
    sample_output = """
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.1.1
rootdir: C:\\dev\\DarkFac
collecting ... collected 5 items

tests/test_demo.py::test_ok PASSED [ 20%]
tests/test_demo.py::test_fail FAILED [ 40%]
tests/test_demo.py::test_other PASSED [100%]

================================== FAILURES ===================================
__________________________________ test_fail __________________________________

    def test_fail():
        val = calculate(10)
>       assert val == 25
E       AssertionError: assert 20 == 25

tests\\test_demo.py:84: AssertionError
=========================== short test summary info ===========================
FAILED tests/test_demo.py::test_fail - AssertionError: assert 20 == 25
========================= 1 failed, 4 passed in 0.82s =========================
"""
    engine = TestSubagentEngine()
    report = engine.parse_output(sample_output, exit_code=1, duration_seconds=0.82)

    assert report.success is False
    assert report.verdict == "FAILED"
    assert report.passed_count == 4
    assert report.failed_count == 1
    assert len(report.failures) == 1

    failure = report.failures[0]
    assert "test_fail" in failure.test_id
    assert failure.line == 84
    assert "AssertionError" in failure.error_type
    assert "20 == 25" in failure.error_message
    assert ">       assert val == 25" in failure.snippet

    # Verify agent feedback points directly to the actionable file and line
    assert "tests\\test_demo.py:84" in report.agent_feedback or "tests/test_demo.py:84" in report.agent_feedback
    assert "assert 20 == 25" in report.agent_feedback


def test_log_isolation_to_disk(tmp_path: Path):
    """Verify that verbose raw logs are saved to disk and not returned in context."""
    engine = TestSubagentEngine(log_dir=tmp_path)
    instruction = TestExecutionInstruction(target="tests/test_mock.py", scope=TestScope.FILE)

    raw_verbose_output = "VERBOSE DEBUG LOG LINE\n" * 500 + "\n=== 1 passed in 0.10s ===\n"

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=raw_verbose_output,
            stderr="",
        )
        report = engine.execute(instruction)

        assert report.success is True
        assert report.raw_log_path is not None
        log_file = Path(report.raw_log_path)
        assert log_file.exists()
        assert "VERBOSE DEBUG LOG LINE" in log_file.read_text(encoding="utf-8")

        # The report summary and feedback must NOT contain the 500 lines of verbose log
        assert "VERBOSE DEBUG LOG LINE" not in report.concise_summary
        assert "VERBOSE DEBUG LOG LINE" not in report.agent_feedback


def test_generate_subagent_prompt():
    """Verify that the subagent prompt provides explicit instructions and expected format."""
    instr = TestExecutionInstruction(
        target="tests/test_auth.py",
        scope=TestScope.FILE,
        timeout_seconds=90,
        fail_fast=True,
    )
    prompt = generate_subagent_prompt(instr)

    assert "tests/test_auth.py" in prompt
    assert "Specialized Test Runner" in prompt
    assert "DistilledTestReport" in prompt
    assert "90" in prompt
    assert "fail_fast" in prompt or "-x" in prompt


def test_rest_api_run_tests_endpoint():
    """Verify POST /api/harness/run-tests endpoint returns DistilledTestReport."""
    client = TestClient(app)

    # Use dry-run or mock to ensure fast and isolated test execution
    fake_report = DistilledTestReport(
        verdict="PASSED",
        success=True,
        exit_code=0,
        duration_seconds=0.5,
        total_discovered=5,
        passed_count=5,
        failed_count=0,
        skipped_count=0,
        failures=[],
        concise_summary="5 passed in 0.50s",
        agent_feedback="All tests passed successfully.",
    )

    with patch.object(TestSubagentEngine, "execute", return_value=fake_report):
        resp = client.post("/api/harness/run-tests", json={
            "target": "tests/test_sample.py",
            "scope": "file",
            "timeout_seconds": 60,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["verdict"] == "PASSED"
        assert data["success"] is True
        assert data["passed_count"] == 5
        assert data["concise_summary"] == "5 passed in 0.50s"


def test_non_goals_preservation():
    """Verify that existing runner.py and pytest configuration remain untouched and functional."""
    from core.harness.runner import load_config, PROJECT_ROOT
    from core.harness.models import HarnessConfig

    config_path = PROJECT_ROOT / "harness.config.json"
    assert config_path.exists()
    config, _ = load_config(config_path)
    assert isinstance(config, HarnessConfig)
    assert any(step.name.startswith("unit_and_integration_tests") for step in config.steps)


def test_harness_test_runner_specs():
    """Verify optimal model and reasoning effort selection across all harnesses."""
    # 1. Antigravity
    agy_spec = get_harness_test_runner_spec(HarnessType.ANTIGRAVITY)
    assert agy_spec.harness == HarnessType.ANTIGRAVITY
    assert agy_spec.model_id == "flash_lite"
    assert agy_spec.execution_tier == ExecutionTier.LIGHT_HARNESS

    # 2. OpenAI / Codex: Default is Luna low for fast test execution
    codex_spec = get_harness_test_runner_spec(HarnessType.CODEX)
    assert codex_spec.harness == HarnessType.CODEX
    assert codex_spec.model_id == "openai/gpt-5-6-luna-high"
    assert codex_spec.reasoning_effort == "low"
    assert codex_spec.execution_tier == ExecutionTier.LIGHT_HARNESS

    # 2b. OpenAI / Codex: Deep RCA escalates to Luna max
    codex_rca_spec = get_harness_test_runner_spec(HarnessType.CODEX, deep_rca=True)
    assert codex_rca_spec.model_id == "openai/gpt-5-6-luna-high"
    assert codex_rca_spec.reasoning_effort == "max"
    assert codex_rca_spec.execution_tier == ExecutionTier.DEEP_DIAGNOSTIC

    # 3. Grok / Grok Builder: Grok 4.6 with low reasoning effort
    grok_spec = get_harness_test_runner_spec(HarnessType.GROK)
    assert grok_spec.harness == HarnessType.GROK
    assert grok_spec.model_id == "xai/grok-4.6"
    assert grok_spec.reasoning_effort == "low"

    # 4. Claude Code: Claude 3.5 Haiku as standard test runner subagent
    claude_spec = get_harness_test_runner_spec(HarnessType.CLAUDE_CODE)
    assert claude_spec.harness == HarnessType.CLAUDE_CODE
    assert claude_spec.model_id == "anthropic/claude-3.5-haiku"
    assert claude_spec.execution_tier == ExecutionTier.LIGHT_HARNESS

    # 5. Local-first preference rule ($0 cost)
    local_spec = get_harness_test_runner_spec(HarnessType.ANTIGRAVITY, prefer_local=True)
    assert local_spec.execution_tier == ExecutionTier.LOCAL_ZERO_COST
    assert "qwen-code-fast" in local_spec.model_id


def test_generate_subagent_prompt_multi_harness():
    """Verify prompts generated for specific harness environments."""
    instr = TestExecutionInstruction(target="tests/test_harness.py", scope=TestScope.FILE)

    codex_prompt = generate_subagent_prompt(instr, harness=HarnessType.CODEX, deep_rca=True)
    assert "CODEX" in codex_prompt
    assert "openai/gpt-5-6-luna-high" in codex_prompt
    assert "max" in codex_prompt

    grok_prompt = generate_subagent_prompt(instr, harness=HarnessType.GROK)
    assert "GROK" in grok_prompt
    assert "xai/grok-4.6" in grok_prompt

    claude_prompt = generate_subagent_prompt(instr, harness=HarnessType.CLAUDE_CODE)
    assert "CLAUDE_CODE" in claude_prompt
    assert "claude-3.5-haiku" in claude_prompt

