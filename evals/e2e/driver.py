"""Driver side of the E2E evaluation: how a case reaches the autonomous line (USR-160).

``LineRunner`` is the only thing the evaluated system ever sees.  It receives an
:class:`~evals.e2e.corpus.AgentView` (case id + brief) and nothing else: no
``E2ECase``, no spec, no journey, no recovery description.  Faults arrive as a
bare :class:`FaultAction` chosen by the evaluator-side orchestrator.

``ControlStoreLineRunner`` submits through the same public intake the line
already uses (``AutonomousIntakeService.accept``) into a ``ControlStore`` that
the *caller* injects.  It never opens a store by itself and never reads a
database URL from the environment, so a test or a dry run cannot reach the
production line by accident.
"""

from __future__ import annotations

import importlib
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from evals.e2e.corpus import AgentView
from evals.e2e.models import EventKind
from evals.e2e.oracle import OracleDriver

logger = logging.getLogger(__name__)

E2E_CHANNEL = "e2e-eval"
E2E_POLICY_REF = "darkfac://line/e2e-eval/v1"
DEFAULT_PROJECT_ID = "e2e-eval"
_BAD_JOB_STATUSES = frozenset({"retry", "replan", "failed", "cancelled"})
_PROGRESS_JOB_STATUSES = frozenset({"running", "succeeded", "retry", "replan", "waiting_human"})


class RunPhase(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {RunPhase.DELIVERED, RunPhase.FAILED, RunPhase.CANCELLED}


class FaultAction(str, Enum):
    """Evaluator-chosen perturbation, deliberately free of any case content."""

    KILL_WORKER = "kill_worker"
    CUT_NETWORK = "cut_network"
    CANCEL_RUN = "cancel_run"
    INJECT_MERGE_CONFLICT = "inject_merge_conflict"
    FAIL_GATE_ONCE = "fail_gate_once"
    CORRUPT_STATE = "corrupt_state"


class RunnerEvent(BaseModel):
    """An observation made by the runner; the orchestrator stamps time and order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EventKind
    detail: str = Field(default="", max_length=500)


class RunSnapshot(BaseModel):
    """Result of one poll: current phase, total cost so far and events seen since the last poll."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: RunPhase
    cost_usd: float = Field(default=0.0, ge=0)
    events: tuple[RunnerEvent, ...] = ()


@runtime_checkable
class LineRunner(Protocol):
    """Executes one case against the autonomous line.  Sees only :class:`AgentView`."""

    def submit_case(self, view: AgentView) -> str:
        """Hand the brief to the line; return an opaque ``run_ref``."""
        ...

    def poll(self, run_ref: str) -> RunSnapshot:
        """Current phase, cost and the events observed since the previous poll."""
        ...

    def apply_fault(self, run_ref: str, action: FaultAction) -> bool:
        """Apply ``action``; ``False`` means this runner cannot (the fault was NOT exercised)."""
        ...

    def cancel(self, run_ref: str) -> None:
        """Stop the run (used on timeout); must not raise for an already finished run."""
        ...

    def product_driver(self, run_ref: str) -> OracleDriver:
        """A driver bound to the delivered product of a ``DELIVERED`` run."""
        ...


# --------------------------------------------------------------------------- #
# Control store backed runner                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class _RunState:
    view: AgentView
    base_external_id: str
    attempt: int
    current_run_id: str
    run_ids: list[str]
    prior_cost: float = 0.0
    emitted: set[tuple[str, str, int]] = field(default_factory=set)
    fault_applied: bool = False
    baseline_retries: int = 0
    baseline_bad: int = 0
    detected_hint: str | None = None
    detected: bool = False
    recovered: bool = False
    succeeded_at_detection: int = 0
    pending_events: list[RunnerEvent] = field(default_factory=list)


class ControlStoreLineRunner:
    """Submits cases through ``AutonomousIntakeService`` into an injected ``ControlStore``.

    Progress, human requests, detection and recovery are derived from the run's
    jobs.  Faults that need real infrastructure (kill a worker, cut the network,
    ...) are caller supplied ``fault_hooks``; without a hook ``apply_fault``
    returns ``False`` and the orchestrator records the fault as not exercised.
    ``CANCEL_RUN`` is built in: it cancels the current run and resubmits the same
    brief as the next attempt, exactly what the line's owner retry does.
    """

    def __init__(
        self,
        store: Any,
        *,
        driver_factory: Callable[[str], OracleDriver],
        project_id: str = DEFAULT_PROJECT_ID,
        session_id: str | None = None,
        fault_hooks: Mapping[FaultAction, Callable[[str], None]] | None = None,
        now: Callable[[], datetime] | None = None,
        allow_remote_store: bool = False,
    ) -> None:
        if store is None:
            raise ValueError("an explicit ControlStore is required (the runner never opens one by itself)")
        if getattr(store, "mock_mode", False):
            raise ValueError("refusing a control store in mock mode: no worker would ever see the run")
        if "postgres" in type(store).__name__.lower() and not allow_remote_store:
            raise ValueError("refusing a Postgres control store without allow_remote_store=True")
        self._store = store
        self._driver_factory = driver_factory
        self._project_id = project_id
        self._session = session_id or uuid.uuid4().hex[:10]
        self._hooks = dict(fault_hooks or {})
        self._now = now or (lambda: datetime.now(UTC))
        self._runs: dict[str, _RunState] = {}

    # -- submission ---------------------------------------------------------

    def _command(self, state_view: AgentView, external_id: str, attempt: int) -> Any:
        from core.workflow.control_contracts import IntakeCommand

        payload: dict[str, Any] = {
            "title": state_view.case_id,
            "problem": state_view.brief,
            "journey": [state_view.brief],
            "non_goals": [],
            "criteria": [],
        }
        if attempt > 1:
            payload["attempt"] = attempt
        return IntakeCommand(
            project_id=self._project_id,
            channel=E2E_CHANNEL,
            external_id=external_id,
            mode="autonomous",
            policy_ref=E2E_POLICY_REF,
            payload=payload,
        )

    def _accept(self, view: AgentView, external_id: str, attempt: int) -> str:
        from core.demands.autonomous_intake import AutonomousIntakeService

        service = AutonomousIntakeService(store=self._store, demands_store=None)
        receipt = service.accept(self._command(view, external_id, attempt), self._now())
        if not receipt.run_id:
            raise RuntimeError("the line accepted the case without creating a run")
        return str(receipt.run_id)

    def submit_case(self, view: AgentView) -> str:
        base = f"e2e:{self._session}:{view.case_id}"
        run_id = self._accept(view, base, 1)
        self._runs[run_id] = _RunState(
            view=view, base_external_id=base, attempt=1, current_run_id=run_id, run_ids=[run_id]
        )
        logger.info("e2e case %s submitted as run %s", view.case_id, run_id)
        return run_id

    # -- observation --------------------------------------------------------

    def _status(self, run_id: str) -> dict[str, Any]:
        getter = getattr(self._store, "get_run_status", None)
        if getter is None:
            return {}
        try:
            return dict(getter(run_id) or {})
        except Exception as exc:  # noqa: BLE001 - unreadable store: report as no progress
            logger.warning("could not read status of run %s: %s", run_id, type(exc).__name__)
            return {}

    def _cost(self, status: dict[str, Any]) -> float:
        return sum(float(job.get("actual_cost") or 0.0) for job in status.get("jobs") or [])

    def _phase(self, state: _RunState, status: dict[str, Any]) -> RunPhase:
        from core.line.owner_intake import run_state

        jobs = status.get("jobs") or []
        if not status:
            return RunPhase.PENDING
        verdict = run_state(self._store, state.current_run_id)
        if verdict == "succeeded":
            return RunPhase.DELIVERED
        if verdict == "failed":
            return RunPhase.CANCELLED if status.get("status") == "cancelled" else RunPhase.FAILED
        if any(job.get("status") in _PROGRESS_JOB_STATUSES for job in jobs):
            return RunPhase.RUNNING
        return RunPhase.PENDING

    def poll(self, run_ref: str) -> RunSnapshot:
        state = self._runs[run_ref]
        status = self._status(state.current_run_id)
        jobs = list(status.get("jobs") or [])
        events: list[RunnerEvent] = list(state.pending_events)
        state.pending_events.clear()

        def once(key: tuple[str, str, int], kind: EventKind, detail: str) -> None:
            if key not in state.emitted:
                state.emitted.add(key)
                events.append(RunnerEvent(kind=kind, detail=detail))

        for job in jobs:
            stage, iteration, job_status = str(job.get("stage")), int(job.get("iteration") or 0), job.get("status")
            if job_status == "succeeded":
                once(("ok", stage, iteration), EventKind.AGENT_STEP, f"stage {stage} succeeded")
            elif job_status == "waiting_human":
                cause = job.get("cause_code") or "unknown"
                once(("human", stage, iteration), EventKind.HUMAN_INTERVENTION, f"stage {stage} waiting_human cause={cause}")

        phase = self._phase(state, status)
        succeeded = sum(1 for job in jobs if job.get("status") == "succeeded")
        if state.fault_applied and not state.detected:
            retries = sum(int(job.get("retry_count") or 0) for job in jobs)
            bad = sum(1 for job in jobs if job.get("status") in _BAD_JOB_STATUSES)
            reason = state.detected_hint
            if reason is None and (status.get("status") == "cancelled" or phase is RunPhase.CANCELLED):
                reason = "run cancelled"
            if reason is None and retries > state.baseline_retries:
                reason = "job retried"
            if reason is None and bad > state.baseline_bad:
                reason = "job failed or replanned"
            if reason is not None:
                state.detected = True
                state.succeeded_at_detection = succeeded
                events.append(RunnerEvent(kind=EventKind.FAULT_DETECTED, detail=reason))
        if state.detected and not state.recovered and (phase is RunPhase.DELIVERED or succeeded > state.succeeded_at_detection):
            state.recovered = True
            events.append(RunnerEvent(kind=EventKind.RECOVERED, detail="line resumed progress"))
        return RunSnapshot(phase=phase, cost_usd=round(state.prior_cost + self._cost(status), 6), events=tuple(events))

    # -- faults / control ---------------------------------------------------

    def _mark_fault(self, state: _RunState) -> None:
        status = self._status(state.current_run_id)
        jobs = status.get("jobs") or []
        state.fault_applied = True
        state.baseline_retries = sum(int(job.get("retry_count") or 0) for job in jobs)
        state.baseline_bad = sum(1 for job in jobs if job.get("status") in _BAD_JOB_STATUSES)

    def apply_fault(self, run_ref: str, action: FaultAction) -> bool:
        state = self._runs[run_ref]
        if action is FaultAction.CANCEL_RUN:
            return self._cancel_and_resubmit(state)
        hook = self._hooks.get(action)
        if hook is None:
            return False
        self._mark_fault(state)
        try:
            hook(state.current_run_id)
        except Exception as exc:  # noqa: BLE001 - a failed hook means the fault was not applied
            logger.warning("fault hook %s failed: %s", action.value, type(exc).__name__)
            state.fault_applied = False
            return False
        return True

    def _cancel_and_resubmit(self, state: _RunState) -> bool:
        canceller = getattr(self._store, "cancel_run", None)
        if canceller is None:
            return False
        old_run = state.current_run_id
        old_cost = self._cost(self._status(old_run))
        result = canceller(old_run, reason="e2e_fault_cancel", actor=E2E_CHANNEL, now=self._now())
        if not result.get("ok", False):
            return False
        state.prior_cost += old_cost
        state.attempt += 1
        external_id = f"{state.base_external_id}:a{state.attempt}"
        new_run = self._accept(state.view, external_id, state.attempt)
        state.current_run_id = new_run
        state.run_ids.append(new_run)
        state.fault_applied = True
        state.baseline_retries = 0
        state.baseline_bad = 0
        state.detected_hint = "run cancelled"
        state.pending_events.append(
            RunnerEvent(kind=EventKind.TOOL_CALL, detail=f"resubmitted as attempt {state.attempt}")
        )
        return True

    def cancel(self, run_ref: str) -> None:
        state = self._runs.get(run_ref)
        canceller = getattr(self._store, "cancel_run", None)
        if state is None or canceller is None:
            return
        try:
            canceller(state.current_run_id, reason="e2e_eval_cancel", actor=E2E_CHANNEL, now=self._now())
        except Exception as exc:  # noqa: BLE001 - cancel must never break the evaluator
            logger.warning("cancel of run %s failed: %s", state.current_run_id, type(exc).__name__)

    def product_driver(self, run_ref: str) -> OracleDriver:
        return self._driver_factory(self._runs[run_ref].current_run_id)


# --------------------------------------------------------------------------- #
# Runner loading (CLI)                                                         #
# --------------------------------------------------------------------------- #


class RunnerSpecError(ValueError):
    """The ``--runner module:factory`` spec is malformed or does not build a ``LineRunner``."""


def load_runner(spec: str) -> LineRunner:
    """Build a runner from ``package.module:factory`` (factory takes no arguments).

    The factory is where the caller explicitly wires the store, the product driver
    and any fault hooks; there is no default runner and no environment fallback.
    """

    module_name, separator, attribute = (spec or "").partition(":")
    if not separator or not module_name.strip() or not attribute.strip():
        raise RunnerSpecError("runner spec must look like 'package.module:factory'")
    try:
        module = importlib.import_module(module_name.strip())
        factory = getattr(module, attribute.strip())
    except (ImportError, AttributeError) as exc:
        raise RunnerSpecError(f"cannot import runner {spec!r}: {type(exc).__name__}") from None
    runner = factory() if callable(factory) else factory
    if not isinstance(runner, LineRunner):
        raise RunnerSpecError(f"runner {spec!r} does not implement the LineRunner protocol")
    return runner
