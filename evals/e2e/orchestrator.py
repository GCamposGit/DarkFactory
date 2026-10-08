"""Evaluator-side orchestrator: runs corpus cases, injects faults, records trajectories (USR-160).

The orchestrator owns everything the evaluated system must not see: the spec,
the acceptance journey and the recovery scenario.  It hands the runner only an
``AgentView``, applies ``recovery.fault`` through an injectable
:class:`FaultInjector`, drives the delivered product with the shared oracle, and
writes one ``Trajectory`` per run in exactly the format ``replay`` consumes.

``fault_injected`` is recorded by the orchestrator alone (a runner cannot forge
it) and only when the injector reports that the fault was really applied; a
fault that could not be applied is noted and the run stays ``not_exercised``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from evals.e2e.corpus import Corpus, agent_view
from evals.e2e.driver import FaultAction, LineRunner, RunnerEvent, RunPhase, RunSnapshot
from evals.e2e.models import (
    E2ECase,
    EventKind,
    FaultKind,
    FaultPhase,
    FinalState,
    JourneyResult,
    RecoveryScenario,
    Trajectory,
    TrajectoryEvent,
)
from evals.e2e.oracle import evaluate_journey
from evals.e2e.replay import E2EReport, build_report

logger = logging.getLogger(__name__)

DEFAULT_FAULT_ACTIONS: dict[FaultKind, FaultAction] = {
    FaultKind.WORKER_CRASH: FaultAction.KILL_WORKER,
    FaultKind.PROCESS_CRASH: FaultAction.KILL_WORKER,
    FaultKind.PROVIDER_OUTAGE: FaultAction.CUT_NETWORK,
    FaultKind.DEPENDENCY_UNAVAILABLE: FaultAction.CUT_NETWORK,
    FaultKind.MERGE_CONFLICT: FaultAction.INJECT_MERGE_CONFLICT,
    FaultKind.FLAKY_GATE: FaultAction.FAIL_GATE_ONCE,
    FaultKind.CORRUPTED_STATE: FaultAction.CORRUPT_STATE,
}

_NEWLINES = re.compile(r"\s+")


class InjectionResult(BaseModel):
    """Whether the fault was really applied, plus a content-free label for the event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    applied: bool
    detail: str = ""


class FaultInjector(Protocol):
    def inject(self, runner: LineRunner, run_ref: str, scenario: RecoveryScenario) -> InjectionResult: ...


class ActionFaultInjector:
    """Maps ``FaultKind`` to a :class:`FaultAction` and asks the runner to apply it.

    Only the action reaches the runner: never the scenario description.
    """

    def __init__(self, mapping: dict[FaultKind, FaultAction] | None = None) -> None:
        self._mapping = dict(DEFAULT_FAULT_ACTIONS if mapping is None else mapping)

    def inject(self, runner: LineRunner, run_ref: str, scenario: RecoveryScenario) -> InjectionResult:
        action = self._mapping.get(scenario.fault)
        label = f"{scenario.fault.value}:{action.value}" if action else scenario.fault.value
        if action is None:
            return InjectionResult(applied=False, detail=label)
        try:
            applied = bool(runner.apply_fault(run_ref, action))
        except Exception as exc:  # noqa: BLE001 - a broken fault hook must not abort the evaluation
            logger.warning("fault %s failed to apply: %s", label, type(exc).__name__)
            return InjectionResult(applied=False, detail=label)
        return InjectionResult(applied=applied, detail=label)


@dataclass(frozen=True)
class OrchestratorConfig:
    system_id: str
    run_label: str = "run"
    poll_interval_seconds: float = 5.0
    repeats: int = 1
    inject_faults: bool = True
    case_ids: tuple[str, ...] | None = None


class _Recorder:
    def __init__(self, clock: Callable[[], float], started: float) -> None:
        self._clock = clock
        self._started = started
        self.events: list[TrajectoryEvent] = []
        self._last_t = 0.0

    def elapsed(self) -> float:
        return max(0.0, self._clock() - self._started)

    def add(self, kind: EventKind, detail: str = "") -> None:
        t = max(self._last_t, round(self.elapsed(), 6))
        self._last_t = t
        self.events.append(
            TrajectoryEvent(seq=len(self.events), t_seconds=t, kind=kind, detail=_NEWLINES.sub(" ", detail).strip()[:500])
        )

    @property
    def last_t(self) -> float:
        return self._last_t


class E2EOrchestrator:
    """Runs cases against a :class:`LineRunner` and produces replay-ready trajectories."""

    def __init__(
        self,
        corpus: Corpus,
        runner: LineRunner,
        config: OrchestratorConfig,
        *,
        injector: FaultInjector | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if config.repeats < 1:
            raise ValueError("repeats must be >= 1")
        if config.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0")
        self._corpus = corpus
        self._runner = runner
        self._config = config
        self._injector: FaultInjector = injector or ActionFaultInjector()
        self._clock = clock
        self._sleep = sleep

    # -- selection ----------------------------------------------------------

    def selected_cases(self) -> list[E2ECase]:
        wanted = self._config.case_ids
        if wanted is None:
            return list(self._corpus.cases)
        unknown = sorted(set(wanted) - {case.case_id for case in self._corpus.cases})
        if unknown:
            raise ValueError(f"unknown case ids: {', '.join(unknown)}")
        return [case for case in self._corpus.cases if case.case_id in set(wanted)]

    # -- one run ------------------------------------------------------------

    def _absorb(self, rec: _Recorder, events: Iterable[RunnerEvent]) -> bool:
        recovered = False
        for event in events:
            if event.kind is EventKind.FAULT_INJECTED:
                logger.warning("runner tried to emit fault_injected; ignored (evaluator-owned event)")
                continue
            rec.add(event.kind, event.detail)
            recovered = recovered or event.kind is EventKind.RECOVERED
        return recovered

    def _inject(self, case: E2ECase, run_ref: str, rec: _Recorder, notes: list[str]) -> bool:
        result = self._injector.inject(self._runner, run_ref, case.recovery)
        if result.applied:
            rec.add(EventKind.FAULT_INJECTED, result.detail)
        else:
            notes.append(f"fault_not_applied:{result.detail or case.recovery.fault.value}")
        return result.applied

    def _await_recovery(self, case: E2ECase, run_ref: str, rec: _Recorder, deadline: float) -> RunSnapshot | None:
        """Poll until the runner reports ``recovered`` or the recovery window closes."""

        injected_at = rec.elapsed()
        window = min(float(case.recovery.max_recovery_seconds), deadline - injected_at)
        last: RunSnapshot | None = None
        while True:
            last = self._runner.poll(run_ref)
            if self._absorb(rec, last.events):
                return last
            if rec.elapsed() - injected_at >= window:
                return last
            self._sleep(self._config.poll_interval_seconds)

    def run_case(self, case: E2ECase, *, repeat: int = 1) -> Trajectory:
        view = agent_view(case)  # the ONLY thing the runner receives about the case
        started = self._clock()
        rec = _Recorder(self._clock, started)
        notes: list[str] = []
        journey: list[JourneyResult] = []
        cost = 0.0
        final = FinalState.ERROR
        run_ref: str | None = None
        fault_pending = self._config.inject_faults
        deadline = float(case.time_budget_seconds)
        try:
            run_ref = self._runner.submit_case(view)
            while True:
                snapshot = self._runner.poll(run_ref)
                cost = max(cost, snapshot.cost_usd)
                self._absorb(rec, snapshot.events)
                if (
                    fault_pending
                    and case.recovery.phase is FaultPhase.BUILD
                    and snapshot.phase is RunPhase.RUNNING
                ):
                    fault_pending = False
                    self._inject(case, run_ref, rec, notes)
                if snapshot.phase.terminal:
                    break
                if rec.elapsed() >= deadline:
                    self._runner.cancel(run_ref)
                    final = FinalState.TIMEOUT
                    break
                self._sleep(self._config.poll_interval_seconds)
            if final is not FinalState.TIMEOUT:
                if snapshot.phase is RunPhase.DELIVERED:
                    final = FinalState.DELIVERED
                    if fault_pending and case.recovery.phase is FaultPhase.BUILD:
                        notes.append("build_fault_never_reached_running_phase")
                else:
                    final = FinalState.ABANDONED
            if final is FinalState.DELIVERED:
                journey, cost = self._run_journey(case, run_ref, rec, notes, fault_pending, deadline, cost)
        except Exception as exc:  # noqa: BLE001 - one broken run must not stop the evaluation
            logger.error("case %s failed in the runner: %s", case.case_id, type(exc).__name__)
            final = FinalState.ERROR
            notes.append(f"runner_error:{type(exc).__name__}")
            if run_ref is not None:
                try:
                    self._runner.cancel(run_ref)
                except Exception:  # noqa: BLE001
                    logger.warning("cancel after error failed for %s", case.case_id)
        duration = max(rec.last_t, round(rec.elapsed(), 6))
        return Trajectory(
            trajectory_id=f"{self._config.run_label}-{case.case_id}-r{repeat}",
            case_id=case.case_id,
            case_sha256=case.content_sha256(),
            system_id=self._config.system_id,
            final_state=final,
            duration_seconds=duration,
            cost_usd=round(cost, 6),
            journey_results=journey,
            events=rec.events,
            notes="; ".join(notes)[:1000],
        )

    def _run_journey(
        self,
        case: E2ECase,
        run_ref: str,
        rec: _Recorder,
        notes: list[str],
        fault_pending: bool,
        deadline: float,
        cost: float,
    ) -> tuple[list[JourneyResult], float]:
        driver = self._runner.product_driver(run_ref)
        pending = fault_pending and case.recovery.phase is FaultPhase.RUNTIME
        latest_cost = cost

        def after_step(step_id: str) -> None:
            nonlocal pending, latest_cost
            if not pending or step_id != case.recovery.inject_after_step:
                return
            pending = False
            if self._inject(case, run_ref, rec, notes):
                snapshot = self._await_recovery(case, run_ref, rec, deadline)
                if snapshot is not None:
                    latest_cost = max(latest_cost, snapshot.cost_usd)

        results = evaluate_journey(case, driver, after_step=after_step)
        if pending:
            notes.append("runtime_fault_step_not_reached")
        return results, latest_cost

    # -- whole corpus -------------------------------------------------------

    def run_all(self) -> list[Trajectory]:
        trajectories: list[Trajectory] = []
        for case in self.selected_cases():
            for repeat in range(1, self._config.repeats + 1):
                trajectory = self.run_case(case, repeat=repeat)
                logger.info("case %s r%d -> %s", case.case_id, repeat, trajectory.final_state.value)
                trajectories.append(trajectory)
        return trajectories

    def run_and_report(self, out_dir: Path) -> tuple[list[Trajectory], E2EReport]:
        """Run, persist ``trajectories/*.json`` and ``report.json`` under ``out_dir``, return both."""

        trajectories_dir = out_dir / "trajectories"
        if trajectories_dir.exists() and any(trajectories_dir.iterdir()):
            raise FileExistsError("output directory already holds trajectories; choose an empty one")
        trajectories = self.run_all()
        write_trajectories(trajectories, trajectories_dir)
        report = build_report(self._corpus, trajectories, system_id=self._config.system_id)
        write_report(report, out_dir / "report.json")
        return trajectories, report


def write_trajectories(trajectories: Sequence[Trajectory], directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for trajectory in trajectories:
        path = directory / f"{trajectory.trajectory_id}.json"
        path.write_text(json.dumps(trajectory.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8", newline="\n")
        paths.append(path)
    return paths


def write_report(report: E2EReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8", newline="\n")


def load_report(path: Path) -> E2EReport:
    return E2EReport.model_validate_json(path.read_text(encoding="utf-8"))
