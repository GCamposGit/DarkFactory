"""3-way merge for .factory/demands/demands.json (USR-112).

When a ticket execution runs concurrently with new ticket registrations or
status updates on main, textual git merge can produce conflicts on adjacent lines.
This module performs a semantic 3-way merge by ticket ID:
- Preserves updates made by the current branch to existing tickets.
- Preserves new tickets appended to main during the run.
- Fails closed if the exact same ticket was modified on both sides with different values.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from core.demands.id_allocator import title_collisions

logger = logging.getLogger(__name__)


def _parse_demands_payload(raw: str) -> list[dict[str, Any]]:
    """Parse JSON string into a list of demand objects."""
    raw = raw.strip()
    if not raw:
        return []
    payload = json.loads(raw)
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict) and "demands" in payload and isinstance(payload["demands"], list):
        return [row for row in payload["demands"] if isinstance(row, dict)]
    raise ValueError("Demands payload must be a list of dicts or a dict with a 'demands' list")


def merge_demands_3way(
    base_raw: str,
    ours_raw: str,
    theirs_raw: str,
) -> tuple[bool, str, Optional[str]]:
    """Perform a semantic 3-way merge of demands.json.

    Args:
        base_raw: JSON text at the common merge base.
        ours_raw: JSON text on the current branch (HEAD).
        theirs_raw: JSON text on the remote tracking branch (origin/base).

    Returns:
        (ok, merged_json_text, error_message)
    """
    try:
        base_rows = _parse_demands_payload(base_raw)
        ours_rows = _parse_demands_payload(ours_raw)
        theirs_rows = _parse_demands_payload(theirs_raw)
    except Exception as exc:
        return False, "", f"Failed to parse demands JSON for 3-way merge: {exc}"

    # Check for title collisions across branches (different demands reusing the same ID)
    try:
        collisions = title_collisions(theirs_raw, ours_raw)
        if collisions:
            return False, "", f"demands.json ticket ID collision; merge blocked: {'; '.join(collisions)}"
    except Exception as exc:
        logger.debug("title_collisions check failed with error: %s", exc)

    base_map = {row["id"]: row for row in base_rows if "id" in row}
    ours_map = {row["id"]: row for row in ours_rows if "id" in row}
    theirs_map = {row["id"]: row for row in theirs_rows if "id" in row}

    all_ids = set(base_map) | set(ours_map) | set(theirs_map)

    # 1. Conflict detection (Fail-Closed)
    for tid in sorted(all_ids):
        in_b = tid in base_map
        in_o = tid in ours_map
        in_t = tid in theirs_map

        if in_b:
            b_item = base_map[tid]
            o_item = ours_map.get(tid)
            t_item = theirs_map.get(tid)

            if o_item is None and t_item is None:
                continue
            if o_item is None:
                if t_item != b_item:
                    return False, "", f"Conflict for ticket {tid}: removed in branch but modified in base"
                continue
            if t_item is None:
                if o_item != b_item:
                    return False, "", f"Conflict for ticket {tid}: removed in base but modified in branch"
                continue

            o_changed = (o_item != b_item)
            t_changed = (t_item != b_item)

            if o_changed and t_changed:
                if o_item != t_item:
                    return (
                        False,
                        "",
                        f"Conflict for ticket {tid}: modified in both branches with different values",
                    )
        else:
            # Ticket added in at least one branch
            o_item = ours_map.get(tid)
            t_item = theirs_map.get(tid)
            if o_item is not None and t_item is not None:
                if o_item != t_item:
                    return (
                        False,
                        "",
                        f"Conflict for ticket {tid}: added in both branches with different values",
                    )

    # 2. Build merged result
    # We follow the order of `theirs_rows` (the base branch/main), applying modifications
    # made by `ours`. Any new tickets from `ours` are appended at the end.
    merged_rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for t_item in theirs_rows:
        tid = t_item.get("id")
        if not tid:
            continue
        seen_ids.add(tid)

        if tid in ours_map:
            o_item = ours_map[tid]
            b_item = base_map.get(tid)
            if o_item != b_item:
                # ours updated this ticket
                merged_rows.append(o_item)
            else:
                # ours did not touch this ticket
                merged_rows.append(t_item)
        else:
            # Not in ours
            if tid in base_map:
                # Was in base and deleted by ours
                continue
            # Added in theirs
            merged_rows.append(t_item)

    # Now append tickets added in ours that are not in theirs
    for o_item in ours_rows:
        tid = o_item.get("id")
        if not tid:
            continue
        if tid not in seen_ids:
            merged_rows.append(o_item)
            seen_ids.add(tid)

    merged_text = json.dumps(merged_rows, indent=2, ensure_ascii=False) + "\n"
    return True, merged_text, None
