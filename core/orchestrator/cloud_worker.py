"""Isolated cloud worker for Dark Factory workflow step execution.

Governed by HF-03-04 / ADR-HF-001.
Maintains bounded concurrency (slots), executes deterministic steps,
and isolates task execution inside container boundaries.
"""

from __future__ import annotations

import logging
import os
import shutil
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
from core.workflow.control_contracts import Claim, RuntimeOwner, StageContext
from core.workflow.handlers import HandlerRegistry
from core.workflow.successors import materialize_result

logger = logging.getLogger("darkfac.cloud_worker")

# HF-27-08: agent-CLI binaries the worker autodetects on PATH. A binary being
# present does not by itself grant the `harness:<name>` capability -- an
# (optional) auth prober must also pass, so the worker never publishes a
# capability it cannot actually honor.
_HARNESS_BINARIES: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "grok": "grok",
    "antigravity": "antigravity",
}
_TOOLING_BINARIES: tuple[str, ...] = ("git", "gh", "node", "python")
_CAPABILITY_PROBE_INTERVAL_SEC = 3600.0

DEFAULT_CAPABILITIES: tuple[str, ...] = (
    "grill_engine",
    "planner",
    "researcher",
    "environment_probe",
    "developer",
    "validator",
    "reviewer",
    "integrator",
    "deployer",
    "journey_tester",
    "memory_agent",
    "evaluator",
    "benchmarker",
)

# HF-27-09: worker-priority topology (VPS > Desktop > Notebook). The
# higher-priority worker polls faster and claims immediately; lower-priority
# workers poll slower and only claim jobs a higher-priority worker had a
# `ready_age_sec` window to grab first. Unset/unknown priority keeps prior
# behaviour (5s poll default in `main()`, no ready_age filter).
_PRIORITY_POLL_INTERVAL_SEC: dict[str, float] = {
    "primary": 2.0,
    "secondary": 10.0,
    "fallback": 20.0,
}
_PRIORITY_READY_AGE_SEC: dict[str, float] = {
    "primary": 0.0,
    "secondary": 30.0,
    "fallback": 30.0,
}


def _autodetect_tooling_caps() -> list[str]:
    """`git`/`gh`/`node`/`python` present on PATH, plus `harness:<x>` for each
    agent CLI binary found on PATH.

    Only whether the *binary* is discoverable, not whether it is
    authenticated -- `capability_prober` (applied afterwards, same as the
    pre-existing `DARKFAC_WORKER_CAPS` path) is what drops an unauthenticated
    `harness:x`. Opt-in via `CloudWorker(..., autodetect_tooling=True)` (set
    by `main()` for the real worker loop) so every existing capability unit
    test, which asserts an exact capability list, is unaffected.
    """
    caps: list[str] = [tool for tool in _TOOLING_BINARIES if shutil.which(tool)]
    for harness, binary in _HARNESS_BINARIES.items():
        if shutil.which(binary):
            caps.append(f"harness:{harness}")
    return caps


def _priority_defaults(priority: str | None) -> tuple[float | None, float]:
    """Return (poll_interval_sec, ready_age_sec) for a `DARKFAC_WORKER_PRIORITY` value.

    `poll_interval_sec` is None for an unset/unknown priority, so callers can
    tell "no opinion" apart from an explicit value.
    """
    key = (priority or "").strip().lower()
    return _PRIORITY_POLL_INTERVAL_SEC.get(key), _PRIORITY_READY_AGE_SEC.get(key, 0.0)


class WorkerSlotStatus(BaseModel):
    """Status of worker concurrency slots and allocation."""

    model_config = ConfigDict(frozen=True)

    worker_id: str
    max_slots: int
    allocated_slots: int
    available_slots: int
    is_saturated: bool
    is_draining: bool = False
    # HF-27-08 item D: published so a panel/heartbeat consumer can tell
    # "no worker with harness:claude" apart from "all workers saturated".
    capabilities: list[str] = Field(default_factory=list)


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
        ready_age_sec: float | None = None,
        capability_prober: Callable[[str], bool] | None = None,
        registry: HandlerRegistry | None = None,
        intake_service: Any = None,
        routing_config: Any = None,
        capability_probe_interval_s: float = _CAPABILITY_PROBE_INTERVAL_SEC,
        autodetect_tooling: bool = False,
    ) -> None:
        self.worker_id = worker_id or os.environ.get("DARKFAC_WORKER_ID", "cloud-worker-1")
        self.max_slots = (
            max_slots
            if max_slots is not None
            else int(os.environ.get("DARKFAC_MAX_CONCURRENT_SLOTS", "2"))
        )
        self.database_url = database_url or os.environ.get("DARKFAC_HF02_DATABASE_URL")

        self._capability_prober = capability_prober
        self._autodetect_tooling = autodetect_tooling
        self._capability_probe_interval_s = capability_probe_interval_s
        self._last_capability_probe_monotonic: float | None = None

        if capabilities is not None:
            self.capabilities = list(capabilities)
        else:
            self.capabilities = self._compute_capabilities()

        self._registry: HandlerRegistry | None = registry
        self._registry_caps: tuple[str, ...] = tuple(self.capabilities) if registry is not None else ()
        self._intake_service = intake_service
        self._routing_config = routing_config

        # HF-27-09: priority topology. Explicit `ready_age_sec` wins; then an
        # explicit env override; then the DARKFAC_WORKER_PRIORITY default;
        # unset/unknown priority keeps 0.0 (today's behaviour, unfiltered).
        if ready_age_sec is not None:
            self.ready_age_sec = ready_age_sec
        else:
            env_ready_age = os.environ.get("DARKFAC_WORKER_READY_AGE_SEC")
            if env_ready_age:
                self.ready_age_sec = float(env_ready_age)
            else:
                _, self.ready_age_sec = _priority_defaults(os.environ.get("DARKFAC_WORKER_PRIORITY"))

        self._store = store
        self._artifact_store = artifact_store
        self._active_tasks: dict[str, float] = {}
        self._draining = False
        self._running = False
        self._stop_event: threading.Event | None = None

    @staticmethod
    def _filter_capabilities(capabilities: list[str], prober: Callable[[str], bool]) -> list[str]:
        kept: list[str] = []
        for cap in capabilities:
            if cap.startswith("harness:"):
                harness_name = cap.split(":", 1)[1]
                try:
                    ok = prober(harness_name)
                except Exception as exc:
                    logger.warning("Capability prober raised for %s; dropping capability: %s", cap, exc)
                    ok = False
                if not ok:
                    logger.warning("Dropping capability %s: auth probe failed", cap)
                    continue
            kept.append(cap)
        return kept

    def _compute_capabilities(self) -> list[str]:
        """DARKFAC_WORKER_CAPS host caps on top of role caps, plus (opt-in,
        HF-27-08) autodetected tooling/harnesses, then the auth-probe filter.

        `harness:any` is appended only in the autodetect path, and only for
        whatever `harness:*` capability actually survived the auth-probe
        filter -- never a stale claim about a harness that was just dropped.
        """
        env_caps = [c.strip() for c in os.environ.get("DARKFAC_WORKER_CAPS", "").split(",") if c.strip()]
        capabilities = list(DEFAULT_CAPABILITIES) + [c for c in env_caps if c not in DEFAULT_CAPABILITIES]

        if self._autodetect_tooling:
            for cap in _autodetect_tooling_caps():
                if cap not in capabilities:
                    capabilities.append(cap)

        if self._capability_prober is not None:
            capabilities = self._filter_capabilities(capabilities, self._capability_prober)

        if self._autodetect_tooling and "harness:any" not in capabilities:
            if any(c.startswith("harness:") for c in capabilities):
                capabilities.append("harness:any")

        self._last_capability_probe_monotonic = time.monotonic()
        return capabilities

    def refresh_capabilities(self, *, force: bool = False) -> bool:
        """Re-run `_compute_capabilities()` if the probe interval has elapsed.

        No-op (returns False) when autodetection/probing was never
        requested, so a worker started with an explicit `capabilities` list
        or without `autodetect_tooling`/`capability_prober` never re-probes.
        Returns True when capabilities actually changed.
        """
        if not self._autodetect_tooling and self._capability_prober is None:
            return False
        if not force:
            elapsed = time.monotonic() - (self._last_capability_probe_monotonic or 0.0)
            if elapsed < self._capability_probe_interval_s:
                return False
        new_caps = self._compute_capabilities()
        changed = new_caps != self.capabilities
        self.capabilities = new_caps
        return changed

    @property
    def registry(self) -> HandlerRegistry:
        """Line stage handler registry (HF-27-08), rebuilt when capabilities change."""
        if self._registry is None or self._registry_caps != tuple(self.capabilities):
            from core.line.bindings import build_line_registry

            self._registry = build_line_registry(
                self.capabilities,
                store=self.store,
                routing_config=self._routing_config,
                intake_service=self.intake_service,
            )
            self._registry_caps = tuple(self.capabilities)
        return self._registry

    @property
    def intake_service(self) -> Any:
        """`AutonomousIntakeService` bound to this worker's store (for planning's milestone children)."""
        if self._intake_service is None:
            from core.demands.autonomous_intake import AutonomousIntakeService

            self._intake_service = AutonomousIntakeService(self.store)
        return self._intake_service

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
            capabilities=list(self.capabilities),
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

    def _build_stage_context(self, claim: Claim) -> StageContext:
        """Assemble a `StageContext` for `claim` (HF-27-08 item D).

        `input_refs` (D-f) comes from the most recently succeeded job of
        this run -- exactly the shape each `core/line/stage_*.py` handler's
        own docstring already documents (e.g. `ReleaseStageHandler` expects
        `[pr_url, merge_sha]`, `IntegrationStageHandler`'s own output_refs).
        The other `StageContext` fields (plan_ref/plan_digest/...) are line
        placeholders: the line's real state lives on the run's git branch
        (`core.line.workspace`), not in these HF-05 plan/candidate digests.
        """
        run_id = claim.job_key.run_id
        try:
            input_refs = self.store.get_latest_success_output_refs(run_id, exclude_stage=claim.job_key.stage)
        except Exception as exc:  # pragma: no cover - defensive, must never block dispatch
            logger.warning("Failed to resolve input_refs for run %s: %s", run_id, exc)
            input_refs = []
        return StageContext(
            claim=claim,
            plan_ref=f"plan://line/{run_id}",
            plan_digest="sha256:" + "0" * 64,
            config_version="1.0",
            environment_ref="env-local",
            identity=self.worker_id,
            route_ref=claim.route_ref,
            memory_version="1.0",
            input_refs=input_refs,
        )

    def _start_lease_heartbeat(self, claim: Claim) -> tuple[threading.Event, threading.Thread]:
        """Background thread renewing `claim`'s lease while a (possibly ~30min) handler runs."""
        stop_event = threading.Event()
        interval = max(5.0, getattr(self.store, "lease_duration_sec", 45) / 3.0)

        def _loop() -> None:
            while not stop_event.wait(interval):
                try:
                    self.store.heartbeat(claim, datetime.now(UTC))
                except Exception as exc:
                    logger.warning("Lease heartbeat failed for %s: %s", claim.lease_id, exc)
                    return

        thread = threading.Thread(target=_loop, name=f"lease-heartbeat-{claim.lease_id}", daemon=True)
        thread.start()
        return stop_event, thread

    def dispatch_claimed_job(self, claim: Claim, now: datetime | None = None) -> StepExecutionResult:
        """Execute a claimed stage via the line's HandlerRegistry, finish the job, and materialize successors.

        HF-27-08 item D: the old generic-prompt/`deterministic_mock` path
        and the `<stage>_deliverable.json` artifact-as-result are gone.
        `registry.dispatch(context)` calls the real `core/line/stage_*.py`
        handler (via `core.line.bindings.build_line_registry`); a stage
        with no real executor is `missing_handler` (fail-closed), never a
        synthetic success.
        """
        effective_now = now or datetime.now(UTC)
        stage = claim.job_key.stage
        run_id = claim.job_key.run_id

        def _execute_stage() -> dict[str, Any]:
            context = self._build_stage_context(claim)
            stop_heartbeat, heartbeat_thread = self._start_lease_heartbeat(claim)
            try:
                stage_result = self.registry.dispatch(context)
            finally:
                stop_heartbeat.set()
                heartbeat_thread.join(timeout=2.0)

            self.store.finish(claim, stage_result, now=effective_now)
            materialize_result(claim.job_key, stage_result, self.store, now=effective_now)

            return {"stage_result": stage_result.model_dump()}

        return self.execute_step(run_id, stage, _execute_stage)

    def poll_and_execute_once(self, now: datetime | None = None) -> bool:
        """Attempt to claim one pending job and execute it.

        Returns True if a job was claimed and executed; False otherwise.
        """
        if self._draining:
            return False
        if len(self._active_tasks) >= self.max_slots:
            return False

        effective_now = now or datetime.now(UTC)
        try:
            claim = self.store.claim(
                worker=self.worker_id,
                capabilities=self.capabilities,
                now=effective_now,
                ready_age_sec=self.ready_age_sec,
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
                    if self.refresh_capabilities():
                        logger.info("Worker %s capabilities refreshed: %s", self.worker_id, self.capabilities)
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
        default=None,
        help=(
            "Polling interval in seconds for the worker loop. Defaults to the "
            "DARKFAC_WORKER_PRIORITY preset (primary=2s, secondary=10s, "
            "fallback=20s) when that env var is set, else 5s."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.status:
        # Cheap inspection path: no autodetect/auth-probe subprocesses, so
        # `--status` stays fast even when real harness CLIs are installed
        # on the host (a full probe -- by design, per HF-27-09/HF-27-08 --
        # runs real subprocesses and can take tens of seconds per harness).
        worker = CloudWorker()
        status = worker.slot_status()
        print(status.model_dump_json(indent=2))
        return 0

    # HF-27-09: drop a `harness:x` capability whose auth probe fails instead
    # of publishing it and later failing the claim. Best-effort: any import
    # or probe failure here just skips capability filtering.
    capability_prober: Callable[[str], bool] | None = None
    try:
        from core.line.auth_bootstrap import probe_harness_auth

        capability_prober = probe_harness_auth
    except Exception as exc:  # pragma: no cover - defensive, optional dependency
        logger.debug("Capability auth probing unavailable: %s", exc)

    # HF-27-08: real worker processes autodetect git/gh/node/python plus
    # harness:<x> on PATH (opt-in flag so unit tests constructing CloudWorker
    # directly keep their exact, environment-independent capability lists).
    worker = CloudWorker(capability_prober=capability_prober, autodetect_tooling=True)

    poll_interval_sec = args.poll_interval
    if poll_interval_sec is None:
        priority_poll, _ = _priority_defaults(os.environ.get("DARKFAC_WORKER_PRIORITY"))
        poll_interval_sec = priority_poll if priority_poll is not None else 5.0

    return worker.run_forever(poll_interval_sec=poll_interval_sec)


if __name__ == "__main__":
    sys.exit(main())
