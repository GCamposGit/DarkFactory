"""Admission view of durable local runs, using their existing abandonment contract.

This is an admission guard, not a replacement for worker fencing or resource leases.
Only successfully persisted progress can establish ownership across hosts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from core.line.local_progress import ABANDON_AFTER_SECONDS, PHASES


def active_manual_owner(
    events: Iterable[Mapping[str, Any]], *, now: datetime
) -> str | None:
    """Return an unclosed, non-abandoned local run from newest-first events.

    Closing a run releases ownership even when its last phase remains running.
    A failed phase alone does not close the run: its operator may be repairing it.
    The existing six-hour local-run abandonment contract bounds legacy/stale runs.
    """
    latest: dict[str, Mapping[str, Any]] = {}
    for event in events:
        run_id = str(event.get("run_id") or "")
        phase = event.get("phase")
        if not run_id or phase not in (*PHASES, "run"):
            continue
        if run_id not in latest:
            latest[run_id] = event
    cutoff = now - timedelta(seconds=ABANDON_AFTER_SECONDS)
    for run_id, event in latest.items():
        if event.get("phase") == "run" and event.get("status") in {"succeeded", "failed", "cancelled"}:
            continue
        raw_at = event.get("at")
        try:
            at = raw_at if isinstance(raw_at, datetime) else datetime.fromisoformat(str(raw_at))
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
        except (TypeError, ValueError) as exc:
            # Surface a diagnosable fail-closed refusal instead of an eternal invisible owner.
            raise ValueError("manual_ownership_invalid_timestamp") from exc
        if at >= cutoff:
            return run_id
    return None
