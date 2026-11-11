"""Shared split of quota spent on merged work and quota spent without a merge."""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence


def consumption_points(record: Mapping[str, object]) -> float:
    """Positive headroom points. Weekly delta wins over the legacy column."""

    for key in ("weekly_delta", "delta_headroom"):
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value > 0:
            return float(value)
    return 0.0


def aggregate_merge_quota(
    records: Sequence[Mapping[str, object]],
    merged_ticket_ids: Iterable[str],
) -> dict[str, float | int | str]:
    """Sum consumption that belongs to a merged ticket and consumption that does not.

    A row without ``ticket_id`` counts as spent without a merge. The unit is the
    same headroom-point delta stored in the ticket quota ledger.
    """

    merged = {str(ticket_id) for ticket_id in merged_ticket_ids if str(ticket_id).strip()}
    with_merge = 0.0
    without_merge = 0.0
    for record in records:
        points = consumption_points(record)
        ticket_id = str(record.get("ticket_id") or "").strip()
        if ticket_id and ticket_id in merged:
            with_merge += points
        else:
            without_merge += points
    return {
        "quota_spent_with_merge": round(with_merge, 4),
        "quota_spent_without_merge": round(without_merge, 4),
        "merged_tickets": len(merged),
        "unit": "headroom_points",
    }


def merged_ticket_ids_from_demands(demands: Sequence[Mapping[str, object]]) -> set[str]:
    """Completed demands that carry delivery evidence count as their own merge."""

    merged: set[str] = set()
    for demand in demands:
        status = str(demand.get("status") or "").strip().lower()
        evidence = str(demand.get("delivery_evidence") or "").strip()
        ticket_id = str(demand.get("id") or "").strip()
        if ticket_id and status == "completed" and evidence:
            merged.add(ticket_id)
    return merged


def load_merge_quota(
    records: Sequence[Mapping[str, object]] | None = None,
    demands: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, float | int | str]:
    """Read the ledger and the demand list unless the caller injects both."""

    if records is None:
        from core.usage.ticket_cost import read_ticket_quota_history

        records = read_ticket_quota_history()
    if demands is None:
        demands = _load_demands()
    return aggregate_merge_quota(records, merged_ticket_ids_from_demands(demands))


def load_merge_quota_lines(
    records: Sequence[Mapping[str, object]] | None = None,
    demands: Sequence[Mapping[str, object]] | None = None,
) -> list[str]:
    """Two briefing lines. The numbers come from :func:`aggregate_merge_quota`."""

    report = load_merge_quota(records, demands)
    return [
        f"Cota gasta com merge: {report['quota_spent_with_merge']}",
        f"Cota gasta sem merge: {report['quota_spent_without_merge']}",
    ]


def _load_demands() -> list[dict[str, object]]:
    import json

    from core.paths import project_root

    path = project_root() / ".factory" / "demands" / "demands.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("demands"), list):
        return [row for row in payload["demands"] if isinstance(row, dict)]
    return []
