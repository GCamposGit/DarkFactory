"""Apply candidate priority and the no-merge brake to a real work queue."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Mapping, Optional, Sequence

from core.orchestrator.candidate_priority import (
    CandidateSchedulingConfig,
    WorkItem,
    WorkKind,
    prioritize_work,
)

logger = logging.getLogger(__name__)

_EMPTY_SIGNAL = {"", "none", "unknown", "null"}
_CORRECTION_VERDICTS = {"changes_required", "changes_requested", "correction", "correcao"}


def _signal_text(value: object) -> str:
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip()
    if text.lower() in _EMPTY_SIGNAL:
        return ""
    return text


def _first_signal(demand: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        text = _signal_text(demand.get(key))
        if text:
            return text
    return ""


def work_kind_for(demand: Mapping[str, object]) -> WorkKind:
    """Classify a demand. A planned ticket with no real work signal is a new BUILD.

    Explicit ``work_kind``, ``kind``, ``next_stage`` and ``candidate_ready`` still
    win. Otherwise a worktree, an open PR, an existing candidate, a pending
    stage or a review verdict distinguishes work that already started.
    """

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
    verdict = _signal_text(demand.get("review_verdict") or demand.get("last_verdict")).lower()
    if verdict in _CORRECTION_VERDICTS or _first_signal(demand, "correction_of", "fix_of"):
        return WorkKind.CORRECTION
    pending = str(demand.get("pending_stage") or demand.get("resume_stage") or "").strip().lower()
    if pending == "review":
        return WorkKind.REVIEW
    if pending in {"correction", "correcao", "fix"}:
        return WorkKind.CORRECTION
    if _first_signal(demand, "pr_url", "pull_request", "pull_request_url", "pr_number"):
        return WorkKind.READY_CANDIDATE
    # ``candidate_digest`` is the admission fingerprint, including the placeholder
    # the dispatcher stores before a candidate exists. It does not mean the build
    # is ready. A published candidate carries a sha or an explicit ref.
    if _first_signal(demand, "candidate_sha", "candidate_ref"):
        return WorkKind.READY_CANDIDATE
    if _first_signal(demand, "worktree", "worktree_path") or pending in {"resume", "resumption"}:
        return WorkKind.RESUME
    return WorkKind.NEW_BUILD


def _ticket_from_branch(slug: str, wanted: Mapping[str, str]) -> Optional[str]:
    lowered = slug.lower()
    if lowered in wanted:
        return wanted[lowered]
    for key, original in wanted.items():
        if lowered.startswith(key + "-"):
            return original
    return None


def signals_from_worktrees(
    ticket_ids: Sequence[str],
    worktrees: Sequence[Mapping[str, object]],
    pending: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, dict[str, str]]:
    """Map ticket ids to worktree and pending-delivery signals. No process calls."""

    wanted = {str(ticket_id).lower(): str(ticket_id) for ticket_id in ticket_ids if str(ticket_id).strip()}
    found: dict[str, dict[str, str]] = {}
    for row in worktrees:
        branch = str(row.get("branch") or "").removeprefix("refs/heads/")
        if not branch.startswith("ticket/"):
            continue
        ticket_id = _ticket_from_branch(branch.split("/", 1)[1], wanted)
        if ticket_id is None:
            continue
        path = _signal_text(row.get("worktree"))
        if path:
            found.setdefault(ticket_id, {})["worktree"] = path
    for row in pending or ():
        ticket_id = str(row.get("ticket_id") or "").strip()
        if ticket_id.lower() not in wanted:
            continue
        original = wanted[ticket_id.lower()]
        stage = _signal_text(row.get("stage"))
        worktree = _signal_text(row.get("worktree"))
        slot = found.setdefault(original, {})
        if stage:
            slot["pending_stage"] = stage
        if worktree and "worktree" not in slot:
            slot["worktree"] = worktree
    return found


def discover_work_signals(
    ticket_ids: Sequence[str],
    *,
    repo_root: Optional[Path] = None,
) -> dict[str, dict[str, str]]:
    """Read local worktrees and pending delivery for tickets already in progress."""

    ids = [str(ticket_id) for ticket_id in ticket_ids if str(ticket_id).strip()]
    if not ids:
        return {}
    if repo_root is None:
        from core.paths import project_root

        repo_root = project_root()
    rows: list[dict[str, str]] = []
    try:
        from core.git.ticket_workspace import list_worktrees

        rows = list_worktrees(repo_root)
    except Exception as exc:  # noqa: BLE001 - a missing git list must not stall the queue
        logger.warning("sinais de worktree indisponiveis: %s", type(exc).__name__)
    pending_rows: list[dict[str, object]] = []
    try:
        from core.line.pending_delivery import queue_dir

        directory = queue_dir()
        if directory.is_dir():
            for ticket_id in ids:
                path = directory / f"{ticket_id}.json"
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict):
                    payload.setdefault("ticket_id", ticket_id)
                    pending_rows.append(payload)
    except Exception as exc:  # noqa: BLE001 - pending delivery is an optional signal
        logger.warning("sinais de entrega pendente indisponiveis: %s", type(exc).__name__)
    return signals_from_worktrees(ids, rows, pending_rows)


def _with_signals(
    demand: Mapping[str, object],
    signals: Mapping[str, object],
) -> dict[str, object]:
    merged = dict(demand)
    for key, value in signals.items():
        if not _signal_text(merged.get(key)):
            merged[key] = value
    return merged


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
    work_signals: Optional[Mapping[str, Mapping[str, object]]] = None,
) -> tuple[list[str], dict[str, str]]:
    """Reorder the queue and drop new BUILD admissions the policy refuses.

    Ready candidates, reviews, corrections and resumptions stay ahead of a new
    BUILD and keep their relative order. Host slots are assigned later by the
    dispatcher, so a shortage of hosts does not remove work from the queue.
    ``quota_available is None`` does not invent a shortage. The unmerged
    candidate limit comes from the environment unless the caller passes one.
    ``None`` removes the cap.
    """

    supplied = work_signals or {}
    enriched: list[dict[str, object]] = []
    by_id: dict[str, Mapping[str, object]] = {}
    for demand in demands:
        ticket_id = str(demand.get("id") or "")
        row = _with_signals(demand, supplied.get(ticket_id) or {})
        enriched.append(row)
        if ticket_id and ticket_id not in by_id:
            by_id[ticket_id] = row

    items: list[WorkItem] = []
    original: list[str] = []
    for index, ticket_id in enumerate(queue):
        tid = str(ticket_id)
        original.append(tid)
        demand = by_id.get(tid) or _with_signals({"id": tid}, supplied.get(tid) or {})
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
        count_unmerged_candidates(enriched)
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
