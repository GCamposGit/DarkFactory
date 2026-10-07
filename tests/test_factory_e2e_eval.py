"""USR-133: held-out end-to-end factory evaluation (corpus, replay, receipts, anti-leak)."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evals.e2e import cli as e2e_cli
from evals.e2e.corpus import (
    Corpus,
    CorpusError,
    agent_view,
    load_corpus,
    validate_cases,
    verify_manifest,
)
from evals.e2e.leakage import LeakError, LeakKind, LeakScanner
from evals.e2e.models import (
    E2ECase,
    EventKind,
    Expectation,
    FinalState,
    JourneyResult,
    Manifest,
    Trajectory,
    TrajectoryEvent,
)
from evals.e2e.oracle import (
    Observation,
    check_expectation,
    evaluate_journey,
    signed_body,
)
from evals.e2e.replay import (
    RecoveryStatus,
    RejectReason,
    ReplayError,
    build_report,
    compare_reports,
    load_trajectories,
    verify_report,
)

pytestmark = pytest.mark.offline


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


def case_dict(case: E2ECase) -> dict[str, Any]:
    return json.loads(case.model_dump_json())


def make_trajectory(
    case: E2ECase,
    trajectory_id: str,
    *,
    system_id: str = "sys-a",
    state: FinalState = FinalState.DELIVERED,
    failing_steps: tuple[str, ...] = (),
    duration: float = 600.0,
    cost: float = 1.5,
    events: list[tuple[float, EventKind, str]] | None = None,
    case_sha256: str | None = None,
) -> Trajectory:
    return Trajectory(
        trajectory_id=trajectory_id,
        case_id=case.case_id,
        case_sha256=case_sha256 or case.content_sha256(),
        system_id=system_id,
        final_state=state,
        duration_seconds=duration,
        cost_usd=cost,
        journey_results=[JourneyResult(step_id=s, passed=s not in failing_steps) for s in case.step_ids],
        events=[
            TrajectoryEvent(seq=index, t_seconds=t, kind=kind, detail=detail)
            for index, (t, kind, detail) in enumerate(events or [])
        ],
    )


RECOVERED_EVENTS = [
    (10.0, EventKind.AGENT_STEP, "plan"),
    (100.0, EventKind.FAULT_INJECTED, "fault"),
    (110.0, EventKind.FAULT_DETECTED, "detected"),
    (160.0, EventKind.RECOVERED, "resumed"),
]


# --------------------------------------------------------------------------- #
# Corpus                                                                       #
# --------------------------------------------------------------------------- #


def test_seed_corpus_is_small_but_real_and_distinct(corpus: Corpus) -> None:
    assert 6 <= len(corpus) <= 8
    assert len({c.product_kind for c in corpus.cases}) == len(corpus)
    assert len({c.product_id for c in corpus.cases}) == len(corpus)
    for case in corpus.cases:
        assert case.visibility.value == "reserved"
        assert len(case.journey) >= 4
        assert case.recovery.max_recovery_seconds > 0
        assert case.canary.startswith("DFE2E-CANARY-")


def test_manifest_matches_corpus_and_is_hash_only(corpus: Corpus) -> None:
    manifest_path = Path(__file__).resolve().parent.parent / "evals" / "e2e" / "manifest.json"
    raw = manifest_path.read_text(encoding="utf-8")
    manifest = Manifest.model_validate_json(raw)
    assert verify_manifest(corpus, manifest) == []
    scanner = LeakScanner(corpus.cases)
    assert scanner.scan_text(raw) == []  # the published manifest leaks nothing
    assert {e.case_id for e in manifest.entries} == {c.case_id for c in corpus.cases}


def test_journeys_are_public_interface_only(corpus: Corpus) -> None:
    base = case_dict(corpus.get("E2E-CLI-UNITS"))  # type: ignore[arg-type]
    for bad_args in (["convert", "1", "src/units.py", "mi"], ["/etc/passwd"], ["convert", "..\\x", "km", "mi"]):
        broken = json.loads(json.dumps(base))
        broken["journey"][0]["args"] = bad_args
        with pytest.raises(ValidationError, match="references the implementation"):
            E2ECase.model_validate(broken)
    api = case_dict(corpus.get("E2E-API-NOTES"))  # type: ignore[arg-type]
    api["journey"][0]["path"] = "/app/server.js"
    with pytest.raises(ValidationError, match="references the implementation"):
        E2ECase.model_validate(api)


def test_case_rejects_inconsistent_recovery_and_empty_expectation(corpus: Corpus) -> None:
    base = case_dict(corpus.get("E2E-API-NOTES"))  # type: ignore[arg-type]
    broken = json.loads(json.dumps(base))
    broken["recovery"]["inject_after_step"] = "no_such_step"
    with pytest.raises(ValidationError):
        E2ECase.model_validate(broken)
    broken = json.loads(json.dumps(base))
    broken["recovery"].update(phase="build", inject_after_step="create_first")
    with pytest.raises(ValidationError):
        E2ECase.model_validate(broken)
    with pytest.raises(ValidationError):
        Expectation()


def test_corpus_rejects_shared_journey_and_shared_seed_files(corpus: Corpus) -> None:
    donor = corpus.get("E2E-API-NOTES")
    assert donor is not None
    clone = case_dict(donor)
    clone.update(
        case_id="E2E-API-CLONE",
        product_id="notes-api-clone",
        product_kind="webhook_service",
        title="A clone with another name",
        canary="DFE2E-CANARY-0123456789abcdef",
    )
    with pytest.raises(CorpusError, match="share their acceptance journey"):
        validate_cases([donor, E2ECase.model_validate(clone)], require_distinct_kinds=False)

    batch = corpus.get("E2E-BATCH-SALES")
    assert batch is not None
    seeded = case_dict(corpus.get("E2E-CLI-UNITS"))  # type: ignore[arg-type]
    seeded.update(
        case_id="E2E-CLI-SEEDED",
        product_id="unitconv-seeded",
        product_kind="http_api",
        title="Other product reusing sales data",
        canary="DFE2E-CANARY-fedcba9876543210",
        seed_files={"data/shared.csv": batch.seed_files["sales.csv"]},
    )
    with pytest.raises(CorpusError, match="share seed file content"):
        validate_cases([batch, E2ECase.model_validate(seeded)], require_distinct_kinds=False)


def test_corpus_rejects_duplicate_kinds_and_ids(corpus: Corpus) -> None:
    first = corpus.get("E2E-API-NOTES")
    bot = corpus.get("E2E-BOT-EXPENSES")  # build-phase fault: no step reference to remap
    assert first is not None and bot is not None
    with pytest.raises(CorpusError, match="duplicate case_id"):
        validate_cases([first, first])
    same_kind = case_dict(bot)
    same_kind.update(
        case_id="E2E-DUP-KIND",
        product_id="other-product",
        title="Same kind different product",
        canary="DFE2E-CANARY-1111111111111111",
        product_kind=first.product_kind.value,
    )
    with pytest.raises(CorpusError, match="distinct product kinds"):
        validate_cases([first, E2ECase.model_validate(same_kind)])


def test_load_corpus_detects_hash_drift_and_bad_files(tmp_path: Path, corpus: Corpus) -> None:
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    for case in corpus.cases:
        (corpus_dir / f"{case.case_id}.json").write_text(case.model_dump_json(indent=2), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(corpus.build_manifest().model_dump_json(indent=2), encoding="utf-8")
    assert len(load_corpus(corpus_dir, manifest_path=manifest_path)) == len(corpus)

    tampered = case_dict(corpus.cases[0])
    tampered["spec"] = tampered["spec"] + " An extra sentence changes the hash of this case."
    (corpus_dir / f"{corpus.cases[0].case_id}.json").write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(CorpusError, match="hash drift"):
        load_corpus(corpus_dir, manifest_path=manifest_path)

    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(corpus.cases[1].content_sha256(), "0" * 64), encoding="utf-8"
    )
    with pytest.raises(CorpusError, match="invalid manifest"):
        load_corpus(corpus_dir, manifest_path=manifest_path)

    (corpus_dir / "E2E-BROKEN.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(CorpusError, match="invalid case file"):
        load_corpus(corpus_dir, manifest_path=None)


# --------------------------------------------------------------------------- #
# Executable oracle                                                            #
# --------------------------------------------------------------------------- #


class FakeExpenseBot:
    """Minimal black-box implementation used only to prove the oracle is executable."""

    def __init__(self, *, broken: bool = False) -> None:
        self.ledger: dict[str, list[tuple[str, int]]] = {}
        self.broken = broken

    def execute(self, step: Any) -> Observation:
        parts = step.message.split()
        items = self.ledger.setdefault(step.user, [])
        if parts[0] == "/add":
            try:
                cents = round(float(parts[1]) * 100)
            except ValueError:
                return Observation(text="Invalid amount")
            items.append((parts[2], cents))
            return Observation(text=f"Added {cents / 100:.2f} to {parts[2]}")
        if parts[0] == "/total":
            if len(parts) == 2:
                return Observation(text=f"{parts[1]}: {sum(c for k, c in items if k == parts[1]) / 100:.2f}")
            shown = sum(c for _, c in items) / 100 + (1 if self.broken else 0)
            return Observation(text=f"Total: {shown:.2f}")
        if parts[0] == "/undo":
            kind, cents = items.pop()
            return Observation(text=f"Removed {cents / 100:.2f} {kind}")
        return Observation(text="Unknown command")


class FakeWebhook:
    def __init__(self) -> None:
        self.seen: set[str] = set()

    def execute(self, step: Any) -> Observation:
        if step.path == "/stats":
            return Observation(status=200, json_body={"received": len(self.seen)})
        body, headers = signed_body(step)
        expected = hmac.new(b"test-secret-1", body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(headers.get("X-Signature", ""), expected):
            return Observation(status=401)
        event_id = step.json_body["id"]
        if event_id in self.seen:
            return Observation(status=200, json_body={"duplicate": True})
        self.seen.add(event_id)
        return Observation(status=202, json_body={"accepted": True})


def test_oracle_journey_is_executable_against_a_black_box(corpus: Corpus) -> None:
    bot_case = corpus.get("E2E-BOT-EXPENSES")
    assert bot_case is not None
    assert all(r.passed for r in evaluate_journey(bot_case, FakeExpenseBot()))
    failed = [r.step_id for r in evaluate_journey(bot_case, FakeExpenseBot(broken=True)) if not r.passed]
    assert "total_all" in failed

    webhook_case = corpus.get("E2E-WEBHOOK-COUNTER")
    assert webhook_case is not None
    assert all(r.passed for r in evaluate_journey(webhook_case, FakeWebhook()))


def test_driver_crash_fails_the_step_not_the_evaluator(corpus: Corpus) -> None:
    class Crashing:
        def execute(self, step: Any) -> Observation:
            raise RuntimeError("product exploded")

    case = corpus.cases[0]
    results = evaluate_journey(case, Crashing())
    assert [r.step_id for r in results] == case.step_ids
    assert not any(r.passed for r in results)


def test_check_expectation_codes_are_content_free() -> None:
    expect = Expectation(
        exit_code=0,
        status=200,
        contains=["alpha"],
        not_contains=["beta"],
        regex=["^x+$"],
        error_contains=["warn"],
        json_subset={"a": {"b": 1}},
    )
    good = Observation(exit_code=0, status=200, text="alpha\nxx", error_text="a warn", json_body={"a": {"b": 1, "c": 2}})
    assert check_expectation(expect, good) == []
    bad = Observation(exit_code=1, status=500, text="beta", error_text="", json_body={"a": {"b": True}})
    assert check_expectation(expect, bad) == [
        "exit_code",
        "status",
        "contains[0]",
        "not_contains[0]",
        "regex[0]",
        "error_contains[0]",
        "json_subset",
    ]


# --------------------------------------------------------------------------- #
# Replay metrics                                                               #
# --------------------------------------------------------------------------- #


def test_replay_measures_success_intervention_time_cost_and_recovery(corpus: Corpus) -> None:
    a, b, c, d, e = corpus.cases[:5]
    runs = [
        make_trajectory(a, "run-a", duration=200, cost=1.0, events=RECOVERED_EVENTS),
        make_trajectory(b, "run-b", state=FinalState.TIMEOUT, duration=300, cost=3.0),
        make_trajectory(
            c,
            "run-c",
            failing_steps=(c.step_ids[-1],),
            duration=200,
            cost=2.0,
            events=[(5.0, EventKind.HUMAN_INTERVENTION, "owner unblocked")],
        ),
        make_trajectory(
            d,
            "run-d",
            duration=400,
            cost=4.0,
            events=[
                (50.0, EventKind.FAULT_INJECTED, "fault"),
                (60.0, EventKind.FAULT_DETECTED, "seen"),
                (70.0, EventKind.HUMAN_INTERVENTION, "owner helped"),
                (90.0, EventKind.RECOVERED, "fixed"),
            ],
        ),
        make_trajectory(
            e,
            "run-e",
            duration=500,
            cost=5.0,
            events=[
                (50.0, EventKind.FAULT_INJECTED, "fault"),
                (60.0, EventKind.FAULT_DETECTED, "seen"),
            ],
        ),
    ]
    report = build_report(corpus, runs)
    s = report.summary
    assert s.runs == 5 and s.rejected_runs == 0
    assert (s.e2e_success_runs, s.e2e_success_rate) == (3, 0.6)  # a, d, e delivered with all steps passing
    assert (s.autonomous_success_runs, s.autonomous_success_rate) == (2, 0.4)
    assert (s.interventions_total, s.runs_with_intervention, s.intervention_rate) == (2, 2, 0.4)
    assert s.duration_mean_seconds == 320.0 and s.duration_median_seconds == 300.0 and s.duration_p90_seconds == 500.0
    assert s.cost_total_usd == 15.0 and s.cost_mean_usd == 3.0 and s.cost_per_success_usd == 5.0
    assert (s.recovery_exercised, s.recovery_recovered, s.recovery_human_assisted, s.recovery_failed) == (3, 1, 1, 1)
    assert s.recovery_not_exercised == 2
    assert s.recovery_rate == round(1 / 3, 6)
    assert s.recovery_seconds_mean == 60.0
    assert s.cases_total == len(corpus) and s.cases_covered == 5
    assert [m.case_id for m in report.missing_cases] == [corpus.cases[5].case_id]
    statuses = {r.trajectory_id: r.recovery_status for r in report.receipts}
    assert statuses["run-a"] is RecoveryStatus.RECOVERED
    assert statuses["run-d"] is RecoveryStatus.HUMAN_ASSISTED
    assert statuses["run-e"] is RecoveryStatus.FAILED
    assert statuses["run-b"] is RecoveryStatus.NOT_EXERCISED


def test_recovery_beyond_deadline_or_without_detection_fails(corpus: Corpus) -> None:
    case = corpus.cases[0]
    late = [
        (10.0, EventKind.FAULT_INJECTED, "f"),
        (20.0, EventKind.FAULT_DETECTED, "d"),
        (10.0 + case.recovery.max_recovery_seconds + 1, EventKind.RECOVERED, "r"),
    ]
    undetected = [(10.0, EventKind.FAULT_INJECTED, "f"), (30.0, EventKind.RECOVERED, "r")]
    report = build_report(
        corpus,
        [
            make_trajectory(case, "late-run", duration=5000, events=late),
            make_trajectory(case, "blind-run", duration=5000, events=undetected),
        ],
    )
    assert {r.recovery_status for r in report.receipts} == {RecoveryStatus.FAILED}
    assert report.summary.recovery_rate == 0.0


def test_budget_overrun_is_reported_without_changing_success(corpus: Corpus) -> None:
    case = corpus.cases[0]
    run = make_trajectory(case, "run-over", duration=case.time_budget_seconds + 1)
    report = build_report(corpus, [run])
    assert report.summary.e2e_success_runs == 1 and report.summary.budget_exceeded_runs == 1


def test_replay_is_deterministic_and_receipts_are_hashed(corpus: Corpus) -> None:
    runs = [make_trajectory(c, f"run-{c.case_id}", events=RECOVERED_EVENTS) for c in corpus.cases]
    first = build_report(corpus, runs)
    second = build_report(corpus, list(reversed(runs)))
    assert first.model_dump_json() == second.model_dump_json()
    assert first.report_sha256 == second.report_sha256
    assert verify_report(first) == []
    assert first.manifest_sha256 == corpus.build_manifest().manifest_sha256
    for receipt, run in zip(first.receipts, sorted(runs, key=lambda t: (t.case_id, t.trajectory_id))):
        assert receipt.trajectory_sha256 == hashlib.sha256(
            json.dumps(run.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        assert receipt.case_sha256 == corpus.get(receipt.case_id).content_sha256()  # type: ignore[union-attr]
    assert first.summary.coverage == 1.0 and not first.missing_cases


def test_verify_report_detects_tampering(corpus: Corpus) -> None:
    report = build_report(corpus, [make_trajectory(corpus.cases[0], "run-1")])
    forged_receipt = report.receipts[0].model_copy(update={"e2e_success": False})
    assert verify_report(report.model_copy(update={"receipts": [forged_receipt]}))
    forged_summary = report.summary.model_copy(update={"e2e_success_rate": 0.0})
    assert "report hash mismatch" in verify_report(report.model_copy(update={"summary": forged_summary}))


def test_invalid_runs_are_failed_receipts_not_silent_successes(corpus: Corpus) -> None:
    case = corpus.cases[0]
    good = make_trajectory(case, "run-good")
    stale = make_trajectory(case, "run-stale", case_sha256="1" * 64)
    unknown = Trajectory(
        trajectory_id="run-unknown",
        case_id="E2E-NOT-IN-CORPUS",
        case_sha256="2" * 64,
        system_id="sys-a",
        final_state=FinalState.DELIVERED,
        duration_seconds=1,
        cost_usd=0,
    )
    wrong_step = good.model_copy(
        update={"trajectory_id": "run-step", "journey_results": [JourneyResult(step_id="ghost", passed=True)]}
    )
    duplicate = make_trajectory(corpus.cases[1], "run-good")
    report = build_report(corpus, [good, stale, unknown, wrong_step, duplicate])
    reasons = {r.trajectory_id: r.rejected_reason for r in report.receipts}
    assert reasons["run-stale"] is RejectReason.STALE_CASE_HASH
    assert reasons["run-unknown"] is RejectReason.UNKNOWN_CASE
    assert reasons["run-step"] is RejectReason.UNKNOWN_STEP_ID
    assert RejectReason.DUPLICATE_TRAJECTORY_ID in reasons.values()
    assert report.summary.rejected_runs == 4
    assert report.summary.e2e_success_runs == 1 and report.summary.runs == 5
    assert report.summary.recovery_exercised == 0  # rejected runs never count as recoveries


def test_system_selection_is_explicit(corpus: Corpus) -> None:
    a = make_trajectory(corpus.cases[0], "run-1", system_id="sys-a")
    b = make_trajectory(corpus.cases[0], "run-2", system_id="sys-b")
    with pytest.raises(ReplayError, match="system_id is required"):
        build_report(corpus, [a, b])
    assert build_report(corpus, [a, b], system_id="sys-b").summary.runs == 1
    with pytest.raises(ReplayError):
        build_report(corpus, [])


def test_compare_reports_requires_same_corpus_and_lists_case_changes(corpus: Corpus) -> None:
    base_runs = [make_trajectory(c, f"b-{c.case_id}", system_id="base", cost=2.0) for c in corpus.cases]
    cand_runs = [
        make_trajectory(c, f"c-{c.case_id}", system_id="cand", cost=1.0, failing_steps=(c.step_ids[0],) if i == 0 else ())
        for i, c in enumerate(corpus.cases)
    ]
    base = build_report(corpus, base_runs)
    cand = build_report(corpus, cand_runs)
    comparison = compare_reports(base, cand)
    assert comparison.deltas["cost_mean_usd"] == -1.0
    assert comparison.deltas["e2e_success_rate"] == round(5 / 6 - 1, 6)
    assert [c.case_id for c in comparison.case_changes] == [corpus.cases[0].case_id]

    other = base.model_copy(update={"manifest_sha256": "3" * 64})
    with pytest.raises(ReplayError):  # fails integrity first (hash no longer matches)
        compare_reports(other, cand)


# --------------------------------------------------------------------------- #
# Anti-leak                                                                    #
# --------------------------------------------------------------------------- #


def _leaky_trajectory(corpus: Corpus, trajectory_id: str, detail: str) -> Trajectory:
    return make_trajectory(corpus.cases[0], trajectory_id, events=[(1.0, EventKind.AGENT_STEP, detail)])


def test_trajectory_with_canary_spec_or_oracle_text_is_rejected_without_echo(corpus: Corpus) -> None:
    case = corpus.get("E2E-API-NOTES")
    assert case is not None
    spec_sentence = "The list endpoint returns a JSON array ordered by id ascending and filters on a"
    oracle_literal = "ship v1 in autumn"  # only present in the oracle, not in the brief
    assert len(oracle_literal) >= 12
    runs = [
        _leaky_trajectory(corpus, "leak-canary", f"saw {case.canary} in a file"),
        _leaky_trajectory(corpus, "leak-spec", spec_sentence),
        _leaky_trajectory(corpus, "leak-oracle", f"body: {oracle_literal}"),
        make_trajectory(case, "clean-run"),
    ]
    report = build_report(corpus, runs)
    reasons = {r.trajectory_id: r.rejected_reason for r in report.receipts}
    assert {reasons[k] for k in ("leak-canary", "leak-spec", "leak-oracle")} == {RejectReason.LEAK_DETECTED}
    assert reasons["clean-run"] is None
    kinds = {f.kind for f in report.leak_findings}
    assert kinds == {LeakKind.CANARY, LeakKind.SPEC_NGRAM, LeakKind.ORACLE_LITERAL}
    dumped = report.model_dump_json()
    for secret in (case.canary, spec_sentence, oracle_literal, case.spec[:40]):
        assert secret not in dumped
    assert all(f.case_id == case.case_id and len(f.case_sha256_prefix) == 12 for f in report.leak_findings)


def test_report_self_check_blocks_reserved_content(corpus: Corpus) -> None:
    case = corpus.cases[0]
    run = make_trajectory(case, "run-1", system_id=f"sys-{case.canary}")
    with pytest.raises(LeakError):
        build_report(corpus, [run])


def test_agent_never_sees_spec_or_oracle(corpus: Corpus) -> None:
    scanner = LeakScanner(corpus.cases)
    for case in corpus.cases:
        view = agent_view(case)
        assert set(view.model_dump()) == {"case_id", "brief"}
        assert scanner.scan_value(view.model_dump()) == []
        assert case.spec not in view.brief
        assert case.canary not in view.brief


def test_scanner_ignores_public_text_and_short_literals(corpus: Corpus) -> None:
    scanner = LeakScanner(corpus.cases)
    for case in corpus.cases:
        assert scanner.scan_text(case.brief) == []
    assert scanner.scan_text("status 200 ok") == []
    assert scanner.scan_text("") == []


def test_scanner_skips_public_cases_by_default(corpus: Corpus) -> None:
    case = corpus.cases[0]
    public = E2ECase.model_validate({**case_dict(case), "visibility": "public"})
    assert LeakScanner([public]).scan_text(case.canary) == []
    assert LeakScanner([public], only_reserved=False).scan_text(case.canary)


# --------------------------------------------------------------------------- #
# Loading and CLI                                                              #
# --------------------------------------------------------------------------- #


def test_load_trajectories_and_cli_replay(tmp_path: Path, corpus: Corpus, capsys: pytest.CaptureFixture[str]) -> None:
    runs = [make_trajectory(c, f"cli-{c.case_id}", events=RECOVERED_EVENTS) for c in corpus.cases]
    directory = tmp_path / "runs"
    directory.mkdir()
    (directory / "a.json").write_text(json.dumps([r.model_dump(mode="json") for r in runs[:2]]), encoding="utf-8")
    (directory / "b.jsonl").write_text(
        "\n".join(json.dumps(r.model_dump(mode="json")) for r in runs[2:]) + "\n", encoding="utf-8"
    )
    assert len(load_trajectories(directory)) == len(runs)

    out = tmp_path / "report.json"
    code = e2e_cli.main(["replay", "--trajectories", str(directory), "--out", str(out)])
    captured = capsys.readouterr().out
    assert code == 0 and "report_sha256=" in captured
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["summary"]["e2e_success_rate"] == 1.0
    assert LeakScanner(corpus.cases).scan_text(captured + out.read_text(encoding="utf-8")) == []

    assert e2e_cli.main(["verify"]) == 0
    assert e2e_cli.main(["manifest"]) == 0


def test_cli_reports_errors_without_traceback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = tmp_path / "bad.json"
    broken.write_text("{nope", encoding="utf-8")
    assert e2e_cli.main(["replay", "--trajectories", str(broken)]) == 2
    assert "invalid trajectory file" in capsys.readouterr().err
    assert e2e_cli.main(["--corpus", str(tmp_path / "missing"), "verify"]) == 2


def test_trajectory_validation_rejects_disordered_events() -> None:
    base = {
        "trajectory_id": "traj-1",
        "case_id": "E2E-API-NOTES",
        "case_sha256": "a" * 64,
        "system_id": "sys-a",
        "final_state": "delivered",
        "duration_seconds": 10,
        "cost_usd": 0,
    }
    with pytest.raises(ValidationError):
        Trajectory.model_validate({**base, "events": [{"seq": 1, "t_seconds": 5, "kind": "agent_step"}, {"seq": 1, "t_seconds": 6, "kind": "agent_step"}]})
    with pytest.raises(ValidationError):
        Trajectory.model_validate({**base, "events": [{"seq": 0, "t_seconds": 11, "kind": "agent_step"}]})
    with pytest.raises(ValidationError):
        Trajectory.model_validate({**base, "journey_results": [{"step_id": "a", "passed": True}, {"step_id": "a", "passed": False}]})
