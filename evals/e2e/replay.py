"""Deterministic replay runner: recorded trajectories in, hashed report out.

No model, network or clock is touched: the same corpus and trajectories always
produce a byte-identical report.  Reports reference reserved cases by id and
hash only, and are scanned for reserved content before being sealed.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from collections import defaultdict
from collections.abc import Iterable
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evals.e2e.corpus import Corpus
from evals.e2e.leakage import LeakFinding, LeakScanner
from evals.e2e.models import (
    CASE_ID_PATTERN,
    SCHEMA_VERSION,
    SHA256_PATTERN,
    E2ECase,
    EventKind,
    FinalState,
    Trajectory,
    sha256_hex,
)

logger = logging.getLogger(__name__)


class ReplayError(ValueError):
    """Replay input is unusable (corrupt trajectory file, ambiguous system, ...)."""


class RecoveryStatus(str, Enum):
    RECOVERED = "recovered"
    HUMAN_ASSISTED = "human_assisted"
    FAILED = "failed"
    NOT_EXERCISED = "not_exercised"
    REJECTED = "rejected"


class RejectReason(str, Enum):
    UNKNOWN_CASE = "unknown_case"
    STALE_CASE_HASH = "stale_case_hash"
    UNKNOWN_STEP_ID = "unknown_step_id"
    DUPLICATE_TRAJECTORY_ID = "duplicate_trajectory_id"
    LEAK_DETECTED = "leak_detected"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Receipt(_Frozen):
    """Per-run evidence: binds a verdict to the exact case and trajectory bytes."""

    case_id: str = Field(pattern=CASE_ID_PATTERN)
    case_sha256: str = Field(pattern=SHA256_PATTERN)
    trajectory_id: str
    trajectory_sha256: str = Field(pattern=SHA256_PATTERN)
    e2e_success: bool
    autonomous_success: bool
    interventions: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    cost_usd: float = Field(ge=0)
    within_budget: bool
    recovery_status: RecoveryStatus
    recovery_seconds: float | None = None
    rejected_reason: RejectReason | None = None
    receipt_sha256: str = Field(pattern=SHA256_PATTERN)

    @staticmethod
    def seal(payload: dict[str, object]) -> str:
        body = {key: value for key, value in payload.items() if key != "receipt_sha256"}
        return sha256_hex(body)


class CaseRef(_Frozen):
    case_id: str = Field(pattern=CASE_ID_PATTERN)
    case_sha256: str = Field(pattern=SHA256_PATTERN)


class CaseSummary(_Frozen):
    case_id: str = Field(pattern=CASE_ID_PATTERN)
    case_sha256: str = Field(pattern=SHA256_PATTERN)
    runs: int = Field(ge=0)
    successes: int = Field(ge=0)


class Summary(_Frozen):
    runs: int
    rejected_runs: int
    e2e_success_runs: int
    e2e_success_rate: float | None
    autonomous_success_runs: int
    autonomous_success_rate: float | None
    interventions_total: int
    runs_with_intervention: int
    intervention_rate: float | None
    duration_mean_seconds: float | None
    duration_median_seconds: float | None
    duration_p90_seconds: float | None
    cost_total_usd: float
    cost_mean_usd: float | None
    cost_per_success_usd: float | None
    budget_exceeded_runs: int
    recovery_exercised: int
    recovery_recovered: int
    recovery_human_assisted: int
    recovery_failed: int
    recovery_not_exercised: int
    recovery_rate: float | None
    recovery_seconds_mean: float | None
    cases_total: int
    cases_covered: int
    coverage: float


class E2EReport(_Frozen):
    schema_version: str = SCHEMA_VERSION
    corpus_id: str
    corpus_version: str
    manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    system_id: str
    summary: Summary
    per_case: list[CaseSummary]
    missing_cases: list[CaseRef]
    receipts: list[Receipt]
    leak_findings: list[LeakFinding]
    report_sha256: str = Field(pattern=SHA256_PATTERN)

    @staticmethod
    def seal(payload: dict[str, object]) -> str:
        return sha256_hex({key: value for key, value in payload.items() if key != "report_sha256"})


# --------------------------------------------------------------------------- #
# Loading                                                                      #
# --------------------------------------------------------------------------- #


def load_trajectories(path: Path | str) -> list[Trajectory]:
    """Load trajectories from a ``.json`` (object or list), ``.jsonl`` or a directory of both."""

    source = Path(path)
    files = sorted([*source.glob("*.json"), *source.glob("*.jsonl")]) if source.is_dir() else [source]
    if not files:
        raise ReplayError("no trajectory files found")
    result: list[Trajectory] = []
    for file in files:
        try:
            text = file.read_text(encoding="utf-8")
            if file.suffix == ".jsonl":
                items = [json.loads(line) for line in text.splitlines() if line.strip()]
            else:
                loaded = json.loads(text)
                items = loaded if isinstance(loaded, list) else [loaded]
            result.extend(Trajectory.model_validate(item) for item in items)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise ReplayError(f"invalid trajectory file {file.name}: {type(exc).__name__}") from None
    return result


# --------------------------------------------------------------------------- #
# Per-run evaluation                                                           #
# --------------------------------------------------------------------------- #


def _is_success(case: E2ECase, trajectory: Trajectory) -> bool:
    if trajectory.final_state is not FinalState.DELIVERED:
        return False
    passed = {item.step_id: item.passed for item in trajectory.journey_results}
    return all(passed.get(step_id) is True for step_id in case.step_ids)


def _recovery(case: E2ECase, trajectory: Trajectory, success: bool) -> tuple[RecoveryStatus, float | None]:
    events = trajectory.events
    fault = next((e for e in events if e.kind is EventKind.FAULT_INJECTED), None)
    if fault is None:
        return RecoveryStatus.NOT_EXERCISED, None
    detected = next((e for e in events if e.kind is EventKind.FAULT_DETECTED and e.seq > fault.seq), None)
    if detected is None:
        return RecoveryStatus.FAILED, None
    recovered = next((e for e in events if e.kind is EventKind.RECOVERED and e.seq > detected.seq), None)
    if recovered is None:
        return RecoveryStatus.FAILED, None
    seconds = round(recovered.t_seconds - fault.t_seconds, 6)
    if not success or seconds > case.recovery.max_recovery_seconds:
        return RecoveryStatus.FAILED, seconds
    assisted = any(e.kind is EventKind.HUMAN_INTERVENTION and e.seq > fault.seq for e in events)
    return (RecoveryStatus.HUMAN_ASSISTED if assisted else RecoveryStatus.RECOVERED), seconds


def _make_receipt(
    case: E2ECase | None,
    trajectory: Trajectory,
    *,
    reject: RejectReason | None,
) -> Receipt:
    interventions = sum(1 for e in trajectory.events if e.kind is EventKind.HUMAN_INTERVENTION)
    success = False
    status = RecoveryStatus.REJECTED
    recovery_seconds: float | None = None
    within_budget = False
    if reject is None and case is not None:
        success = _is_success(case, trajectory)
        status, recovery_seconds = _recovery(case, trajectory, success)
        within_budget = (
            trajectory.duration_seconds <= case.time_budget_seconds and trajectory.cost_usd <= case.cost_budget_usd
        )
    payload: dict[str, object] = {
        "case_id": trajectory.case_id,
        "case_sha256": trajectory.case_sha256,
        "trajectory_id": trajectory.trajectory_id,
        "trajectory_sha256": trajectory.content_sha256(),
        "e2e_success": success,
        "autonomous_success": success and interventions == 0,
        "interventions": interventions,
        "duration_seconds": trajectory.duration_seconds,
        "cost_usd": trajectory.cost_usd,
        "within_budget": within_budget,
        "recovery_status": status.value,
        "recovery_seconds": recovery_seconds,
        "rejected_reason": reject.value if reject else None,
    }
    payload["receipt_sha256"] = Receipt.seal(payload)
    return Receipt.model_validate(payload)


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(math.ceil(0.9 * len(ordered)) - 1, 0)], 6)


def _summarize(receipts: list[Receipt], cases_total: int, cases_covered: int) -> Summary:
    runs = len(receipts)
    successes = sum(r.e2e_success for r in receipts)
    durations = [r.duration_seconds for r in receipts]
    cost_total = round(sum(r.cost_usd for r in receipts), 6)
    by_status: dict[RecoveryStatus, int] = defaultdict(int)
    for receipt in receipts:
        by_status[receipt.recovery_status] += 1
    exercised = sum(by_status[s] for s in (RecoveryStatus.RECOVERED, RecoveryStatus.HUMAN_ASSISTED, RecoveryStatus.FAILED))
    recovery_secs = [r.recovery_seconds for r in receipts if r.recovery_status is RecoveryStatus.RECOVERED and r.recovery_seconds is not None]
    with_intervention = sum(1 for r in receipts if r.interventions > 0)
    return Summary(
        runs=runs,
        rejected_runs=sum(1 for r in receipts if r.rejected_reason is not None),
        e2e_success_runs=successes,
        e2e_success_rate=_rate(successes, runs),
        autonomous_success_runs=sum(r.autonomous_success for r in receipts),
        autonomous_success_rate=_rate(sum(r.autonomous_success for r in receipts), runs),
        interventions_total=sum(r.interventions for r in receipts),
        runs_with_intervention=with_intervention,
        intervention_rate=_rate(with_intervention, runs),
        duration_mean_seconds=round(statistics.fmean(durations), 6) if durations else None,
        duration_median_seconds=round(statistics.median(durations), 6) if durations else None,
        duration_p90_seconds=_p90(durations),
        cost_total_usd=cost_total,
        cost_mean_usd=round(cost_total / runs, 6) if runs else None,
        cost_per_success_usd=round(cost_total / successes, 6) if successes else None,
        budget_exceeded_runs=sum(1 for r in receipts if r.rejected_reason is None and not r.within_budget),
        recovery_exercised=exercised,
        recovery_recovered=by_status[RecoveryStatus.RECOVERED],
        recovery_human_assisted=by_status[RecoveryStatus.HUMAN_ASSISTED],
        recovery_failed=by_status[RecoveryStatus.FAILED],
        recovery_not_exercised=by_status[RecoveryStatus.NOT_EXERCISED],
        recovery_rate=_rate(by_status[RecoveryStatus.RECOVERED], exercised),
        recovery_seconds_mean=round(statistics.fmean(recovery_secs), 6) if recovery_secs else None,
        cases_total=cases_total,
        cases_covered=cases_covered,
        coverage=round(cases_covered / cases_total, 6) if cases_total else 0.0,
    )


# --------------------------------------------------------------------------- #
# Report                                                                       #
# --------------------------------------------------------------------------- #


def build_report(
    corpus: Corpus,
    trajectories: Iterable[Trajectory],
    *,
    system_id: str | None = None,
) -> E2EReport:
    """Replay ``trajectories`` of one system against ``corpus``.

    Invalid runs (unknown/stale case, unknown step, duplicate id, leaked
    reserved content) are kept as failed receipts so they cannot inflate rates.
    """

    items = list(trajectories)
    systems = sorted({t.system_id for t in items})
    if system_id is None:
        if len(systems) != 1:
            raise ReplayError("system_id is required unless trajectories come from exactly one system")
        system_id = systems[0]
    items = sorted((t for t in items if t.system_id == system_id), key=lambda t: (t.case_id, t.trajectory_id))

    scanner = LeakScanner(corpus.cases)
    receipts: list[Receipt] = []
    leaks: list[LeakFinding] = []
    seen_ids: set[str] = set()
    for trajectory in items:
        case = corpus.get(trajectory.case_id)
        reject: RejectReason | None = None
        if case is None:
            reject = RejectReason.UNKNOWN_CASE
        elif trajectory.case_sha256 != case.content_sha256():
            reject = RejectReason.STALE_CASE_HASH
        elif not {r.step_id for r in trajectory.journey_results} <= set(case.step_ids):
            reject = RejectReason.UNKNOWN_STEP_ID
        elif trajectory.trajectory_id in seen_ids:
            reject = RejectReason.DUPLICATE_TRAJECTORY_ID
        if reject is None:
            findings = scanner.scan_value(trajectory.model_dump(mode="json"), location=f"trajectory:{trajectory.trajectory_id}")
            if findings:
                leaks.extend(findings)
                reject = RejectReason.LEAK_DETECTED
        seen_ids.add(trajectory.trajectory_id)
        receipts.append(_make_receipt(case, trajectory, reject=reject))
        if reject is not None:
            logger.warning("trajectory %s rejected: %s", trajectory.trajectory_id, reject.value)

    runs_by_case: dict[str, list[Receipt]] = defaultdict(list)
    for receipt in receipts:
        runs_by_case[receipt.case_id].append(receipt)
    per_case: list[CaseSummary] = []
    missing: list[CaseRef] = []
    for case in corpus.cases:
        sha = case.content_sha256()
        valid = [r for r in runs_by_case.get(case.case_id, []) if r.rejected_reason is None]
        if valid:
            per_case.append(
                CaseSummary(case_id=case.case_id, case_sha256=sha, runs=len(valid), successes=sum(r.e2e_success for r in valid))
            )
        else:
            missing.append(CaseRef(case_id=case.case_id, case_sha256=sha))

    payload = {
        "schema_version": SCHEMA_VERSION,
        "corpus_id": corpus.corpus_id,
        "corpus_version": corpus.corpus_version,
        "manifest_sha256": corpus.build_manifest().manifest_sha256,
        "system_id": system_id,
        "summary": _summarize(receipts, len(corpus), len(per_case)).model_dump(mode="json"),
        "per_case": [c.model_dump(mode="json") for c in per_case],
        "missing_cases": [c.model_dump(mode="json") for c in missing],
        "receipts": [r.model_dump(mode="json") for r in receipts],
        "leak_findings": [f.model_dump(mode="json") for f in leaks],
    }
    # Fail closed: a report must never carry reserved text (ids/hashes only).
    scanner.assert_clean(payload, location="report")
    payload["report_sha256"] = E2EReport.seal(payload)
    return E2EReport.model_validate(payload)


def verify_report(report: E2EReport) -> list[str]:
    """Recompute every receipt hash and the report hash; return problems (empty = intact)."""

    problems: list[str] = []
    for receipt in report.receipts:
        if Receipt.seal(receipt.model_dump(mode="json")) != receipt.receipt_sha256:
            problems.append(f"receipt {receipt.trajectory_id} hash mismatch")
    if E2EReport.seal(report.model_dump(mode="json")) != report.report_sha256:
        problems.append("report hash mismatch")
    return problems


# --------------------------------------------------------------------------- #
# Comparison                                                                   #
# --------------------------------------------------------------------------- #

_COMPARED_METRICS = (
    "e2e_success_rate",
    "autonomous_success_rate",
    "intervention_rate",
    "duration_median_seconds",
    "cost_mean_usd",
    "cost_per_success_usd",
    "recovery_rate",
    "coverage",
)


class CaseChange(_Frozen):
    case_id: str
    case_sha256: str
    base_successes: int
    base_runs: int
    candidate_successes: int
    candidate_runs: int


class Comparison(_Frozen):
    manifest_sha256: str
    base_system: str
    candidate_system: str
    deltas: dict[str, float | None]
    case_changes: list[CaseChange]


def compare_reports(base: E2EReport, candidate: E2EReport) -> Comparison:
    """Compare two sealed reports over the same corpus version, by ids and hashes only."""

    for report in (base, candidate):
        problems = verify_report(report)
        if problems:
            raise ReplayError("report failed integrity check: " + "; ".join(problems))
    if base.manifest_sha256 != candidate.manifest_sha256:
        raise ReplayError("reports were produced against different corpus manifests")
    deltas: dict[str, float | None] = {}
    for metric in _COMPARED_METRICS:
        left = getattr(base.summary, metric)
        right = getattr(candidate.summary, metric)
        deltas[metric] = None if left is None or right is None else round(right - left, 6)
    base_cases = {c.case_id: c for c in base.per_case}
    cand_cases = {c.case_id: c for c in candidate.per_case}
    changes: list[CaseChange] = []
    for case_id in sorted(set(base_cases) | set(cand_cases)):
        left_case, right_case = base_cases.get(case_id), cand_cases.get(case_id)
        left_pair = (left_case.successes, left_case.runs) if left_case else (0, 0)
        right_pair = (right_case.successes, right_case.runs) if right_case else (0, 0)
        if left_pair != right_pair:
            sha = (left_case or right_case).case_sha256  # type: ignore[union-attr]
            changes.append(
                CaseChange(
                    case_id=case_id,
                    case_sha256=sha,
                    base_successes=left_pair[0],
                    base_runs=left_pair[1],
                    candidate_successes=right_pair[0],
                    candidate_runs=right_pair[1],
                )
            )
    return Comparison(
        manifest_sha256=base.manifest_sha256,
        base_system=base.system_id,
        candidate_system=candidate.system_id,
        deltas=deltas,
        case_changes=changes,
    )
