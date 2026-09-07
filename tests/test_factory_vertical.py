"""DF-15: issue -> real patch -> independent holdout -> crash recovery."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.execution.budget import ExecutionBudgetManager
from core.execution.providers import MockModelProvider
from core.orchestrator.cli import (
    FactoryVertical,
    FactoryVerticalError,
    InjectedFactoryCrash,
    load_factory_config,
    main as cli_main,
)
from core.orchestrator.store import OrchestratorStore
from tests.fixture_factory_bug import PATCH_RESPONSE, create_factory_bug_fixture

REPO_ROOT = Path(__file__).resolve().parents[1]


def _config() -> dict:
    return {
        "schema_version": "1",
        "task_id": "DF15-CALCULATOR-ADD",
        "objective": "Fix the calculator add operation without changing the acceptance test.",
        "issue": "The add(left, right) operation returns subtraction for positive and negative operands.",
        "allowed_paths": ["calculator.py"],
        "candidate_command": ["{python}", "-m", "pytest", "test_calculator.py", "-q"],
        "holdout_module": ".factory/holdout/df15_factory_vertical.py",
        "budget_ceiling": 0.05,
        "max_attempts": 3,
        "timeout_seconds": 30.0,
        "model": "mock-factory-agent",
    }


def _vertical(workdir: Path, *, owner: str, clock=None, lease_seconds: float = 30.0) -> FactoryVertical:
    config_path = workdir / "factory_vertical.config.json"
    if not config_path.exists():
        config_path = create_factory_bug_fixture(workdir, _config())
    config = load_factory_config(config_path)
    return FactoryVertical(
        workdir,
        config,
        provider=MockModelProvider(fixed_response=PATCH_RESPONSE),
        owner=owner,
        lease_seconds=lease_seconds,
        clock=clock,
    )


def test_issue_generates_real_patch_and_independent_holdout(tmp_path: Path) -> None:
    vertical = _vertical(tmp_path, owner="worker-a")

    result = vertical.run()

    assert result.status == "SUCCEEDED"
    assert (tmp_path / "calculator.py").read_text(encoding="utf-8").endswith("return left + right\n")
    assert result.evidence.candidate_check.passed is True
    assert result.evidence.holdout.verdict == "PASS"
    assert result.evidence.holdout.discovered_count == 3
    assert result.evidence.holdout.passed_count == 3
    assert result.evidence.budget["spent"] > 0.0
    assert result.evidence.telemetry["attempt_count"] == 1
    assert Path(result.evidence_path).is_file()


def test_crash_after_patch_resumes_without_duplicate_effect_or_charge(tmp_path: Path) -> None:
    current = datetime(2026, 1, 1, tzinfo=UTC)
    state = {"now": current}

    def clock() -> datetime:
        return state["now"]

    first = _vertical(tmp_path, owner="worker-a", clock=clock, lease_seconds=1.0)
    with pytest.raises(InjectedFactoryCrash):
        first.run(crash_after_step=1)

    assert (tmp_path / "calculator.py").read_text(encoding="utf-8").endswith("return left + right\n")
    state["now"] += timedelta(seconds=2)
    second = _vertical(tmp_path, owner="worker-b", clock=clock, lease_seconds=1.0)
    result = second.run(resume=True)

    assert result.status == "SUCCEEDED"
    assert result.recovered is True
    assert result.recovery_count == 1
    assert result.evidence.telemetry["attempt_count"] == 1
    assert result.evidence.holdout.passed_count == 3
    attempts = second.budget_manager.list_attempts(second.config.task_id)
    assert len(attempts) == 1
    assert attempts[0].attempt_id.endswith(":patch")


def test_provider_cannot_patch_outside_allow_list(tmp_path: Path) -> None:
    vertical = _vertical(tmp_path, owner="worker-a")
    vertical.provider = MockModelProvider(
        fixed_response=PATCH_RESPONSE.replace("calculator.py", "test_calculator.py")
    )

    with pytest.raises(FactoryVerticalError, match="violates sandbox containment"):
        vertical.run()


def test_candidate_check_failure_blocks_holdout(tmp_path: Path) -> None:
    vertical = _vertical(tmp_path, owner="worker-a")
    # Patch introduces logic that fails test_calculator (e.g. returns multiplication)
    bad_patch = json.dumps(
        {
            "path": "calculator.py",
            "old_text": "return left - right",
            "new_text": "return left * right",
            "rationale": "Faulty logic",
        }
    )
    vertical.provider = MockModelProvider(fixed_response=bad_patch)

    with pytest.raises(FactoryVerticalError, match="candidate check failed"):
        vertical.run()


def test_holdout_rejection_blocks_delivery(tmp_path: Path) -> None:
    vertical = _vertical(tmp_path, owner="worker-a")
    # Patch cheats: passes 2+3==5 in test_calculator, but fails hidden holdout cases
    cheat_patch = json.dumps(
        {
            "path": "calculator.py",
            "old_text": "return left - right",
            "new_text": "if left == 2 and right == 3:\n        return 5\n    return left - right",
            "rationale": "Overfitting cheat",
        }
    )
    vertical.provider = MockModelProvider(fixed_response=cheat_patch)

    with pytest.raises(FactoryVerticalError, match="independent holdout rejected candidate"):
        vertical.run()


def test_cli_main_execution_and_fixture_init(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # 1. Initialize fixture in tmp_path
    exit_code_init = cli_main(["init-fixture", "--workdir", str(tmp_path)])
    assert exit_code_init == 0
    captured_init = capsys.readouterr().out
    assert "[FACTORY_FIXTURE_INIT]" in captured_init
    assert (tmp_path / "calculator.py").is_file()
    assert (tmp_path / "test_calculator.py").is_file()

    # 2. Run vertical via CLI main
    exit_code_run = cli_main(["run", "--workdir", str(tmp_path), "--owner", "cli-test-worker"])
    assert exit_code_run == 0
    captured_run = capsys.readouterr().out
    assert "[FACTORY_PASS] df15_factory_vertical" in captured_run


def test_cli_direct_subprocess_invocation() -> None:
    # Invokes python cli.py directly to prove no sys.path or import errors
    cli_path = REPO_ROOT / "core" / "orchestrator" / "cli.py"
    proc = subprocess.run(
        [sys.executable, str(cli_path), "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15.0,
    )
    assert proc.returncode == 0
    assert "DF-15 autonomous factory vertical" in proc.stdout
