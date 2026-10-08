"""USR-160: driver, fault-injecting orchestrator and comparable runs for the E2E eval.

Hermetic: a deterministic fake line runner and a fake clock (no sleeping), plus a
temp-file ``SQLiteControlStore`` for the control-store runner.  Nothing here talks
to a real line, a real database URL or a paid model.
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from evals.e2e import cli as e2e_cli
from evals.e2e.corpus import AgentView, Corpus, load_corpus
from evals.e2e.driver import (
    ControlStoreLineRunner,
    FaultAction,
    LineRunner,
    RunnerEvent,
    RunnerSpecError,
    RunPhase,
    RunSnapshot,
    load_runner,
)
from evals.e2e.leakage import LeakScanner
from evals.e2e.models import (
    E2ECase,
    EventKind,
    FaultKind,
    FaultPhase,
    FinalState,
    OracleStep,
    Trajectory,
)
from evals.e2e.oracle import Observation
from evals.e2e.orchestrator import (
    ActionFaultInjector,
    E2EOrchestrator,
    InjectionResult,
    OrchestratorConfig,
    load_report,
)
from evals.e2e.replay import (
    RecoveryStatus,
    RejectReason,
    build_report,
    compare_reports,
    load_trajectories,
    verify_report,
)

pytestmark = pytest.mark.offline


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Script:
    """What the fake line does for one case."""

    outcome: RunPhase = RunPhase.DELIVERED
    deliver_at: float = 100.0
    cost: float = 1.0
    human_at: float | None = None
    detect_delay: float | None = 2.0
    recover_delay: float | None = 10.0
    supports_fault: bool = True
    failing_steps: tuple[str, ...] = ()
    forged_detail: str = ""


class ScriptedDriver:
    """Product driver: passes every step except the scripted failing ones."""

    def __init__(self, failing_steps: tuple[str, ...]) -> None:
        self.failing_steps = failing_steps
        self.steps_seen: list[str] = []

    def execute(self, step: OracleStep) -> Observation:
        self.steps_seen.append(step.id)
        return Observation(exit_code=1 if step.id in self.failing_steps else 0)


class FakeRunner:
    def __init__(self, clock: FakeClock, scripts: dict[str, Script] | None = None) -> None:
        self.clock = clock
        self.scripts = scripts or {}
        self.received: list[Any] = []  # every argument ever handed to the runner
        self.submitted_at: dict[str, float] = {}
        self.fault_at: dict[str, float] = {}
        self.cancelled: list[str] = []
        self.sent: dict[str, set[str]] = {}
        self._case_of: dict[str, str] = {}
        self.drivers: dict[str, ScriptedDriver] = {}

    def _script(self, run_ref: str) -> Script:
        return self.scripts.get(self._case_of[run_ref], Script())

    def submit_case(self, view: AgentView) -> str:
        self.received.append(view)
        run_ref = f"fake-{len(self._case_of) + 1}"
        self._case_of[run_ref] = view.case_id
        self.submitted_at[run_ref] = self.clock.now
        self.sent[run_ref] = set()
        return run_ref

    def poll(self, run_ref: str) -> RunSnapshot:
        script = self._script(run_ref)
        elapsed = self.clock.now - self.submitted_at[run_ref]
        events: list[RunnerEvent] = []

        def once(key: str, kind: EventKind, detail: str) -> None:
            if key not in self.sent[run_ref]:
                self.sent[run_ref].add(key)
                events.append(RunnerEvent(kind=kind, detail=detail))

        if elapsed >= 1:
            once("plan", EventKind.AGENT_STEP, "planning done")
        if script.human_at is not None and elapsed >= script.human_at:
            once("human", EventKind.HUMAN_INTERVENTION, "owner answered a grill question")
        if script.forged_detail:
            once("forged", EventKind.TOOL_CALL, script.forged_detail)
            once("forged2", EventKind.FAULT_INJECTED, "runner forged an injection")
        fault_time = self.fault_at.get(run_ref)
        if fault_time is not None:
            since = self.clock.now - fault_time
            if script.detect_delay is not None and since >= script.detect_delay:
                once("detected", EventKind.FAULT_DETECTED, "lease expired")
            if script.recover_delay is not None and since >= script.recover_delay:
                once("recovered", EventKind.RECOVERED, "job re-claimed")
        if elapsed >= script.deliver_at:
            phase = script.outcome
        else:
            phase = RunPhase.RUNNING if elapsed >= 1 else RunPhase.PENDING
        return RunSnapshot(phase=phase, cost_usd=script.cost if elapsed >= 1 else 0.0, events=tuple(events))

    def apply_fault(self, run_ref: str, action: FaultAction) -> bool:
        self.received.append(action)
        if not self._script(run_ref).supports_fault:
            return False
        self.fault_at[run_ref] = self.clock.now
        return True

    def cancel(self, run_ref: str) -> None:
        self.cancelled.append(run_ref)

    def product_driver(self, run_ref: str) -> ScriptedDriver:
        driver = ScriptedDriver(self._script(run_ref).failing_steps)
        self.drivers[run_ref] = driver
        return driver


def synthetic_case(
    base: E2ECase,
    case_id: str,
    *,
    fault: FaultKind = FaultKind.WORKER_CRASH,
    phase: FaultPhase = FaultPhase.BUILD,
    time_budget: int = 1000,
) -> E2ECase:
    """A case with a trivial journey (exit_code 0) so the fake product can pass or fail it."""

    data = json.loads(base.model_dump_json())
    data.update(case_id=case_id, seed_files={}, time_budget_seconds=time_budget, cost_budget_usd=5)
    data["journey"] = [
        {"id": step, "kind": "cli", "args": ["run"], "expect": {"exit_code": 0}} for step in ("s1", "s2", "s3")
    ]
    data["recovery"] = {
        "fault": fault.value,
        "phase": phase.value,
        "description": "synthetic fault used by the hermetic driver tests",
        "max_recovery_seconds": 60,
    }
    if phase is FaultPhase.RUNTIME:
        data["recovery"]["inject_after_step"] = "s2"
    return E2ECase.model_validate(data)


@pytest.fixture(scope="module")
def real_corpus() -> Corpus:
    return load_corpus()


@pytest.fixture
def synth_corpus(real_corpus: Corpus) -> Corpus:
    bases = [real_corpus.get(case_id) for case_id in ("E2E-API-NOTES", "E2E-CLI-UNITS", "E2E-BOT-EXPENSES")]
    assert all(bases)
    return Corpus(
        [
            synthetic_case(bases[0], "E2E-SYN-BUILD"),  # type: ignore[arg-type]
            synthetic_case(bases[1], "E2E-SYN-RUNTIME", fault=FaultKind.PROCESS_CRASH, phase=FaultPhase.RUNTIME),  # type: ignore[arg-type]
            synthetic_case(bases[2], "E2E-SYN-PLAIN", fault=FaultKind.FLAKY_GATE),  # type: ignore[arg-type]
        ]
    )


def make_orchestrator(
    corpus: Corpus,
    scripts: dict[str, Script] | None = None,
    *,
    system_id: str = "fake-a",
    label: str = "t",
    **config: Any,
) -> tuple[E2EOrchestrator, FakeRunner, FakeClock]:
    clock = FakeClock()
    runner = FakeRunner(clock, scripts)
    cfg = OrchestratorConfig(system_id=system_id, run_label=label, poll_interval_seconds=5.0, **config)
    return E2EOrchestrator(corpus, runner, cfg, clock=clock, sleep=clock.sleep), runner, clock


def kinds(trajectory: Trajectory) -> list[EventKind]:
    return [event.kind for event in trajectory.events]


# --------------------------------------------------------------------------- #
# Trajectory format and fault injection                                        #
# --------------------------------------------------------------------------- #


def test_fake_runner_satisfies_protocol() -> None:
    assert isinstance(FakeRunner(FakeClock()), LineRunner)


def test_build_fault_is_injected_and_recovered_in_replay_format(synth_corpus: Corpus, tmp_path: Path) -> None:
    orchestrator, runner, _ = make_orchestrator(synth_corpus, {"E2E-SYN-BUILD": Script(recover_delay=10.0)})
    trajectory = orchestrator.run_case(synth_corpus.get("E2E-SYN-BUILD"))  # type: ignore[arg-type]

    assert trajectory.final_state is FinalState.DELIVERED
    assert trajectory.case_sha256 == synth_corpus.get("E2E-SYN-BUILD").content_sha256()  # type: ignore[union-attr]
    assert [r.passed for r in trajectory.journey_results] == [True, True, True]
    assert trajectory.cost_usd == 1.0
    order = kinds(trajectory)
    assert order.index(EventKind.FAULT_INJECTED) < order.index(EventKind.FAULT_DETECTED) < order.index(EventKind.RECOVERED)
    assert FaultAction.KILL_WORKER in runner.received
    injected = next(e for e in trajectory.events if e.kind is EventKind.FAULT_INJECTED)
    assert injected.detail == "worker_crash:kill_worker"

    # The file written by the orchestrator is exactly what the replay loads.
    (tmp_path / "t").mkdir()
    path = tmp_path / "t" / f"{trajectory.trajectory_id}.json"
    path.write_text(json.dumps(trajectory.model_dump(mode="json")), encoding="utf-8")
    assert load_trajectories(tmp_path / "t") == [trajectory]
    report = build_report(synth_corpus, [trajectory], system_id="fake-a")
    receipt = report.receipts[0]
    assert receipt.e2e_success and receipt.autonomous_success
    assert receipt.recovery_status is RecoveryStatus.RECOVERED
    assert verify_report(report) == []


def test_runtime_fault_is_injected_between_journey_steps(synth_corpus: Corpus) -> None:
    orchestrator, runner, _ = make_orchestrator(synth_corpus, {"E2E-SYN-RUNTIME": Script(recover_delay=7.0)})
    trajectory = orchestrator.run_case(synth_corpus.get("E2E-SYN-RUNTIME"))  # type: ignore[arg-type]

    assert trajectory.final_state is FinalState.DELIVERED
    assert all(r.passed for r in trajectory.journey_results)
    order = kinds(trajectory)
    assert order.count(EventKind.FAULT_INJECTED) == 1
    assert order.index(EventKind.FAULT_INJECTED) < order.index(EventKind.RECOVERED)
    injected = next(e for e in trajectory.events if e.kind is EventKind.FAULT_INJECTED)
    assert injected.t_seconds >= 100.0  # only after delivery, i.e. against the running product
    report = build_report(synth_corpus, [trajectory], system_id="fake-a")
    assert report.receipts[0].recovery_status is RecoveryStatus.RECOVERED
    assert FaultAction.KILL_WORKER in runner.received


def test_failed_run_and_failing_journey_are_not_successes(synth_corpus: Corpus) -> None:
    scripts = {
        "E2E-SYN-BUILD": Script(outcome=RunPhase.FAILED, deliver_at=40.0),
        "E2E-SYN-RUNTIME": Script(failing_steps=("s3",)),
    }
    orchestrator, _, _ = make_orchestrator(synth_corpus, scripts)
    failed = orchestrator.run_case(synth_corpus.get("E2E-SYN-BUILD"))  # type: ignore[arg-type]
    wrong = orchestrator.run_case(synth_corpus.get("E2E-SYN-RUNTIME"))  # type: ignore[arg-type]
    assert failed.final_state is FinalState.ABANDONED and failed.journey_results == []
    assert [r.passed for r in wrong.journey_results] == [True, True, False]
    report = build_report(synth_corpus, [failed, wrong], system_id="fake-a")
    assert [r.e2e_success for r in report.receipts] == [False, False]
    assert report.summary.e2e_success_rate == 0.0


def test_human_intervention_is_recorded_and_breaks_autonomy(synth_corpus: Corpus) -> None:
    orchestrator, _, _ = make_orchestrator(synth_corpus, {"E2E-SYN-PLAIN": Script(human_at=20.0, recover_delay=30.0)})
    trajectory = orchestrator.run_case(synth_corpus.get("E2E-SYN-PLAIN"))  # type: ignore[arg-type]
    assert EventKind.HUMAN_INTERVENTION in kinds(trajectory)
    report = build_report(synth_corpus, [trajectory], system_id="fake-a")
    receipt = report.receipts[0]
    assert receipt.e2e_success and not receipt.autonomous_success
    assert receipt.interventions == 1
    # The human acted after the fault was injected, so the recovery was assisted.
    assert receipt.recovery_status is RecoveryStatus.HUMAN_ASSISTED


def test_unsupported_fault_is_not_recorded_as_injected(synth_corpus: Corpus) -> None:
    orchestrator, _, _ = make_orchestrator(synth_corpus, {"E2E-SYN-BUILD": Script(supports_fault=False)})
    trajectory = orchestrator.run_case(synth_corpus.get("E2E-SYN-BUILD"))  # type: ignore[arg-type]
    assert EventKind.FAULT_INJECTED not in kinds(trajectory)
    assert "fault_not_applied:worker_crash:kill_worker" in trajectory.notes
    report = build_report(synth_corpus, [trajectory], system_id="fake-a")
    assert report.receipts[0].recovery_status is RecoveryStatus.NOT_EXERCISED


def test_no_faults_flag_runs_a_baseline(synth_corpus: Corpus) -> None:
    orchestrator, runner, _ = make_orchestrator(synth_corpus, inject_faults=False)
    trajectory = orchestrator.run_case(synth_corpus.get("E2E-SYN-BUILD"))  # type: ignore[arg-type]
    assert EventKind.FAULT_INJECTED not in kinds(trajectory)
    assert not any(isinstance(item, FaultAction) for item in runner.received)


def test_fault_without_detection_or_recovery_fails_recovery(synth_corpus: Corpus) -> None:
    orchestrator, _, _ = make_orchestrator(
        synth_corpus, {"E2E-SYN-BUILD": Script(detect_delay=None, recover_delay=None)}
    )
    trajectory = orchestrator.run_case(synth_corpus.get("E2E-SYN-BUILD"))  # type: ignore[arg-type]
    assert EventKind.FAULT_INJECTED in kinds(trajectory)
    report = build_report(synth_corpus, [trajectory], system_id="fake-a")
    assert report.receipts[0].recovery_status is RecoveryStatus.FAILED


def test_timeout_cancels_the_run(real_corpus: Corpus) -> None:
    case = synthetic_case(real_corpus.get("E2E-API-NOTES"), "E2E-SYN-SLOW", time_budget=50)  # type: ignore[arg-type]
    corpus = Corpus([case])
    orchestrator, runner, _ = make_orchestrator(corpus, {"E2E-SYN-SLOW": Script(deliver_at=10_000.0)})
    trajectory = orchestrator.run_case(case)
    assert trajectory.final_state is FinalState.TIMEOUT
    assert runner.cancelled == ["fake-1"]
    assert trajectory.duration_seconds >= 50


def test_runner_crash_becomes_error_trajectory(synth_corpus: Corpus) -> None:
    orchestrator, runner, _ = make_orchestrator(synth_corpus)

    def boom(view: AgentView) -> str:
        raise RuntimeError("line exploded")

    runner.submit_case = boom  # type: ignore[method-assign]
    trajectory = orchestrator.run_case(synth_corpus.get("E2E-SYN-BUILD"))  # type: ignore[arg-type]
    assert trajectory.final_state is FinalState.ERROR
    assert trajectory.notes == "runner_error:RuntimeError"
    assert "line exploded" not in trajectory.model_dump_json()


def test_custom_fault_injector_is_used(synth_corpus: Corpus) -> None:
    class Spy:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def inject(self, runner: LineRunner, run_ref: str, scenario: Any) -> InjectionResult:
            self.seen.append(scenario.fault.value)
            return InjectionResult(applied=True, detail="custom:kill")

    clock = FakeClock()
    spy = Spy()
    orchestrator = E2EOrchestrator(
        synth_corpus,
        FakeRunner(clock, {"E2E-SYN-BUILD": Script(recover_delay=None)}),
        OrchestratorConfig(system_id="fake-a", case_ids=("E2E-SYN-BUILD",)),
        injector=spy,
        clock=clock,
        sleep=clock.sleep,
    )
    [trajectory] = orchestrator.run_all()
    assert spy.seen == ["worker_crash"]
    assert next(e for e in trajectory.events if e.kind is EventKind.FAULT_INJECTED).detail == "custom:kill"


def test_default_mapping_covers_every_fault_kind() -> None:
    from evals.e2e.orchestrator import DEFAULT_FAULT_ACTIONS

    assert set(DEFAULT_FAULT_ACTIONS) == set(FaultKind)
    assert ActionFaultInjector({}).inject(FakeRunner(FakeClock()), "x", _scenario(FaultKind.WORKER_CRASH)).applied is False


def _scenario(fault: FaultKind) -> Any:
    from evals.e2e.models import RecoveryScenario

    return RecoveryScenario(
        fault=fault, phase=FaultPhase.BUILD, description="fault used only by the mapping test", max_recovery_seconds=60
    )


# --------------------------------------------------------------------------- #
# Anti-leak                                                                    #
# --------------------------------------------------------------------------- #


def test_runner_only_ever_receives_agent_view_and_fault_actions(real_corpus: Corpus) -> None:
    orchestrator, runner, _ = make_orchestrator(real_corpus)
    orchestrator.run_all()
    assert runner.received, "the runner must have been used"
    assert all(isinstance(item, (AgentView, FaultAction)) for item in runner.received)
    assert not any(isinstance(item, E2ECase) for item in runner.received)
    views = [item for item in runner.received if isinstance(item, AgentView)]
    assert {v.case_id for v in views} == {c.case_id for c in real_corpus.cases}
    for view in views:
        case = real_corpus.get(view.case_id)
        assert case is not None and view.brief == case.brief
        assert set(view.model_dump()) == {"case_id", "brief"}

    # Nothing reserved (canary, spec n-grams, oracle literals) is in what the runner saw.
    scanner = LeakScanner(real_corpus.cases)
    handed = [item.model_dump(mode="json") if isinstance(item, AgentView) else item.value for item in runner.received]
    assert scanner.scan_value(handed) == []


def test_runner_cannot_forge_fault_injected(synth_corpus: Corpus) -> None:
    orchestrator, _, _ = make_orchestrator(
        synth_corpus, {"E2E-SYN-BUILD": Script(forged_detail="harmless tool call")}, inject_faults=False
    )
    trajectory = orchestrator.run_case(synth_corpus.get("E2E-SYN-BUILD"))  # type: ignore[arg-type]
    assert EventKind.FAULT_INJECTED not in kinds(trajectory)
    assert EventKind.TOOL_CALL in kinds(trajectory)


def test_leaking_runner_output_is_rejected_by_replay(synth_corpus: Corpus) -> None:
    case = synth_corpus.get("E2E-SYN-BUILD")
    assert case is not None
    orchestrator, _, _ = make_orchestrator(synth_corpus, {"E2E-SYN-BUILD": Script(forged_detail=case.canary)})
    trajectory = orchestrator.run_case(case)
    report = build_report(synth_corpus, [trajectory], system_id="fake-a")
    assert report.receipts[0].rejected_reason is RejectReason.LEAK_DETECTED
    assert not report.receipts[0].e2e_success
    assert case.canary not in report.model_dump_json()


# --------------------------------------------------------------------------- #
# Whole runs, persisted output and comparison                                  #
# --------------------------------------------------------------------------- #


def run_to_dir(corpus: Corpus, out: Path, scripts: dict[str, Script], system_id: str, label: str) -> Any:
    orchestrator, _, _ = make_orchestrator(corpus, scripts, system_id=system_id, label=label, repeats=2)
    return orchestrator.run_and_report(out)


def test_two_real_style_runs_are_comparable(synth_corpus: Corpus, tmp_path: Path) -> None:
    good = {"E2E-SYN-BUILD": Script(), "E2E-SYN-RUNTIME": Script(), "E2E-SYN-PLAIN": Script()}
    worse = {
        "E2E-SYN-BUILD": Script(outcome=RunPhase.FAILED, deliver_at=30.0),
        "E2E-SYN-RUNTIME": Script(failing_steps=("s2",), cost=3.0),
        "E2E-SYN-PLAIN": Script(human_at=10.0),
    }
    _, base = run_to_dir(synth_corpus, tmp_path / "a", good, "sys-a", "a")
    trajectories, candidate = run_to_dir(synth_corpus, tmp_path / "b", worse, "sys-b", "b")

    assert len(trajectories) == 6  # 3 cases x 2 repeats
    assert len(list((tmp_path / "b" / "trajectories").glob("*.json"))) == 6
    assert load_report(tmp_path / "a" / "report.json") == base
    assert verify_report(base) == [] and verify_report(candidate) == []

    comparison = compare_reports(base, candidate)
    assert comparison.base_system == "sys-a" and comparison.candidate_system == "sys-b"
    assert comparison.deltas["e2e_success_rate"] == pytest.approx(-2 / 3, abs=1e-6)
    assert comparison.deltas["intervention_rate"] is not None and comparison.deltas["intervention_rate"] > 0
    assert {c.case_id for c in comparison.case_changes} == {"E2E-SYN-BUILD", "E2E-SYN-RUNTIME"}
    assert comparison.deltas["autonomous_success_rate"] == pytest.approx(-1.0)  # the human-assisted run is not autonomous

    # Deterministic: replaying the persisted files reproduces the sealed report byte for byte.
    replayed = build_report(synth_corpus, load_trajectories(tmp_path / "b" / "trajectories"), system_id="sys-b")
    assert replayed.report_sha256 == candidate.report_sha256


def test_run_and_report_refuses_a_dirty_output_directory(synth_corpus: Corpus, tmp_path: Path) -> None:
    run_to_dir(synth_corpus, tmp_path / "x", {}, "sys-a", "a")
    with pytest.raises(FileExistsError):
        run_to_dir(synth_corpus, tmp_path / "x", {}, "sys-a", "a")


def test_orchestrator_rejects_unknown_case_ids(synth_corpus: Corpus) -> None:
    orchestrator, _, _ = make_orchestrator(synth_corpus, case_ids=("E2E-NOPE",))
    with pytest.raises(ValueError, match="unknown case ids"):
        orchestrator.run_all()


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def _install_fake_runner_module(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("fake_e2e_runner_mod")

    class TickingRunner(FakeRunner):
        """The CLI uses the real clock and sleep, so the fake line's own clock ticks on every poll."""

        def poll(self, run_ref: str) -> RunSnapshot:
            self.clock.now += 10.0
            return super().poll(run_ref)

    module.build = lambda: TickingRunner(FakeClock())  # type: ignore[attr-defined]
    module.not_a_runner = lambda: object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_e2e_runner_mod", module)


def test_cli_run_fails_closed_without_an_explicit_runner(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = e2e_cli.main(["run", "--system", "sys-a", "--out", str(tmp_path / "o")])
    captured = capsys.readouterr()
    assert code == 2
    assert "--runner is required" in captured.err
    assert not (tmp_path / "o").exists()


@pytest.mark.parametrize("spec", ["", "no_colon", ":factory", "module:", "does_not_exist_mod:build"])
def test_cli_run_rejects_bad_runner_specs(spec: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = e2e_cli.main(["run", "--runner", spec, "--system", "sys-a", "--out", str(tmp_path / "o")])
    assert code == 2
    assert not (tmp_path / "o").exists()
    capsys.readouterr()


def test_load_runner_requires_the_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_runner_module(monkeypatch)
    assert isinstance(load_runner("fake_e2e_runner_mod:build"), LineRunner)
    with pytest.raises(RunnerSpecError, match="LineRunner"):
        load_runner("fake_e2e_runner_mod:not_a_runner")


def test_cli_run_and_compare_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fake_runner_module(monkeypatch)
    for system, name in (("sys-a", "a"), ("sys-b", "b")):
        code = e2e_cli.main(
            [
                "run", "--runner", "fake_e2e_runner_mod:build", "--system", system, "--out", str(tmp_path / name),
                "--cases", "E2E-CLI-UNITS,E2E-API-NOTES", "--run-label", name, "--poll-interval", "0.001",
            ]
        )
        assert code == 0
    out = capsys.readouterr().out
    assert "trajectories=2" in out
    assert (tmp_path / "a" / "report.json").is_file()
    assert len(list((tmp_path / "a" / "trajectories").glob("*.json"))) == 2

    code = e2e_cli.main(["compare", "--base", str(tmp_path / "a" / "report.json"), "--candidate", str(tmp_path / "b" / "report.json")])
    assert code == 0
    comparison = json.loads(capsys.readouterr().out)
    assert comparison["base_system"] == "sys-a" and comparison["candidate_system"] == "sys-b"

    # A second run into the same directory is refused (no silent overwrite of evidence).
    code = e2e_cli.main(
        ["run", "--runner", "fake_e2e_runner_mod:build", "--system", "sys-a", "--out", str(tmp_path / "a")]
    )
    assert code == 2


# --------------------------------------------------------------------------- #
# ControlStoreLineRunner on a temp SQLite control store                        #
# --------------------------------------------------------------------------- #


@pytest.fixture
def sqlite_store(tmp_path: Path) -> Any:
    from core.workflow.control_store import SQLiteControlStore

    return SQLiteControlStore(db_path=tmp_path / "control.db")


def _sql(store: Any, sql: str, params: tuple[Any, ...] = ()) -> None:
    conn = store._connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _add_job(store: Any, run_id: str, stage: str, status: str) -> None:
    _sql(
        store,
        "INSERT INTO jobs (run_id, ticket_id, plan_version, stage, iteration, status, role, created_at, updated_at) "
        "SELECT run_id, ticket_id, plan_version, ?, 0, ?, role, created_at, updated_at FROM jobs WHERE run_id = ? LIMIT 1",
        (stage, status, run_id),
    )


def _set_stage(store: Any, run_id: str, stage: str, status: str, cost: float = 0.0) -> None:
    _sql(
        store,
        "UPDATE jobs SET status = ?, actual_cost = ? WHERE run_id = ? AND stage = ?",
        (status, cost, run_id, stage),
    )


def _runner(store: Any, **kwargs: Any) -> ControlStoreLineRunner:
    return ControlStoreLineRunner(store, driver_factory=lambda run_id: ScriptedDriver(()), session_id="t1", **kwargs)


def test_control_store_runner_submits_only_the_agent_view(
    sqlite_store: Any, real_corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("DARKFAC_HF02_DATABASE_URL", "DARKHUB_LINE_DATABASE_URL", "DARKHUB_CONTROL_DATABASE_URL"):
        monkeypatch.setenv(name, "postgresql://invalid.invalid/never-used")
    from evals.e2e.corpus import agent_view

    case = real_corpus.get("E2E-CLI-UNITS")
    assert case is not None
    runner = _runner(sqlite_store)
    run_ref = runner.submit_case(agent_view(case))

    payload = sqlite_store.get_run_payload(run_ref)  # the injected temp store, not any URL from the environment
    assert payload is not None
    assert payload["problem"] == case.brief and payload["title"] == case.case_id
    assert LeakScanner(real_corpus.cases).scan_value(payload) == []
    assert case.spec not in json.dumps(payload) and case.canary not in json.dumps(payload)
    assert runner.poll(run_ref).phase is RunPhase.PENDING


def test_control_store_runner_refuses_unsafe_stores() -> None:
    with pytest.raises(ValueError, match="explicit ControlStore"):
        ControlStoreLineRunner(None, driver_factory=lambda run_id: ScriptedDriver(()))

    class PostgresControlStore:
        mock_mode = False

    class MockedStore:
        mock_mode = True

    with pytest.raises(ValueError, match="Postgres"):
        ControlStoreLineRunner(PostgresControlStore(), driver_factory=lambda run_id: ScriptedDriver(()))
    with pytest.raises(ValueError, match="mock mode"):
        ControlStoreLineRunner(MockedStore(), driver_factory=lambda run_id: ScriptedDriver(()))
    ControlStoreLineRunner(PostgresControlStore(), driver_factory=lambda run_id: ScriptedDriver(()), allow_remote_store=True)


def test_control_store_runner_progress_human_and_hooks(sqlite_store: Any, real_corpus: Corpus) -> None:
    from evals.e2e.corpus import agent_view

    case = real_corpus.get("E2E-API-NOTES")
    assert case is not None
    called: list[str] = []
    runner = _runner(sqlite_store, fault_hooks={FaultAction.KILL_WORKER: called.append})
    run_ref = runner.submit_case(agent_view(case))

    assert runner.apply_fault(run_ref, FaultAction.CUT_NETWORK) is False  # no hook: not exercised
    _set_stage(sqlite_store, run_ref, "grill", "succeeded")
    _add_job(sqlite_store, run_ref, "planning", "pending")  # the line always enqueues the next stage
    first = runner.poll(run_ref)
    assert first.phase is RunPhase.RUNNING
    assert [e.kind for e in first.events] == [EventKind.AGENT_STEP]
    assert runner.poll(run_ref).events == ()  # incremental: nothing new

    assert runner.apply_fault(run_ref, FaultAction.KILL_WORKER) is True
    assert called == [run_ref]
    _set_stage(sqlite_store, run_ref, "planning", "retry")  # the line noticed the lost worker
    _add_job(sqlite_store, run_ref, "development", "pending")
    detected = runner.poll(run_ref)
    assert [e.kind for e in detected.events] == [EventKind.FAULT_DETECTED]
    _set_stage(sqlite_store, run_ref, "planning", "succeeded")
    recovered = runner.poll(run_ref)
    assert EventKind.RECOVERED in [e.kind for e in recovered.events]

    _set_stage(sqlite_store, run_ref, "development", "waiting_human")
    human = runner.poll(run_ref)
    assert [e.kind for e in human.events if e.kind is EventKind.HUMAN_INTERVENTION]


def test_control_store_runner_cancel_fault_with_orchestrator(sqlite_store: Any, real_corpus: Corpus) -> None:
    from core.line.bindings import LINE_STAGES

    case = synthetic_case(real_corpus.get("E2E-API-NOTES"), "E2E-SYN-STORE")  # type: ignore[arg-type]
    corpus = Corpus([case])
    runner = _runner(sqlite_store)
    clock = FakeClock()
    ticks = {"n": 0}

    def line_simulator(seconds: float) -> None:
        """Stands in for the workers: advances the active run a little on every poll interval."""

        clock.sleep(seconds)
        ticks["n"] += 1
        [active] = sqlite_store.list_active_run_ids()
        if ticks["n"] == 1:
            _set_stage(sqlite_store, active, "grill", "running", cost=0.25)
        elif ticks["n"] >= 3 and _stage_status(sqlite_store, active, "grill") != "succeeded":
            _set_stage(sqlite_store, active, "grill", "succeeded", cost=0.5)
            _add_job(sqlite_store, active, "planning", "pending")
        elif ticks["n"] >= 5:
            _set_stage(sqlite_store, active, "planning", "succeeded")
            for stage in LINE_STAGES:
                if stage == "retrospective":
                    continue
                if _stage_status(sqlite_store, active, stage) is None:
                    _add_job(sqlite_store, active, stage, "succeeded")

    orchestrator = E2EOrchestrator(
        corpus,
        runner,
        OrchestratorConfig(system_id="store-sys", run_label="s", poll_interval_seconds=5.0),
        injector=ActionFaultInjector({FaultKind.WORKER_CRASH: FaultAction.CANCEL_RUN}),
        clock=clock,
        sleep=line_simulator,
    )
    trajectory = orchestrator.run_case(case)

    assert trajectory.final_state is FinalState.DELIVERED
    assert all(r.passed for r in trajectory.journey_results)
    order = kinds(trajectory)
    assert order.index(EventKind.FAULT_INJECTED) < order.index(EventKind.FAULT_DETECTED) < order.index(EventKind.RECOVERED)
    assert trajectory.cost_usd >= 0.75  # cancelled attempt (0.25) + the attempt that delivered (0.5)
    report = build_report(corpus, [trajectory], system_id="store-sys")
    assert report.receipts[0].recovery_status is RecoveryStatus.RECOVERED
    assert report.receipts[0].e2e_success and report.receipts[0].autonomous_success


def _stage_status(store: Any, run_id: str, stage: str) -> str | None:
    conn = store._connect()
    try:
        row = conn.execute("SELECT status FROM jobs WHERE run_id = ? AND stage = ?", (run_id, stage)).fetchone()
        return None if row is None else str(row["status"])
    finally:
        conn.close()


def test_control_store_runner_cancel_marks_run_cancelled(sqlite_store: Any, real_corpus: Corpus) -> None:
    from evals.e2e.corpus import agent_view

    case = real_corpus.get("E2E-API-NOTES")
    assert case is not None
    runner = _runner(sqlite_store)
    run_ref = runner.submit_case(agent_view(case))
    runner.cancel(run_ref)
    assert runner.poll(run_ref).phase is RunPhase.CANCELLED

