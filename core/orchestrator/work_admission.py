"""Apply candidate priority and the no-merge brake to a real work queue."""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from core.orchestrator.candidate_priority import (
    CandidateSchedulingConfig,
    WorkItem,
    WorkKind,
    prioritize_work,
)


def work_kind_for(demand: Mapping[str, object]) -> WorkKind:
    """Classify a demand. Unknown rows are new BUILD admissions."""

    raw = str(demand.get("work_kind") or demand.get("kind") or "").strip().lower()
    named = {
        "ready_candidate": WorkKind.READY_CANDIDATE,
        "review": WorkKind.REVIEW,
        "correction": WorkKind.CORRECTION,
        "correcao": WorkKind.CORRECTION,
        "fix": WorkKind.CORRECTION,
        "resume": WorkKind.RESUME,
        "resumption": WorkKind.RESUME,
        "new_build": WorkKind.NEW_BUILD,
        "build": WorkKind.NEW_BUILD,
    }
    if raw in named:
        return named[raw]
    stage = str(demand.get("next_stage") or "").strip().upper()
    if stage == "REVIEW":
        return WorkKind.REVIEW
    if stage in {"CORRECTION", "CORRECAO", "FIX"}:
        return WorkKind.CORRECTION
    if stage in {"RESUME", "RESUMPTION"}:
        return WorkKind.RESUME
    if demand.get("candidate_ready") is True:
        return WorkKind.READY_CANDIDATE
    return WorkKind.NEW_BUILD


def count_unmerged_candidates(demands: Sequence[Mapping[str, object]]) -> int:
    total = 0
    for demand in demands:
        if demand.get("unmerged_candidate") is True or work_kind_for(demand) is WorkKind.READY_CANDIDATE:
            total += 1
    return total


_FROM_ENVIRONMENT = object()


def prioritize_fleet_queue(
    queue: Sequence[str],
    demands: Sequence[Mapping[str, object]],
    *,
    quota_available: Optional[float] = None,
    cycle_cost: float = 0.0,
    review_cost: float = 0.0,
    new_builds_paused: bool = False,
    pause_reason: str = "new_build_paused_after_no_merge_sequence",
    max_unmerged_candidates: object = _FROM_ENVIRONMENT,
    unmerged_candidate_count: Optional[int] = None,
) -> tuple[list[str], dict[str, str]]:
    """Reorder the queue and drop new BUILD admissions the policy refuses.

    Ready candidates, reviews, corrections and resumptions stay ahead of a new
    BUILD and keep their relative order. Host slots are assigned later by the
    dispatcher, so a shortage of hosts does not remove work from the queue.
    ``quota_available is None`` does not invent a shortage. The unmerged
    candidate limit comes from the environment unless the caller passes one.
    ``None`` removes the cap.
    """

    by_id: dict[str, Mapping[str, object]] = {}
    for demand in demands:
        ticket_id = str(demand.get("id") or "")
        if ticket_id and ticket_id not in by_id:
            by_id[ticket_id] = demand

    items: list[WorkItem] = []
    original: list[str] = []
    for index, ticket_id in enumerate(queue):
        tid = str(ticket_id)
        original.append(tid)
        demand = by_id.get(tid, {"id": tid})
        kind = work_kind_for(demand)
        quota_required = 0.0
        quota_pool = None
        if quota_available is not None:
            quota_pool = "shared"
            quota_required = cycle_cost if kind is WorkKind.NEW_BUILD else review_cost
        items.append(
            WorkItem(
                work_id=tid,
                kind=kind,
                queue_rank=index,
                quota_pool=quota_pool,
                quota_required=max(0.0, float(quota_required)),
                host_pool=None,
                host_required=0,
            )
        )

    if max_unmerged_candidates is _FROM_ENVIRONMENT:
        max_unmerged_candidates = scheduling_limit_from_environment()
    candidate_count = (
        count_unmerged_candidates(demands)
        if unmerged_candidate_count is None
        else unmerged_candidate_count
    )
    if new_builds_paused:
        limit = 0 if max_unmerged_candidates is None else min(max_unmerged_candidates, 0)
        candidate_count = max(candidate_count, 1)
        max_unmerged_candidates = limit

    pools = {} if quota_available is None else {"shared": float(quota_available)}
    decision = prioritize_work(
        items,
        pools,
        {},
        max_unmerged_candidates=max_unmerged_candidates,
        unmerged_candidate_count=candidate_count,
    )
    selected = [item.work_id for item in decision.selected]
    skipped: dict[str, str] = {}
    held: list[str] = []
    for ticket_id in original:
        if ticket_id in selected:
            continue
        reason = decision.reason_for(ticket_id) or "deferred"
        if reason == "candidate_limit" and new_builds_paused:
            reason = pause_reason
        if reason == "host":
            held.append(ticket_id)
            continue
        skipped[ticket_id] = reason
    return selected + held, skipped


def scheduling_limit_from_environment(environment: Optional[Mapping[str, str]] = None) -> Optional[int]:
    return CandidateSchedulingConfig.from_environment(environment).max_unmerged_candidates
