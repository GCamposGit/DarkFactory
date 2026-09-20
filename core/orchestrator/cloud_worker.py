"""Isolated cloud worker for Dark Factory workflow step execution.

Governed by HF-03-04 / ADR-HF-001.
Maintains bounded concurrency (slots), executes deterministic steps,
and isolates task execution inside container boundaries.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from pydantic import BaseModel, ConfigDict, Field

from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.orchestrator.cloud_artifacts import CloudArtifactStore
from core.workflow.control_contracts import Claim, JobKey, RuntimeOwner, StageResult
from core.workflow.handlers import STANDARD_STAGES
from core.workflow.successors import STAGE_ROLES, materialize_result

logger = logging.getLogger("darkfac.cloud_worker")

class WorkerSlotStatus(BaseModel):
    """Status of worker concurrency slots and allocation."""

    model_config = ConfigDict(frozen=True)

    worker_id: str
    max_slots: int
    allocated_slots: int
    available_slots: int
    is_saturated: bool
    is_draining: bool = False
    bound_stages: list[str] = Field(default_factory=list)
    is_ready: bool = False


class StepExecutionResult(BaseModel):
    """Outcome of a single step executed by the worker."""

    model_config = ConfigDict(frozen=True)

    step_id: str
    workflow_id: str
    success: bool
    duration_ms: float
    output: Any = None
    error: str | None = None


class CloudWorker:
    """Headless cloud worker enforcing concurrency bounds and executing steps."""

    def __init__(
        self,
        worker_id: str | None = None,
        max_slots: int | None = None,
        database_url: str | None = None,
        capabilities: list[str] | None = None,
        store: PostgresControlStore | None = None,
        artifact_store: CloudArtifactStore | None = None,
        stage_executors: dict[str, Callable[[Claim], StageResult]] | None = None,
        evidence_verifier: Callable[[Claim, StageResult], bool] | None = None,
        heartbeat_interval_sec: float = 10.0,
    ) -> None:
        self.worker_id = worker_id or os.environ.get("DARKFAC_WORKER_ID", "cloud-worker-1")
        self.max_slots = (
            max_slots
            if max_slots is not None
            else int(os.environ.get("DARKFAC_MAX_CONCURRENT_SLOTS", "2"))
        )
        self.database_url = database_url or os.environ.get("DARKFAC_HF02_DATABASE_URL")
        self._stage_executors = dict(stage_executors or {})
        self._evidence_verifier = evidence_verifier
        if heartbeat_interval_sec <= 0:
            raise ValueError("heartbeat_interval_sec must be positive")
        self._heartbeat_interval_sec = heartbeat_interval_sec
        bound_capabilities = {
            STAGE_ROLES[stage] for stage in self._stage_executors if stage in STAGE_ROLES
        }
        if capabilities is not None and not set(capabilities) <= bound_capabilities:
            raise ValueError("Worker capabilities must have a bound stage executor")
        self.capabilities = (
            list(capabilities) if capabilities is not None else sorted(bound_capabilities)
        )
        self._store = store
        self._artifact_store = artifact_store
        self._active_tasks: dict[str, float] = {}
        self._draining = False
        self._running = False
        self._stop_event: threading.Event | None = None

    @property
    def store(self) -> PostgresControlStore:
        if self._store is None:
            self._store = PostgresControlStore(
                database_url=self.database_url,
                runtime_owner=RuntimeOwner.CLOUD_DBOS_POSTGRES.value,
                lease_duration_sec=300,
            )
        return self._store

    @property
    def artifact_store(self) -> CloudArtifactStore:
        if self._artifact_store is None:
            artifacts_root = Path("/app/.factory/artifacts") if Path("/app").is_dir() else Path(".factory/artifacts")
            self._artifact_store = CloudArtifactStore(root_dir=artifacts_root)
        return self._artifact_store

    def slot_status(self) -> WorkerSlotStatus:
        """Query current slot allocation and saturation state."""
        allocated = len(self._active_tasks)
        available = max(0, self.max_slots - allocated) if not self._draining else 0
        return WorkerSlotStatus(
            worker_id=self.worker_id,
            max_slots=self.max_slots,
            allocated_slots=allocated,
            available_slots=available,
            is_saturated=(allocated >= self.max_slots) or self._draining,
            is_draining=self._draining,
            bound_stages=sorted(self._stage_executors),
            is_ready=set(STANDARD_STAGES) <= self._stage_executors.keys()
            and self._evidence_verifier is not None,
        )

    def try_acquire_slot(self, task_id: str) -> bool:
        """Attempt to acquire an execution slot for a task.

        Returns True if acquired; False if worker is draining or saturated (backpressure).
        """
        if self._draining:
            logger.warning("Worker %s is draining: rejecting task %s", self.worker_id, task_id)
            return False
        if task_id in self._active_tasks:
            return True
        if len(self._active_tasks) >= self.max_slots:
            logger.warning("Worker %s saturated: rejecting task %s (backpressure)", self.worker_id, task_id)
            return False
        self._active_tasks[task_id] = time.monotonic()
        return True

    def release_slot(self, task_id: str) -> None:
        """Release slot previously acquired by a task."""
        self._active_tasks.pop(task_id, None)

    def drain(self, timeout_seconds: float = 30.0) -> bool:
        """Signal draining, reject new slot allocations, and wait for active tasks to finish.

        Returns True if all active tasks completed within timeout, False if timed out.
        """
        self._draining = True
        logger.info(
            "Worker %s entering drain mode (active_tasks=%d, timeout=%.1fs)",
            self.worker_id,
            len(self._active_tasks),
            timeout_seconds,
        )
        deadline = time.monotonic() + timeout_seconds
        poll_step = 0.05
        while self._active_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "Worker %s drain timed out after %.1fs with %d active tasks remaining: %s",
                    self.worker_id,
                    timeout_seconds,
                    len(self._active_tasks),
                    list(self._active_tasks.keys()),
                )
                return False
            time.sleep(min(poll_step, remaining))
        logger.info("Worker %s drain completed cleanly; all active tasks finished", self.worker_id)
        return True

    def dispatch_claimed_job(self, claim: Claim, now: datetime | None = None) -> StepExecutionResult:
        """Execute an explicitly bound stage and persist only its real result."""
        start_now = now or datetime.now(UTC)
        stage = claim.job_key.stage
        run_id = claim.job_key.run_id

        try:
            if datetime.fromisoformat(claim.expires_at.replace("Z", "+00:00")) <= start_now:
                return StepExecutionResult(
                    step_id=stage, workflow_id=run_id, success=False,
                    duration_ms=0.0, error="stale_lease",
                )
        except (TypeError, ValueError):
            return StepExecutionResult(
                step_id=stage, workflow_id=run_id, success=False,
                duration_ms=0.0, error="invalid_lease_expiry",
            )

        def _execute_stage() -> dict[str, Any]:
            executor = self._stage_executors.get(stage)
            if executor is None:
                stage_result = StageResult(outcome="failed", cause_code="missing_stage_executor")
            else:
                heartbeat_stop = threading.Event()
                heartbeat_errors: list[str] = []

                def keep_lease() -> None:
                    while not heartbeat_stop.wait(self._heartbeat_interval_sec):
                        try:
                            self.store.heartbeat(claim, datetime.now(UTC))
                        except Exception as exc:
                            heartbeat_errors.append(type(exc).__name__)
                            return

                heartbeat_thread = threading.Thread(
                    target=keep_lease, name="cloud-worker-lease", daemon=True
                )
                heartbeat_thread.start()
                try:
                    stage_result = executor(claim)
                except Exception as exc:
                    logger.error("Stage executor failed (%s)", type(exc).__name__)
                    stage_result = StageResult(outcome="failed", cause_code="stage_executor_error")
                finally:
                    heartbeat_stop.set()
                    heartbeat_thread.join(timeout=self._heartbeat_interval_sec + 1.0)
                if heartbeat_errors or heartbeat_thread.is_alive():
                    raise RuntimeError("Lease heartbeat failed; refusing stage result")
            if not isinstance(stage_result, StageResult):
                raise TypeError(f"Executor for {stage} did not return StageResult")
            if stage_result.outcome == "success":
                refs = [*stage_result.output_refs, *stage_result.evidence_refs]
                if not stage_result.evidence_refs or any(
                    ref.startswith("ref://") or "deterministic_mock" in ref
                    for ref in refs
                ) or self._evidence_verifier is None:
                    stage_result = StageResult(
                        outcome="failed", cause_code="untrusted_stage_evidence"
                    )
                else:
                    try:
                        verified = self._evidence_verifier(claim, stage_result)
                    except Exception as exc:
                        logger.error("Stage evidence verification failed (%s)", type(exc).__name__)
                        verified = False
                    if not verified:
                        stage_result = StageResult(
                            outcome="failed", cause_code="untrusted_stage_evidence"
                        )

            finish_now = datetime.now(UTC)
            self.store.finish(claim, stage_result, now=finish_now)
            materialize_result(claim.job_key, stage_result, self.store, now=finish_now)

            return {
                "stage_result": stage_result.model_dump(),
            }

        execution = self.execute_step(run_id, stage, _execute_stage)
        if execution.success and execution.output is not None:
            outcome = execution.output["stage_result"]["outcome"]
            if outcome != "success":
                return execution.model_copy(update={
                    "success": False,
                    "error": execution.output["stage_result"].get("cause_code") or outcome,
                })
        return execution

    def poll_and_execute_once(self, now: datetime | None = None) -> bool:
        """Attempt to claim one pending job and execute it.

        Returns True if a job was claimed and executed; False otherwise.
        """
        if self._draining:
            return False
        if not self._stage_executors or self._evidence_verifier is None:
            return False
        if len(self._active_tasks) >= self.max_slots:
            return False

        effective_now = now or datetime.now(UTC)
        try:
            claim = self.store.claim(
                worker=self.worker_id,
                capabilities=self.capabilities,
                now=effective_now,
            )
        except Exception as exc:
            logger.warning("Error querying queue for claims: %s", exc)
            return False

        if claim is None:
            return False

        task_key = f"{claim.job_key.run_id}:{claim.job_key.stage}"
        logger.info(
            "Claimed job %s (lease=%s, fencing=%d) on worker %s",
            claim.job_key.canonical_key(),
            claim.lease_id,
            claim.fencing_token,
            self.worker_id,
        )
        res = self.dispatch_claimed_job(claim, now=effective_now)
        if not res.success:
            logger.error("Job execution failed for %s: %s", task_key, res.error)
        else:
            logger.info("Job execution completed for %s in %.2fms", task_key, res.duration_ms)
        return True

    def run_forever(
        self,
        stop_event: threading.Event | None = None,
        poll_interval_sec: float = 2.0,
    ) -> int:
        """Continuous loop with SIGTERM and SIGINT interception that drains on shutdown."""
        if stop_event is None:
            stop_event = threading.Event()
        self._stop_event = stop_event
        self._running = True

        def _signal_handler(signum: int, frame: Any) -> None:
            logger.info("Signal %s received; initiating worker drain and shutdown", signum)
            stop_event.set()

        orig_sigint = None
        orig_sigterm = None
        try:
            orig_sigint = signal.signal(signal.SIGINT, _signal_handler)
        except (ValueError, AttributeError):
            pass

        try:
            orig_sigterm = signal.signal(signal.SIGTERM, _signal_handler)
        except (ValueError, AttributeError):
            pass

        logger.info("Cloud worker %s running loop (poll_interval=%.1fs)", self.worker_id, poll_interval_sec)
        try:
            while not stop_event.is_set():
                try:
                    executed = self.poll_and_execute_once()
                    if executed:
                        # Process immediately next stage if available
                        continue
                except Exception as exc:
                    logger.warning("Transient error in worker poll loop: %s", exc)
                stop_event.wait(timeout=poll_interval_sec)
        finally:
            logger.info("Shutdown initiated for worker %s; draining with 30s timeout...", self.worker_id)
            self.drain(timeout_seconds=30.0)
            self._running = False
            if orig_sigint is not None:
                try:
                    signal.signal(signal.SIGINT, orig_sigint)
                except (ValueError, AttributeError):
                    pass
            if orig_sigterm is not None:
                try:
                    signal.signal(signal.SIGTERM, orig_sigterm)
                except (ValueError, AttributeError):
                    pass
            logger.info("Cloud worker %s terminated cleanly", self.worker_id)
        return 0

    def execute_step(
        self,
        workflow_id: str,
        step_id: str,
        step_callable: Callable[[], Any],
    ) -> StepExecutionResult:
        """Execute a step within an acquired slot and capture execution metrics."""
        task_key = f"{workflow_id}:{step_id}"
        if not self.try_acquire_slot(task_key):
            return StepExecutionResult(
                step_id=step_id,
                workflow_id=workflow_id,
                success=False,
                duration_ms=0.0,
                error=f"Worker {self.worker_id} saturated or draining (max_slots={self.max_slots})",
            )

        start = time.monotonic()
        try:
            output = step_callable()
            duration = (time.monotonic() - start) * 1000.0
            return StepExecutionResult(
                step_id=step_id,
                workflow_id=workflow_id,
                success=True,
                duration_ms=duration,
                output=output,
                error=None,
            )
        except Exception as exc:
            duration = (time.monotonic() - start) * 1000.0
            logger.error("Step execution error in %s: %s", task_key, exc)
            return StepExecutionResult(
                step_id=step_id,
                workflow_id=workflow_id,
                success=False,
                duration_ms=duration,
                output=None,
                error=str(exc),
            )
        finally:
            self.release_slot(task_key)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Dark Factory Cloud Worker")
    parser.add_argument(
        "--status",
        action="store_true",
        help="Inspect and output WorkerSlotStatus in JSON and exit without starting daemon loop",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="Polling interval in seconds for the worker loop",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    worker = CloudWorker()

    if args.status:
        status = worker.slot_status()
        print(status.model_dump_json(indent=2))
        return 0 if status.is_ready else 2

    return worker.run_forever(poll_interval_sec=args.poll_interval)


if __name__ == "__main__":
    sys.exit(main())
