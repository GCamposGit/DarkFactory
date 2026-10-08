"""Isolated cloud worker for Dark Factory workflow step execution.

Governed by HF-03-04 / ADR-HF-001.
Maintains bounded concurrency (slots), executes deterministic steps,
and isolates task execution inside container boundaries.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import signal
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from pydantic import BaseModel, ConfigDict, Field

from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.orchestrator.cloud_artifacts import CloudArtifactStore
from core.paths import state_root
from core.workflow.control_contracts import (
    Claim,
    ExternalOperation,
    InvalidResultError,
    RuntimeOwner,
    StageContext,
    StageResult,
    StaleLeaseError,
)
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
# Two probe cadences, per harness (each probe is a real CLI call; a headless
# `claude -p ok` loads the full system prompt and costs subscription quota):
# - a DROPPED harness is re-probed every 600s, so a completed login (Codex
#   device-auth, a freshly set CLAUDE_CODE_OAUTH_TOKEN) takes effect fast;
# - a HEALTHY harness is only re-probed every 3600s, to detect expiry.
_CAPABILITY_PROBE_INTERVAL_SEC = 600.0
_HEALTHY_PROBE_INTERVAL_SEC = 3600.0
# Delay before a job this worker cannot serve (no authenticated harness) is retried.
_NO_HARNESS_RETRY_MINUTES = 10

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
        healthy_probe_interval_s: float = _HEALTHY_PROBE_INTERVAL_SEC,
        autodetect_tooling: bool = False,
        clock: Callable[[], float] = time.monotonic,
        on_capabilities_probed: Callable[[list[str]], None] | None = None,
        capability_detail_lookup: Callable[[str], str] | None = None,
        human_probe_interval_s: float = 600.0,
        request_loader: Callable[[Any, str], Any] | None = None,
        human_probe_runner: Callable[..., bool] | None = None,
        route_waiter: Any | None = None,
        grill_resender: Callable[[Any, str], bool] | None = None,
        workspace_sweep_interval_s: float = 6 * 3600.0,
        workspace_sweeper: Callable[..., list[str]] | None = None,
        artifact_sweep_interval_s: float = 6 * 3600.0,
        artifact_sweeper: Callable[..., Any] | None = None,
        orphan_run_sweep_interval_s: float = 300.0,
        orphan_run_sweeper: Callable[..., list[Any]] | None = None,
        parked_orphan_notifier: Callable[..., list[Any]] | None = None,
    ) -> None:
        self.worker_id = worker_id or os.environ.get("DARKFAC_WORKER_ID", "cloud-worker-1")
        self.max_slots = (
            max_slots
            if max_slots is not None
            else int(os.environ.get("DARKFAC_MAX_CONCURRENT_SLOTS", "2"))
        )
        self.database_url = database_url or os.environ.get("DARKFAC_HF02_DATABASE_URL")

        self._capability_prober = capability_prober
        # Injectable monotonic clock (tests) and a hook fired after every real
        # probe run with the resulting capabilities (used to kick off the
        # Codex login bootstrap when harness:codex is still missing).
        self._clock = clock
        self._on_capabilities_probed = on_capabilities_probed
        self._capability_detail_lookup = capability_detail_lookup
        self._autodetect_tooling = autodetect_tooling
        # `capability_probe_interval_s` is the cadence for DROPPED harnesses (and
        # the tick gate); healthy ones use `healthy_probe_interval_s`.
        self._capability_probe_interval_s = capability_probe_interval_s
        self._healthy_probe_interval_s = healthy_probe_interval_s
        self._last_capability_probe_monotonic: float | None = None
        # harness name -> (clock time of its last real probe, result)
        self._harness_probe_state: dict[str, tuple[float, bool]] = {}

        self._human_probe_interval_s = human_probe_interval_s
        self._last_human_probe_at: datetime | None = None
        self._request_loader = request_loader
        self._human_probe_runner = human_probe_runner
        self._route_waiter = route_waiter
        self._grill_resender = grill_resender
        # Disk retention (2026-10-06 VPS incident): periodically delete per-run workspaces of terminal runs.
        self._workspace_sweep_interval_s = workspace_sweep_interval_s
        self._workspace_sweeper = workspace_sweeper
        self._last_workspace_sweep_at: datetime | None = None
        # USR-148: same cadence for the `darkfac-artifacts` volume retention (core.orchestrator.artifact_retention).
        self._artifact_sweep_interval_s = artifact_sweep_interval_s
        self._artifact_sweeper = artifact_sweeper
        self._last_artifact_sweep_at: datetime | None = None
        # USR-142: active runs with no open job and no activity for 30 min get their successor scheduled
        # (or are parked on waiting_human) instead of sitting idle forever.
        self._orphan_run_sweep_interval_s = orphan_run_sweep_interval_s
        self._orphan_run_sweeper = orphan_run_sweeper
        self._parked_orphan_notifier = parked_orphan_notifier
        self._last_orphan_run_sweep_at: datetime | None = None

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
    def _filter_capabilities(
        capabilities: list[str],
        prober: Callable[[str], bool],
        detail_lookup: Callable[[str], str] | None = None,
    ) -> list[str]:
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
                    # `detail_lookup` (redacted probe detail: error kind + first
                    # ~300 chars) makes a dropped harness diagnosable from logs.
                    detail = ""
                    if detail_lookup is not None:
                        try:
                            detail = detail_lookup(harness_name)
                        except Exception:  # never let diagnostics break the filter
                            detail = ""
                    logger.warning(
                        "Dropping capability %s: auth probe failed%s", cap, f" ({detail})" if detail else ""
                    )
                    continue
            kept.append(cap)
        return kept

    def _agent_route_unavailable(self, stage: str) -> str | None:
        """Only for real autodetecting workers (explicit-caps test workers keep
        their exact behaviour): see `core.line.bindings.agent_route_unavailable`."""
        if not self._autodetect_tooling:
            return None
        from core.line.bindings import agent_route_unavailable

        return agent_route_unavailable(
            stage,
            self.capabilities,
            openrouter_configured=bool(os.environ.get("OPENROUTER_API_KEY")),
        )

    def _due_cached_prober(self, force_all: bool) -> Callable[[str], bool]:
        """Wrap the configured prober so each harness is only really probed when due.

        Never probed -> due. Dropped (last probe failed) -> due after
        `_capability_probe_interval_s`. Healthy -> due after
        `_healthy_probe_interval_s`. Otherwise the cached result is returned
        without running the CLI. `force_all` re-probes everything.
        """
        assert self._capability_prober is not None
        prober = self._capability_prober

        def probe(harness: str) -> bool:
            now = self._clock()
            state = self._harness_probe_state.get(harness)
            if state is not None and not force_all:
                last, healthy = state
                interval = self._healthy_probe_interval_s if healthy else self._capability_probe_interval_s
                if now - last < interval:
                    return healthy
            try:
                ok = bool(prober(harness))
            except Exception as exc:
                logger.warning("Capability prober raised for harness:%s; treating as dropped: %s", harness, exc)
                ok = False
            self._harness_probe_state[harness] = (now, ok)
            return ok

        return probe

    def _compute_capabilities(self, *, force_all: bool = False) -> list[str]:
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
            capabilities = self._filter_capabilities(
                capabilities, self._due_cached_prober(force_all), self._capability_detail_lookup
            )

        if self._autodetect_tooling and "harness:any" not in capabilities:
            if any(c.startswith("harness:") for c in capabilities):
                capabilities.append("harness:any")

        self._last_capability_probe_monotonic = self._clock()
        return capabilities

    def refresh_capabilities(self, *, force: bool = False) -> bool:
        """Recompute capabilities once per tick (`capability_probe_interval_s`).

        Only the harnesses that are *due* are actually probed: dropped ones
        every 600s, healthy ones every 3600s (see `_due_cached_prober`);
        `force=True` re-probes all. The `on_capabilities_probed` hook fires
        after every tick so the Codex login trigger keeps working.

        No-op (returns False) when autodetection/probing was never
        requested, so a worker started with an explicit `capabilities` list
        or without `autodetect_tooling`/`capability_prober` never re-probes.
        Returns True when capabilities actually changed.
        """
        if not self._autodetect_tooling and self._capability_prober is None:
            return False
        if not force:
            elapsed = self._clock() - (self._last_capability_probe_monotonic or 0.0)
            if elapsed < self._capability_probe_interval_s:
                return False
        new_caps = self._compute_capabilities(force_all=force)
        changed = new_caps != self.capabilities
        self.capabilities = new_caps
        self.notify_capabilities_probed()
        return changed

    def notify_capabilities_probed(self) -> None:
        """Fire the post-probe hook; it must never break the worker loop."""
        if self._on_capabilities_probed is None:
            return
        try:
            self._on_capabilities_probed(list(self.capabilities))
        except Exception as exc:
            logger.warning("Capability post-probe hook failed: %s", exc)

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
            artifacts_root = Path("/app/.factory/artifacts") if Path("/app").is_dir() else (state_root() / "artifacts")
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

    # ----------------------------------------------------------------
    # HF-27-08 item 4: owner-facing messages (b/c/d). (a) -- grill's own
    # waiting_human -- is intentionally not handled here; stage_grill sends
    # its single grill message itself.
    # ----------------------------------------------------------------

    def _notify_once(self, claim: Claim, kind: str, sender: Callable[[], bool]) -> None:
        """Send `sender()` at most once per `(job_key, kind)`, using `external_operations` as the guard.

        Recorded via `store.record_operation`, which requires an *active*
        claim/lease -- this must be called before `store.finish()` releases
        it. A replayed dispatch for the same job_key (e.g. after a crash and
        lease-expiry reclaim) sees the existing operation and skips resending.
        """
        key = f"notify:{kind}:{claim.job_key.canonical_key()}"
        try:
            if self.store.get_operation(key) is not None:
                return
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("get_operation(%s) failed; sending anyway: %s", key, exc)

        sent = False
        try:
            sent = bool(sender())
        except Exception as exc:
            logger.warning("Notification '%s' failed for %s: %s", kind, claim.job_key.canonical_key(), exc)

        if not sent:
            return
        try:
            self.store.record_operation(
                ExternalOperation(
                    operation_key=key,
                    request_digest=hashlib.sha256(key.encode("utf-8")).hexdigest(),
                    provider="notification",
                    status="succeeded",
                    observed_at=datetime.now(UTC).isoformat(),
                ),
                claim,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to record notification idempotency marker %s: %s", key, exc)

    def _run_total_cost(self, run_id: str) -> float:
        try:
            status = self.store.get_run_status(run_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("get_run_status(%s) failed while summing cost: %s", run_id, exc)
            return 0.0
        if not status:
            return 0.0
        return sum(float(job.get("actual_cost") or 0.0) for job in status.get("jobs", []))

    def _notify_stage_outcome(self, claim: Claim, stage_result: StageResult) -> None:
        stage = claim.job_key.stage
        if stage != "build_deploy":
            if stage_result.outcome == "failed":
                self._notify_once(claim, "failed", lambda: self._send_failure_summary(claim, stage_result))
            return

        if stage_result.outcome == "waiting_human":
            self._notify_once(claim, "human_request", lambda: self._send_commercial_acceptance_request(claim, stage_result))
        elif stage_result.outcome == "success":
            self._notify_once(claim, "delivered", lambda: self._send_delivered_message(claim, stage_result))
        elif stage_result.outcome == "failed":
            self._notify_once(claim, "failed", lambda: self._send_failure_summary(claim, stage_result))

    def _project_for(self, claim: Claim):
        from core.line.bindings import default_project_resolver

        return default_project_resolver()(claim.job_key.ticket_id)

    def _send_commercial_acceptance_request(self, claim: Claim, stage_result: StageResult) -> bool:
        from core.line.human import HumanRequest, request_human_help

        project = self._project_for(claim)
        if project is None:
            return False
        run_id = claim.job_key.run_id
        refs = self.store.get_latest_success_output_refs(run_id, exclude_stage="build_deploy")
        pr_url = refs[0] if len(refs) > 1 else ""
        sha = refs[-1] if refs else ""
        preview = f"\nPreview: https://{project.domain}" if project.domain else ""
        guide = (
            f"Revise o PR mergeado: {pr_url or '(url indisponivel)'}\n"
            f"SHA a ser publicado: {sha}"
            f"{preview}\n"
            f"Confirme o aceite comercial para liberar o deploy (botao cb:accept:{run_id})."
        )
        request = HumanRequest(kind="commercial_acceptance", run_id=run_id, blocking_stage="build_deploy", guide_md=guide)
        request_human_help(project, request)
        return True

    def _send_delivered_message(self, claim: Claim, stage_result: StageResult) -> bool:
        project = self._project_for(claim)
        run_id = claim.job_key.run_id
        refs = self.store.get_latest_success_output_refs(run_id, exclude_stage="build_deploy")
        pr_url = refs[0] if len(refs) > 1 else ""
        sha = refs[-1] if refs else ""
        total_cost = self._run_total_cost(run_id)
        assumptions = self._fetch_grill_assumptions(project, pr_url) if project else []
        lines = [
            f"Entregue: run {run_id}",
            f"PR: {pr_url or '(indisponivel)'}",
            f"URL: {pr_url or '(indisponivel)'}",
            f"SHA: {sha or '(indisponivel)'}",
            f"Custo total do run: ${total_cost:.4f}",
        ]
        if assumptions:
            lines.append("Premissas assumidas (GRILL):")
            lines.extend(f"- {a}" for a in assumptions)
        return self._send_owner_text("\n".join(lines))

    def _send_failure_summary(self, claim: Claim, stage_result: StageResult) -> bool:
        run_id = claim.job_key.run_id
        stage = claim.job_key.stage
        text = (
            f"Run {run_id} falhou no estagio '{stage}'.\n"
            f"Motivo: {stage_result.cause_code or '(sem detalhe)'}\n"
            "Proximo passo sugerido: revise o log acima; se for um erro transitorio, "
            "reenvie a demanda; se for um defeito de especificacao, corrija SPEC/tickets "
            "e reenvie."
        )
        return self._send_owner_text(text)

    @staticmethod
    def _fetch_grill_assumptions(project: Any, pr_url: str) -> list[str]:
        """Best-effort: the "## Grill (premissas)" section stage_integration folded into the PR body.

        `.darkfac/runs/<run_id>/GRILL.md` no longer exists on the branch by
        the time `build_deploy` succeeds (stripped before the merge that
        produced this SHA); this re-derives a short list from the merged
        PR's body instead. Returns `[]` on any failure -- this is cosmetic,
        never load-bearing.
        """
        if not pr_url:
            return []
        try:
            import json as _json
            import re
            import subprocess

            # HF-27-08 review item 7: `gh pr view` takes the full PR URL, not
            # a bare number extracted with the wrong cwd/repo context.
            proc = subprocess.run(
                ["gh", "pr", "view", pr_url, "--json", "body"],
                cwd=project.path or ".",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if proc.returncode != 0:
                return []

            body = _json.loads(proc.stdout or "{}").get("body", "")
            section = re.search(r"## Grill \(premissas\)\s*\n\n(.*?)(?:\n##|\Z)", body, re.DOTALL)
            if not section:
                return []
            return [line.lstrip("-* ").strip() for line in section.group(1).splitlines() if line.strip()]
        except Exception:
            return []

    def _send_owner_text(self, text: str) -> bool:
        from core.line.human import _default_sender

        sender = _default_sender()
        if sender is None:
            logger.info("No notification sender configured; owner message dropped: %s", text[:120])
            return False
        return sender(text)

    @staticmethod
    def _log_stage_outcome(claim: Claim, stage_result: StageResult) -> None:
        """WARNING with cause code + a ~300-char redacted snippet for every non-success stage.

        Before this the worker only logged "Job execution completed" for a stage that had failed, so
        a failed run was invisible from the container logs.
        """
        if stage_result.outcome == "success":
            return
        try:
            from core.line.diagnostics import worker_snippet

            snippet = worker_snippet(stage_result.cause_code)
        except Exception:  # pragma: no cover - logging must never break a job
            snippet = "(unavailable)"
        logger.warning(
            "Stage %s of run %s ended %s: %s",
            claim.job_key.stage, claim.job_key.run_id, stage_result.outcome, snippet,
        )

    def dispatch_claimed_job(self, claim: Claim, now: datetime | None = None) -> StepExecutionResult:
        """Execute a claimed stage via the line's HandlerRegistry, finish the job, and materialize successors.

        HF-27-08 item D: the old generic-prompt/`deterministic_mock` path
        and the `<stage>_deliverable.json` artifact-as-result are gone.
        `registry.dispatch(context)` calls the real `core/line/stage_*.py`
        handler (via `core.line.bindings.build_line_registry`); a stage
        with no real executor is `missing_handler` (fail-closed), never a
        synthetic success.

        Review item 6: an exception raised *inside* the handler is caught
        here and turned into a same-stage `retry(handler_error:<Type>)`
        (governed by `successors.MAX_SAME_STAGE_RETRIES`) instead of leaving
        the job `running` until its lease expires and gets reclaimed
        forever. `store.finish`/`materialize_result` are called with a
        *fresh* `datetime.now(UTC)`, taken after the handler returns/raises
        -- a handler can run for ~30 minutes, so the claim-time `now` (used
        only to build `StageContext`) would badly understate elapsed time.
        """
        stage = claim.job_key.stage
        run_id = claim.job_key.run_id

        def _execute_stage() -> dict[str, Any]:
            context = self._build_stage_context(claim)
            stop_heartbeat, heartbeat_thread = self._start_lease_heartbeat(claim)
            try:
                unavailable = self._agent_route_unavailable(stage)
                if unavailable is not None:
                    from core.line import routing
                    from core.line.route_wait import RouteWaiter, run_started_at_lookup
                    from core.projects.models import ProjectDescriptor

                    project = self._project_for(claim)
                    if project is None:
                        project = ProjectDescriptor(
                            id=claim.job_key.ticket_id,
                            repo_url="",
                            default_branch="main",
                        )
                    waiter = self._route_waiter or RouteWaiter(
                        run_started_at=run_started_at_lookup(self.store)
                    )
                    stage_result = waiter.no_route_result(
                        stage,
                        project,
                        claim.job_key.run_id,
                        host_caps=self.capabilities,
                        config=self._routing_config,
                        mode=routing.stage_mode(stage),
                    )
                else:
                    stage_result = self.registry.dispatch(context)
            except Exception as exc:
                logger.error(
                    "Handler for %s raised %s: %s; recording as retry(handler_error)",
                    claim.job_key.canonical_key(), type(exc).__name__, exc,
                )
                stage_result = StageResult(
                    outcome="retry",
                    cause_code=f"handler_error:{type(exc).__name__}: {exc}"[:500],
                )
            finally:
                stop_heartbeat.set()
                heartbeat_thread.join(timeout=2.0)

            self._log_stage_outcome(claim, stage_result)

            # HF-27-08 item 4: owner-facing messages, sent while the claim is
            # still active (record_operation needs it for the idempotency
            # guard) and BEFORE finish() releases it.
            try:
                self._notify_stage_outcome(claim, stage_result)
            except Exception as exc:  # pragma: no cover - defensive, must never block finish
                logger.warning("Owner notification failed for %s: %s", claim.job_key.canonical_key(), exc)

            finish_now = datetime.now(UTC)
            try:
                self.store.finish(claim, stage_result, now=finish_now)
            except (StaleLeaseError, InvalidResultError):
                raise
            except Exception as exc:
                # e.g. a column overflow on the store. Never leave the job
                # `running` until its lease expires: record a short failure
                # (full detail stays in this log) so the run terminates visibly.
                logger.error(
                    "store.finish failed for %s (%s: %s); recording short failure instead",
                    claim.job_key.canonical_key(), type(exc).__name__, exc,
                )
                stage_result = StageResult(outcome="failed", cause_code=f"finish_failed:{type(exc).__name__}"[:60])
                self.store.finish(claim, stage_result, now=datetime.now(UTC))
            materialize_result(claim.job_key, stage_result, self.store, now=finish_now)

            return {"stage_result": stage_result.model_dump()}

        return self.execute_step(run_id, stage, _execute_stage)

    def sweep_waiting_human_requests(self, now: datetime | None = None) -> list[str]:
        """Sweep waiting_human jobs, probe deterministic checks, and resume green non-grill requests (USR-105)."""
        effective_now = now or datetime.now(UTC)
        resumed_runs: list[str] = []
        try:
            waiting_jobs = self.store.list_waiting_jobs(statuses=("waiting_human",))
        except Exception as exc:
            logger.warning("Failed to list waiting jobs for human sweep: %s", exc)
            return []

        seen_runs: set[str] = set()
        for job_key in waiting_jobs:
            run_id = job_key.run_id
            if run_id in seen_runs:
                continue
            seen_runs.add(run_id)

            try:
                from core.line.bindings import default_project_resolver
                from core.line.human import load_request, resume_blocked_job, run_probe

                project = default_project_resolver()(job_key.ticket_id)
                if project is None:
                    continue

                if job_key.stage == "grill":
                    resender = self._grill_resender
                    if resender is None:
                        from core.line.stage_grill import resend_unnotified_grill

                        resender = resend_unnotified_grill
                    try:
                        resender(project, run_id)
                    except Exception as exc:
                        logger.warning("Error resending unnotified grill for run %s: %s", run_id, exc)

                loader = self._request_loader or load_request
                request = loader(project, run_id)
                if request is None:
                    continue

                if request.kind in ("grill", "commercial_acceptance"):
                    continue

                if not request.probe_cmd:
                    continue

                prober = self._human_probe_runner or run_probe
                try:
                    probe_ok = prober(request.probe_cmd, timeout_s=15)
                except TypeError:
                    probe_ok = prober(request.probe_cmd)

                if not probe_ok:
                    continue

                resumed = resume_blocked_job(
                    self.store, run_id, request.blocking_stage, now=effective_now
                )
                if resumed:
                    logger.info(
                        "Auto-resumed waiting job %s (run %s stage %s) after green probe",
                        job_key.canonical_key(),
                        run_id,
                        request.blocking_stage,
                    )
                    resumed_runs.append(run_id)
            except Exception as exc:
                logger.warning("Error during human probe sweep for run %s: %s", run_id, exc)

        return resumed_runs

    def sweep_orphan_runs(self, now: datetime | None = None) -> list[Any]:
        """Schedule the successor of (or park) `active` runs with no open job and no activity for 30 min (USR-142)."""
        sweeper = self._orphan_run_sweeper
        if sweeper is None:
            if os.environ.get("PYTEST_CURRENT_TEST"):
                return []  # tests must never touch run state through the sweeper unless they inject one
            from core.line.orphan_runs import sweep_orphan_runs

            sweeper = sweep_orphan_runs
        actions = sweeper(self.store, now=now or datetime.now(UTC))
        if actions:
            logger.info("Orphan run sweep repaired %d run(s): %s", len(actions), actions)
        try:
            actions = [*actions, *self._notify_parked_orphan_runs()]
        except Exception as exc:  # the parking above already happened; delivery is retried next sweep
            logger.warning("Parked orphan run notification failed: %s", exc)
        return actions

    def _notify_parked_orphan_runs(self) -> list[Any]:
        """Tell the owner, once per run, about runs parked on `orphan_run_no_successor` (USR-155)."""
        notifier = self._parked_orphan_notifier
        if notifier is None:
            if os.environ.get("PYTEST_CURRENT_TEST"):
                return []  # tests must never message the owner unless they inject a notifier
            from core.line.orphan_runs import notify_parked_orphan_runs as notifier
        return notifier(self.store, send=self._send_owner_text)

    def sweep_stale_workspaces(self, now: datetime | None = None) -> list[str]:
        """Delete workspaces of terminal runs older than the retention (default 3 days); never an active run.

        Retention is `DARKFAC_WORKSPACE_RETENTION_DAYS` (default 3; 0 disables the sweep).
        """
        try:
            retention_days = float(os.environ.get("DARKFAC_WORKSPACE_RETENTION_DAYS", "3"))
        except ValueError:
            retention_days = 3.0
        if retention_days <= 0:
            return []
        sweeper = self._workspace_sweeper
        if sweeper is None:
            if os.environ.get("PYTEST_CURRENT_TEST"):
                return []  # tests must never delete real workspaces unless they inject a sweeper
            from core.line.workspace import sweep_stale_workspaces

            sweeper = sweep_stale_workspaces
        protected = {key.rsplit(":", 1)[0] for key in list(self._active_tasks)}
        removed = sweeper(
            run_status=self.store.get_run_status,
            protected_run_ids=protected,
            terminal_max_age_days=retention_days,
            now=(now.timestamp() if now is not None else None),
        )
        if removed:
            logger.info("Workspace sweep removed %d stale run workspace(s): %s", len(removed), removed)
        return removed

    def sweep_stale_artifacts(self, now: datetime | None = None) -> list[str]:
        """Apply the `darkfac-artifacts` retention policy (USR-148); never touches an active/waiting run.

        Env: `DARKFAC_ARTIFACT_RETENTION_DAYS` (default 30; 0 disables), `DARKFAC_ARTIFACT_ORPHAN_RETENTION_DAYS`
        (60), `DARKFAC_ARTIFACT_RETENTION_DRY_RUN` (log only). Returns the removed (or would-be removed) ids.
        """
        from core.orchestrator import artifact_retention as retention

        config = retention.retention_config_from_env()
        if not config.enabled:
            return []
        sweeper = self._artifact_sweeper
        if sweeper is None:
            if os.environ.get("PYTEST_CURRENT_TEST"):
                return []  # tests must never delete real artifacts unless they inject a sweeper
            sweeper = retention.sweep_stale_artifacts
        protected = {key.rsplit(":", 1)[0] for key in list(self._active_tasks)}
        report = sweeper(
            root=self.artifact_store.root_dir,
            run_status=self.store.get_run_status,
            protected_run_ids=protected,
            config=config,
            now=(now.timestamp() if now is not None else None),
        )
        return list(getattr(report, "removed_ids", []) or [])

    def poll_and_execute_once(self, now: datetime | None = None) -> bool:
        """Attempt to claim one pending job and execute it.

        Returns True if a job was claimed and executed; False otherwise.
        """
        if self._draining:
            return False
        if len(self._active_tasks) >= self.max_slots:
            return False

        effective_now = now or datetime.now(UTC)
        if (
            self._last_human_probe_at is None
            or (effective_now - self._last_human_probe_at).total_seconds() >= self._human_probe_interval_s
        ):
            self._last_human_probe_at = effective_now
            try:
                self.sweep_waiting_human_requests(now=effective_now)
            except Exception as exc:
                logger.warning("Human probe sweep failed: %s", exc)

        if (
            self._last_workspace_sweep_at is None
            or (effective_now - self._last_workspace_sweep_at).total_seconds() >= self._workspace_sweep_interval_s
        ):
            self._last_workspace_sweep_at = effective_now
            try:
                self.sweep_stale_workspaces(now=effective_now)
            except Exception as exc:
                logger.warning("Workspace sweep failed: %s", exc)

        if (
            self._last_artifact_sweep_at is None
            or (effective_now - self._last_artifact_sweep_at).total_seconds() >= self._artifact_sweep_interval_s
        ):
            self._last_artifact_sweep_at = effective_now
            try:
                self.sweep_stale_artifacts(now=effective_now)
            except Exception as exc:
                logger.warning("Artifact retention sweep failed: %s", exc)

        if (
            self._last_orphan_run_sweep_at is None
            or (effective_now - self._last_orphan_run_sweep_at).total_seconds() >= self._orphan_run_sweep_interval_s
        ):
            self._last_orphan_run_sweep_at = effective_now
            try:
                self.sweep_orphan_runs(now=effective_now)
            except Exception as exc:
                logger.warning("Orphan run sweep failed: %s", exc)

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
    capability_detail_lookup: Callable[[str], str] | None = None
    try:
        from core.line.auth_bootstrap import last_probe_detail, probe_harness_auth

        capability_prober = probe_harness_auth
        capability_detail_lookup = last_probe_detail
    except Exception as exc:  # pragma: no cover - defensive, optional dependency
        logger.debug("Capability auth probing unavailable: %s", exc)

    # HF-27-08: real worker processes autodetect git/gh/node/python plus
    # harness:<x> on PATH (opt-in flag so unit tests constructing CloudWorker
    # directly keep their exact, environment-independent capability lists).
    login_hook: Callable[[list[str]], None] | None = None
    try:
        from core.harness.remote_worker import find_codex_binary
        from core.line.auth_bootstrap import CodexLoginTrigger

        trigger = CodexLoginTrigger()
        env_caps = [c.strip() for c in os.environ.get("DARKFAC_WORKER_CAPS", "").split(",")]
        codex_expected = "harness:codex" in env_caps or bool(find_codex_binary())

        def login_hook(caps: list[str]) -> None:
            if trigger.maybe_start(caps, codex_expected=codex_expected):
                logger.info("Started Codex device-auth login bootstrap (Telegram relay)")

    except Exception as exc:  # pragma: no cover - defensive, optional dependency
        logger.debug("Codex login bootstrap unavailable: %s", exc)

    worker = CloudWorker(
        capability_prober=capability_prober,
        autodetect_tooling=True,
        on_capabilities_probed=login_hook,
        capability_detail_lookup=capability_detail_lookup,
    )
    # Boot-time check (the constructor's probe ran before the hook existed to fire).
    worker.notify_capabilities_probed()

    poll_interval_sec = args.poll_interval
    if poll_interval_sec is None:
        priority_poll, _ = _priority_defaults(os.environ.get("DARKFAC_WORKER_PRIORITY"))
        poll_interval_sec = priority_poll if priority_poll is not None else 5.0

    return worker.run_forever(poll_interval_sec=poll_interval_sec)


if __name__ == "__main__":
    sys.exit(main())
