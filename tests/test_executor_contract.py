"""Unit and integration contract tests for DF-13: AgentExecutor, ProcessSandbox and Boundaries."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from core.execution.agent_executor import (
    AgentExecutor,
    Checkpoint,
    ExecutionStatus,
    TaskSpec,
)
from core.execution.budget import ExecutionBudgetManager
from core.execution.contracts import (
    AttemptOutcome,
    Budget,
    ReservationStatus,
    UnknownCostPolicy,
)
from core.execution.providers import MockModelProvider
from core.execution.sandbox import (
    NetworkContainment,
    PathContainment,
    ProcessSandbox,
    SandboxSecurityError,
    kill_process_tree,
)


@pytest.fixture
def temp_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "sandbox_ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "core").mkdir()
    (ws / "core" / "app.py").write_text("print('hello')", encoding="utf-8")
    (ws / "MISSION.md").write_text("# Mission", encoding="utf-8")
    return ws


def test_path_containment_allowed_and_escapes(temp_workspace: Path) -> None:
    containment = PathContainment(
        root_dir=temp_workspace,
        allowed_paths=["core/", "tests/"],
        protect_governance=True,
    )

    # 1. Allowed paths
    assert containment.is_path_allowed("core/app.py", for_write=True)
    assert containment.is_path_allowed("tests/test_foo.py", for_write=True)
    resolved = containment.assert_path_allowed("core/app.py")
    assert resolved.exists()

    # 2. Outside allowed paths
    assert not containment.is_path_allowed("docs/readme.md", for_write=True)
    with pytest.raises(SandboxSecurityError):
        containment.assert_path_allowed("docs/readme.md", for_write=True)

    # 3. Path traversal escape attempts
    assert not containment.is_path_allowed("../../windows/system32", for_write=False)
    with pytest.raises(SandboxSecurityError):
        containment.assert_path_allowed("../../windows/system32")

    # 4. Governance protection
    assert not containment.is_path_allowed("MISSION.md", for_write=True)
    with pytest.raises(SandboxSecurityError):
        containment.assert_path_allowed("MISSION.md", for_write=True)


def test_network_containment_rules() -> None:
    containment = NetworkContainment(
        allow_network=False,
        allow_loopback=True,
        allowed_hosts=["api.openrouter.ai"],
    )

    # Loopback allowed
    assert containment.is_destination_allowed("localhost")
    assert containment.is_destination_allowed("127.0.0.1")
    assert containment.is_destination_allowed("::1")
    containment.assert_destination_allowed("127.0.0.1")

    # Untrusted external hosts blocked
    assert not containment.is_destination_allowed("evil.com")
    with pytest.raises(SandboxSecurityError):
        containment.assert_destination_allowed("evil.com")

    # Strict mode without network blocks even listed hosts if allow_network=False
    assert not containment.is_destination_allowed("api.openrouter.ai")

    # With allow_network=True, allowed_hosts succeed
    enabled = NetworkContainment(
        allow_network=True,
        allowed_hosts=["api.openrouter.ai"],
    )
    assert enabled.is_destination_allowed("api.openrouter.ai")
    assert not enabled.is_destination_allowed("malicious.site")


def test_process_sandbox_timeout_and_tree_kill(temp_workspace: Path) -> None:
    sandbox = ProcessSandbox(working_dir=temp_workspace, default_timeout_seconds=0.5)

    # Quick fast command succeeds
    quick_script = "print('quick output')"
    if sys.platform != "win32":
        quick_script += "; import os; print(f'pgrp={os.getpgrp()}')"
    cmd_quick = [sys.executable, "-c", quick_script]
    res_quick = sandbox.run_command(cmd_quick, timeout_seconds=2.0)
    assert res_quick.exit_code == 0
    assert "quick output" in res_quick.stdout
    assert not res_quick.timed_out
    if sys.platform != "win32":
        child_group = int(
            next(line for line in res_quick.stdout.splitlines() if line.startswith("pgrp="))
            .split("=", 1)[1]
        )
        assert child_group != os.getpgrp()

    # Long running command that exceeds timeout gets killed
    cmd_sleep = [
        sys.executable,
        "-c",
        "import time; time.sleep(10)",
    ]
    res_sleep = sandbox.run_command(cmd_sleep, timeout_seconds=0.3)
    assert res_sleep.timed_out
    assert res_sleep.exit_code == -1


def test_executor_start_cancel_and_budget(temp_workspace: Path) -> None:
    provider = MockModelProvider()
    budget_mgr = ExecutionBudgetManager()
    executor = AgentExecutor(
        working_dir=temp_workspace,
        provider=provider,
        budget_manager=budget_mgr,
    )

    spec = TaskSpec(
        task_id="TASK-TEST-01",
        objective="Verify executor lifecycle",
        allowed_paths=["core/"],
        budget_ceiling=1.0,
        max_attempts=2,
    )

    # 1. Start execution
    run = executor.start(spec)
    assert run.status == ExecutionStatus.RUNNING
    assert run.active_reservation is not None
    assert run.active_reservation.amount == 0.5
    assert run.active_reservation.status == ReservationStatus.ACTIVE

    budget = budget_mgr.get_budget("TASK-TEST-01")
    assert budget.reserved == 0.5
    assert budget.available_amount == 0.5

    # 2. Cancel execution
    cancelled_run = executor.cancel(run.run_id, reason="Testing cancellation")
    assert cancelled_run.status == ExecutionStatus.CANCELLED
    assert cancelled_run.cancellation_reason == "Testing cancellation"
    assert cancelled_run.active_reservation is None

    # Released reservation restored available budget
    updated_budget = budget_mgr.get_budget("TASK-TEST-01")
    assert updated_budget.reserved == 0.0
    assert updated_budget.available_amount == 1.0


def test_executor_step_execution_and_resume_checkpoint(temp_workspace: Path) -> None:
    provider = MockModelProvider(fixed_response="Generated test solution code")
    budget_mgr = ExecutionBudgetManager()
    executor = AgentExecutor(
        working_dir=temp_workspace,
        provider=provider,
        budget_manager=budget_mgr,
    )

    spec = TaskSpec(
        task_id="TASK-TEST-02",
        objective="Run step and checkpoint",
        allowed_paths=["core/"],
        budget_ceiling=1.0,
        max_attempts=3,
    )

    run = executor.start(spec)

    # Execute single step
    attempt = executor.execute_step(run.run_id, "Write test code")
    assert attempt.outcome == AttemptOutcome.SUCCEEDED
    assert attempt.tokens > 0
    assert attempt.latency > 0.0
    assert attempt.measured_cost is not None
    assert len(run.attempts) == 1

    # Checkpoint creation
    checkpoint = Checkpoint(
        checkpoint_id="chk_01",
        run_id=run.run_id,
        task_id=spec.task_id,
        step_index=1,
        state_payload={"objective": spec.objective, "allowed_paths": spec.allowed_paths},
    )

    # Resume from checkpoint
    resumed_run = executor.resume(checkpoint)
    assert resumed_run.status == ExecutionStatus.RUNNING
    assert resumed_run.latest_checkpoint is not None
    assert resumed_run.latest_checkpoint.checkpoint_id == "chk_01"
    assert resumed_run.active_reservation is not None


def test_provider_inference_and_unknown_cost_policy() -> None:
    provider = MockModelProvider(simulate_unknown_cost=True)

    # Rejection under strict REJECT policy
    with pytest.raises(ValueError, match="unknown cost and policy is REJECT"):
        provider.generate("Test prompt", model="mock-model", unknown_cost_policy=UnknownCostPolicy.REJECT)

    # Success with estimated cost under ESTIMATE policy
    resp = provider.generate("Test prompt", model="mock-model", unknown_cost_policy=UnknownCostPolicy.ESTIMATE)
    assert not resp.is_measured
    assert resp.measured_cost is None
    assert resp.estimated_cost > 0.0
