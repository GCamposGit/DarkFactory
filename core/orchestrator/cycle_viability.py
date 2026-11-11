"""Estimate the remaining cycle and reserve review quota before BUILD (USR-493).

Admission asks whether the predicted cost of PLAN, BUILD, correction and REVIEW
still fits in the harness headroom. BUILD starts only after an atomic reservation
of the review slice. A stage without history uses the configured fallback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

_STAGE_ALIASES = {
    "PLAN": "plan",
    "PLANNING": "plan",
    "BUILD": "build",
    "DEVELOPMENT": "build",
    "CORRECTION": "correction",
    "CORRECAO": "correction",
    "FIX": "correction",
    "REVIEW": "review",
}

_FALLBACK_ENV = {
    "plan": "ORCHESTRATOR_CYCLE_FALLBACK_PLAN",
    "build": "ORCHESTRATOR_CYCLE_FALLBACK_BUILD",
    "correction": "ORCHESTRATOR_CYCLE_FALLBACK_CORRECTION",
    "review": "ORCHESTRATOR_CYCLE_FALLBACK_REVIEW",
}

_FALLBACK_DEFAULTS = {
    "plan": 1.0,
    "build": 2.0,
    "correction": 1.0,
    "review": 1.0,
}


@dataclass(frozen=True)
class StageEstimate:
    """Headroom points required for the rest of one cycle."""

    plan: float
    build: float
    correction: float
    review: float
    sources: Mapping[str, str]

    def __post_init__(self) -> None:
        for name in ("plan", "build", "correction", "review"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cost cannot be negative")

    @property
    def total(self) -> float:
        return self.plan + self.build + self.correction + self.review

    def as_dict(self) -> dict[str, float]:
        return {
            "plan": self.plan,
            "build": self.build,
            "correction": self.correction,
            "review": self.review,
            "total": self.total,
        }


@dataclass(frozen=True)
class BuildGuard:
    admitted: bool
    reason: str
    reservation_id: Optional[str] = None
    required_total: float = 0.0
    review_percent: float = 0.0
    available: float = 0.0


def fallback_estimate(environment: Optional[Mapping[str, str]] = None) -> StageEstimate:
    """Configured cost used when a stage has no positive historical sample."""

    source = os.environ if environment is None else environment
    values: dict[str, float] = {}
    for stage, variable in _FALLBACK_ENV.items():
        raw = source.get(variable)
        if raw is None or str(raw).strip() == "":
            values[stage] = _FALLBACK_DEFAULTS[stage]
            continue
        parsed = float(str(raw).strip())
        if parsed < 0:
            raise ValueError(f"{variable} cannot be negative")
        values[stage] = parsed
    return StageEstimate(
        plan=values["plan"],
        build=values["build"],
        correction=values["correction"],
        review=values["review"],
        sources={stage: "fallback" for stage in values},
    )


def _stage_name(raw: object) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    return _STAGE_ALIASES.get(raw.strip().upper())


def _positive_points(record: Mapping[str, object]) -> Optional[float]:
    for key in ("weekly_delta", "delta_headroom"):
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value > 0:
            return float(value)
    return None


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def estimate_from_records(
    records: Sequence[Mapping[str, object]],
    *,
    fallback: Optional[StageEstimate] = None,
) -> StageEstimate:
    """Median positive consumption per stage. Missing stages keep the fallback."""

    base = fallback or fallback_estimate()
    samples: dict[str, list[float]] = {name: [] for name in _FALLBACK_DEFAULTS}
    for record in records:
        stage = _stage_name(record.get("stage"))
        if stage is None:
            continue
        points = _positive_points(record)
        if points is not None:
            samples[stage].append(points)
    sources = dict(base.sources)
    values = {
        "plan": base.plan,
        "build": base.build,
        "correction": base.correction,
        "review": base.review,
    }
    for stage, observed in samples.items():
        if not observed:
            continue
        values[stage] = round(_median(observed), 4)
        sources[stage] = "history"
    return StageEstimate(
        plan=values["plan"],
        build=values["build"],
        correction=values["correction"],
        review=values["review"],
        sources=sources,
    )


def load_cycle_estimate(
    records: Optional[Sequence[Mapping[str, object]]] = None,
    *,
    reports_dir: Optional[os.PathLike[str]] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> StageEstimate:
    """Read the ticket quota ledger unless the caller already supplied rows."""

    if records is None:
        from pathlib import Path

        from core.usage.ticket_cost import read_ticket_quota_history

        records = read_ticket_quota_history(None if reports_dir is None else Path(reports_dir))
    return estimate_from_records(records, fallback=fallback_estimate(environment))


def _number(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def cycle_is_covered(
    harness: Optional[str],
    headroom_by_harness: Mapping[str, object],
    estimate: StageEstimate,
    reserved_by_harness: Optional[Mapping[str, float]] = None,
) -> tuple[bool, str, float]:
    """Return whether ``harness`` can pay the whole cycle after reservations."""

    if not harness:
        return False, "unknown_harness", 0.0
    headroom = _number(headroom_by_harness.get(harness))
    if headroom is None:
        return False, "unknown_headroom", 0.0
    reserved = 0.0
    if reserved_by_harness is not None:
        reserved = float(reserved_by_harness.get(harness, 0.0) or 0.0)
    available = headroom - reserved
    if available + 1e-9 < estimate.total:
        return False, "insufficient_quota", available
    return True, "covered", available


def reserved_percent_by_harness(manager: object | None = None) -> dict[str, float]:
    """Active review reservations grouped by harness."""

    if manager is None:
        from core.usage.reservation import QuotaReservationManager

        manager = QuotaReservationManager()
    rows = manager.active_rows()  # type: ignore[attr-defined]
    totals: dict[str, float] = {}
    for row in rows:
        if str(row.get("purpose") or "") != "review":
            continue
        harness = str(row.get("harness") or "")
        if not harness:
            continue
        totals[harness] = totals.get(harness, 0.0) + float(row.get("percent") or 0.0)
    return totals


def guard_build_start(
    *,
    ticket_id: str,
    harness: str,
    headroom: object,
    estimate: Optional[StageEstimate] = None,
    manager: object | None = None,
    provider_id: Optional[str] = None,
) -> BuildGuard:
    """Refuse BUILD when the cycle does not fit, otherwise reserve the review slice."""

    estimate = estimate or load_cycle_estimate()
    covered, reason, available = cycle_is_covered(
        harness,
        {harness: headroom},
        estimate,
        reserved_by_harness=None,
    )
    if not covered:
        return BuildGuard(
            admitted=False,
            reason=reason,
            required_total=estimate.total,
            review_percent=estimate.review,
            available=available,
        )
    if manager is None:
        from core.usage.reservation import QuotaReservationManager

        manager = QuotaReservationManager()
    provider = provider_id or harness
    admitted, reserve_reason, reservation_id, remaining = manager.reserve_if_affordable(  # type: ignore[attr-defined]
        provider_id=provider,
        harness=harness,
        percent=estimate.review,
        headroom=float(headroom),  # type: ignore[arg-type]
        required_total=estimate.total,
        ticket_id=ticket_id,
        purpose="review",
    )
    return BuildGuard(
        admitted=admitted,
        reason=reserve_reason if not admitted else reason,
        reservation_id=reservation_id,
        required_total=estimate.total,
        review_percent=estimate.review,
        available=remaining,
    )
