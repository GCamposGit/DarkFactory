"""Agent executor lifecycle managing start, resume, cancel, checkpoints and budget reservations.

Conforms to Section 2 and Section 3 of DEVELOPMENT_PLAN_2026-09-05:
- High-level interface: start / resume / cancel for tool-calling harnesses.
- TaskSpec, Run, Attempt, Checkpoint contracts with deterministic state transitions.
- Integrates with ExecutionBudgetManager to prevent budget/quota overruns.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from core.execution.budget import ExecutionBudgetManager
from core.execution.contracts import (
    AttemptOutcome,
    AttemptRecord,
    Budget,
    ReservationRecord,
    ReservationStatus,
    UnknownCostPolicy,
)
from core.execution.providers import ModelProvider, ProviderResponse
from core.execution.sandbox import PathContainment, ProcessSandbox


class ExecutionStatus(str, Enum):
    """Lifecycle state of an agent execution run."""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TaskSpec:
    """Delimited task specification submitted to the executor."""

    task_id: str
    objective: str
    allowed_paths: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    budget_ceiling: float = 1.0
    max_attempts: int = 3
    timeout_seconds: float = 60.0
    risk_class: str = "B"


@dataclass(frozen=True)
class Checkpoint:
    """Serializable recovery checkpoint for an execution run."""

    checkpoint_id: str
    run_id: str
    task_id: str
    step_index: int
    state_payload: dict[str, Any]
    current_sha: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class ExecutionRun:
    """State tracking for an active or completed execution run."""

    run_id: str
    task_id: str
    status: ExecutionStatus
    task_spec: TaskSpec
    created_at: datetime
    updated_at: datetime
    attempts: list[AttemptRecord] = field(default_factory=list)
    active_reservation: ReservationRecord | None = None
    latest_checkpoint: Checkpoint | None = None
    cancellation_reason: str | None = None


class AgentExecutor:
    """Supervisor-controlled executor coordinating models, sandbox and budgets."""

    def __init__(
        self,
        working_dir: Path | str,
        provider: ModelProvider,
        *,
        budget_manager: ExecutionBudgetManager | None = None,
    ) -> None:
        self.working_dir = Path(working_dir).resolve()
        self.provider = provider
        self.budget_manager = budget_manager or ExecutionBudgetManager()
        self._runs: dict[str, ExecutionRun] = {}

    def start(self, spec: TaskSpec) -> ExecutionRun:
        """Start a new isolated execution run under task spec limits."""
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC)

        # 1. Setup budget envelope for task if not present
        budget = self.budget_manager.get_budget(spec.task_id)
        if budget is None:
            budget = Budget(
                ceiling=spec.budget_ceiling,
                max_attempts=spec.max_attempts,
                unknown_cost_policy=UnknownCostPolicy.ESTIMATE,
            )
            self.budget_manager.register_budget(spec.task_id, budget)

        # 2. Reserve initial budget slice
        reservation_amount = round(spec.budget_ceiling / max(1, spec.max_attempts), 4)
        reservation = self.budget_manager.reserve(
            budget_id=spec.task_id,
            attempt_id=f"{run_id}_init",
            amount=reservation_amount,
        )

        run = ExecutionRun(
            run_id=run_id,
            task_id=spec.task_id,
            status=ExecutionStatus.RUNNING,
            task_spec=spec,
            created_at=now,
            updated_at=now,
            active_reservation=reservation,
        )
        self._runs[run_id] = run
        return run

    def resume(self, checkpoint: Checkpoint) -> ExecutionRun:
        """Resume an interrupted execution from a verified checkpoint."""
        run = self._runs.get(checkpoint.run_id)
        now = datetime.now(UTC)

        if run is None:
            # Reconstruct run from checkpoint
            spec = TaskSpec(
                task_id=checkpoint.task_id,
                objective=checkpoint.state_payload.get("objective", "Resumed task"),
                allowed_paths=checkpoint.state_payload.get("allowed_paths", []),
            )
            run = ExecutionRun(
                run_id=checkpoint.run_id,
                task_id=checkpoint.task_id,
                status=ExecutionStatus.RUNNING,
                task_spec=spec,
                created_at=checkpoint.timestamp,
                updated_at=now,
                latest_checkpoint=checkpoint,
            )
            self._runs[checkpoint.run_id] = run
        else:
            run.status = ExecutionStatus.RUNNING
            run.latest_checkpoint = checkpoint
            run.updated_at = now

        # Ensure active budget reservation
        if run.active_reservation is None or run.active_reservation.status != ReservationStatus.ACTIVE:
            res_amount = round(run.task_spec.budget_ceiling / max(1, run.task_spec.max_attempts), 4)
            run.active_reservation = self.budget_manager.reserve(
                budget_id=run.task_id,
                attempt_id=f"{run.run_id}_step_{checkpoint.step_index + 1}",
                amount=res_amount,
            )

        return run

    def cancel(self, run_id: str, reason: str = "Operator cancelled") -> ExecutionRun:
        """Cancel an in-flight execution run and release budget reservations."""
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(f"No execution run found for id '{run_id}'")

        now = datetime.now(UTC)
        run.status = ExecutionStatus.CANCELLED
        run.cancellation_reason = reason
        run.updated_at = now

        if run.active_reservation and run.active_reservation.status == ReservationStatus.ACTIVE:
            self.budget_manager.release(run.active_reservation.reservation_id)
            run.active_reservation = None

        return run

    def execute_step(
        self,
        run_id: str,
        step_prompt: str,
        *,
        model: str = "mock-model",
    ) -> AttemptRecord:
        """Execute a single bounded attempt step within budget and sandbox limits."""
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(f"No execution run found for id '{run_id}'")
        if run.status != ExecutionStatus.RUNNING:
            raise RuntimeError(f"Cannot execute step on run with status '{run.status.value}'")

        attempt_id = f"att_{uuid.uuid4().hex[:8]}"
        start_time = time.perf_counter()
        input_hash = hashlib.sha256(step_prompt.encode("utf-8")).hexdigest()

        # Check budget availability
        budget = self.budget_manager.get_budget(run.task_id)
        if budget is None or budget.is_expired():
            run.status = ExecutionStatus.FAILED
            attempt = AttemptRecord(
                attempt_id=attempt_id,
                input_artifact_hash=input_hash,
                outcome=AttemptOutcome.DEADLINE_EXCEEDED,
                timestamp=datetime.now(UTC),
            )
            run.attempts.append(attempt)
            return attempt

        # Execute inference call
        resp: ProviderResponse = self.provider.generate(
            step_prompt,
            model=model,
            unknown_cost_policy=budget.unknown_cost_policy,
        )

        latency = max(0.001, time.perf_counter() - start_time)
        output_hash = hashlib.sha256(resp.text.encode("utf-8")).hexdigest()

        attempt = AttemptRecord(
            attempt_id=attempt_id,
            invocation_id=f"inv_{uuid.uuid4().hex[:8]}",
            input_artifact_hash=input_hash,
            output_artifact_hash=output_hash,
            mode="live" if resp.is_measured else "simulated",
            tokens=resp.total_tokens,
            measured_cost=resp.measured_cost,
            estimated_cost=resp.estimated_cost,
            latency=latency,
            outcome=AttemptOutcome.SUCCEEDED,
            timestamp=datetime.now(UTC),
        )

        # Commit cost to reservation if active
        if run.active_reservation and run.active_reservation.status == ReservationStatus.ACTIVE:
            self.budget_manager.commit(
                run.active_reservation.reservation_id,
                attempt,
            )
            run.active_reservation = None
        run.attempts.append(attempt)
        run.updated_at = datetime.now(UTC)
        return attempt

    def get_run(self, run_id: str) -> ExecutionRun | None:
        return self._runs.get(run_id)
