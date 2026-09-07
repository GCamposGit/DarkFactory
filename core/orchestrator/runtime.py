"""Small recoverable execution runtime built on :mod:`orchestrator.store`."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from core.orchestrator.store import (
    LeaseClaim,
    OrchestratorStore,
    RunRecord,
    RunStatus,
)


RuntimeStep = Callable[["RuntimeContext"], Any]


@dataclass(frozen=True)
class RuntimeContext:
    """Read-only view supplied to one step of a recoverable run."""

    task_id: str
    run_id: str
    step_index: int
    checkpoint: dict[str, Any]
    lease: LeaseClaim

    @property
    def outputs(self) -> dict[str, Any]:
        """Return outputs already committed by earlier steps."""
        outputs = self.checkpoint.get("outputs", {})
        return deepcopy(outputs) if isinstance(outputs, dict) else {}


class RuntimeResult(BaseModel):
    """Terminal result returned after all runtime steps are committed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    status: RunStatus
    step_index: int = Field(ge=0)
    checkpoint: dict[str, Any]
    result: Any = None
    recovered: bool = False
    recovery_count: int = Field(default=0, ge=0)


class OrchestratorRuntime:
    """Execute a finite sequence of idempotent, checkpointable steps.

    The runtime intentionally does not catch arbitrary step exceptions. A
    process crash or an exception before the next checkpoint leaves the run in
    ``RUNNING`` with its last durable checkpoint. A later owner can reclaim
    the expired lease and continue at that checkpoint. Side effects inside a
    step must therefore be idempotent or keyed by the run/step identity.
    """

    def __init__(
        self,
        store: OrchestratorStore,
        *,
        owner: str,
        lease_seconds: float = 30.0,
    ) -> None:
        normalized_owner = owner.strip()
        if not normalized_owner:
            raise ValueError("owner must not be blank")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.store = store
        self.owner = normalized_owner
        self.lease_seconds = float(lease_seconds)

    @staticmethod
    def _steps(steps: Sequence[RuntimeStep]) -> tuple[RuntimeStep, ...]:
        normalized = tuple(steps)
        if any(not callable(step) for step in normalized):
            raise TypeError("every runtime step must be callable")
        return normalized

    @staticmethod
    def _checkpoint_outputs(run: RunRecord) -> dict[str, Any]:
        outputs = run.checkpoint.get("outputs", {})
        if not isinstance(outputs, dict):
            raise ValueError("persisted checkpoint outputs must be an object")
        return deepcopy(outputs)

    def run(
        self,
        task_id: str,
        steps: Sequence[RuntimeStep],
        *,
        run_id: str | None = None,
    ) -> RuntimeResult:
        """Start or recover ``task_id`` and execute its remaining steps.

        ``OrchestratorStore.claim`` selects a fresh run when none is active,
        or atomically reclaims the existing run after its lease expires. The
        same method therefore covers both first execution and post-crash
        recovery without a second, racy discovery/claim operation.
        """
        normalized_steps = self._steps(steps)
        claim = self.store.claim(
            task_id,
            owner=self.owner,
            lease_seconds=self.lease_seconds,
            run_id=run_id,
        )
        run = self.store.get_run(claim.run_id)
        outputs = self._checkpoint_outputs(run)

        for step_index in range(run.step_index, len(normalized_steps)):
            # Renewal before and after user code makes the lease boundary
            # explicit. If the worker was paused beyond the lease, checkpoint
            # and completion fail closed with StaleLeaseError.
            claim = self.store.renew(claim, lease_seconds=self.lease_seconds)
            context = RuntimeContext(
                task_id=task_id,
                run_id=claim.run_id,
                step_index=step_index,
                checkpoint=deepcopy(run.checkpoint),
                lease=claim,
            )
            value = normalized_steps[step_index](context)
            claim = self.store.renew(claim, lease_seconds=self.lease_seconds)
            outputs[str(step_index)] = value
            checkpoint = dict(run.checkpoint)
            checkpoint["outputs"] = outputs
            checkpoint["last_step_index"] = step_index
            run = self.store.save_checkpoint(
                claim,
                checkpoint,
                step_index=step_index + 1,
            )

        completed = self.store.complete(claim, result=outputs)
        return RuntimeResult(
            task_id=completed.task_id,
            run_id=completed.run_id,
            status=completed.status,
            step_index=completed.step_index,
            checkpoint=completed.checkpoint,
            result=completed.result,
            recovered=claim.recovered,
            recovery_count=completed.recovery_count,
        )

    def start(
        self,
        task_id: str,
        steps: Sequence[RuntimeStep],
    ) -> RuntimeResult:
        """Start a task; an existing active run is recovered by the store."""
        return self.run(task_id, steps)

    def resume(
        self,
        task_id: str,
        steps: Sequence[RuntimeStep],
        *,
        run_id: str | None = None,
    ) -> RuntimeResult:
        """Resume the active run for a task, optionally pinning its run id."""
        active = self.store.get_active_run(task_id)
        selected_run_id = run_id or (active.run_id if active is not None else None)
        if selected_run_id is None:
            raise ValueError(f"task {task_id!r} has no recoverable run")
        return self.run(task_id, steps, run_id=selected_run_id)


# Compatibility names for callers that refer to the component generically.
Runtime = OrchestratorRuntime
RecoverableRuntime = OrchestratorRuntime


__all__ = [
    "OrchestratorRuntime",
    "RecoverableRuntime",
    "Runtime",
    "RuntimeContext",
    "RuntimeResult",
    "RuntimeStep",
]
