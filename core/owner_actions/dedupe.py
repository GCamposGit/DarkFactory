"""Repair OA-NNN collisions introduced by rebasing/merging with ``origin/main`` (USR-255).

Two branches can each assign the next ``OA-NNN`` while neither has seen the other's commit
(``scripts/owner_action.py add`` and :meth:`OwnerActionStore.add` only looked at the local file). A
rebase or merge then replays both appends at different positions in the JSON array; Git resolves
that as two separate, non-conflicting line ranges, so the merged file ends up with the *same id
twice* instead of a conflict an agent could see and fix. The official gate
(``tests/test_owner_actions_core.py::test_seeded_backlog_is_valid_and_complete``) only notices once
it fails to load the backlog ("item OA-NNN duplicado ignorado"), well after the rebase succeeded.

:func:`renumber_duplicate_owner_actions` runs right after a sync with the base branch (before the
gate) and fixes this in the working tree: for every id that appears more than once, the row whose
content matches the base branch's copy is left untouched (it is the "real" one, already known to
``origin/main``), and every other occurrence - the one this branch introduced independently - is
renumbered to the next free ``OA-NNN``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.git.safe_show import GitRunner, safe_show
from core.owner_actions.models import SEQUENTIAL_ID_PATTERN

logger = logging.getLogger(__name__)

OWNER_ACTIONS_RELATIVE = ".factory/owner_actions/owner_actions.json"


def _extract_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("actions"), list):
        return payload["actions"]
    return []


def _remote_rows(cwd: Path, remote_ref: str, runner: GitRunner | None) -> list[dict[str, Any]]:
    result = safe_show(remote_ref, OWNER_ACTIONS_RELATIVE, cwd=cwd, runner=runner)
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return [row for row in _extract_rows(payload) if isinstance(row, dict)]


def renumber_duplicate_owner_actions(
    cwd: Path,
    *,
    remote_ref: str = "origin/main",
    runner: GitRunner | None = None,
) -> list[str]:
    """Renumber owner-action rows in ``cwd``'s working tree that collide after a sync.

    Reads ``.factory/owner_actions/owner_actions.json`` from the working tree and compares it
    against ``remote_ref``'s copy (``git show``, best effort: a missing file, unreadable JSON or
    unknown ref just means nothing is fixed). For every duplicated id, the occurrence equal to
    ``remote_ref``'s row is kept; any other occurrence is assigned the next free ``OA-NNN`` (looking
    at both the working tree and ``remote_ref``, so the new id cannot collide either). Returns the
    ``"OLD-ID -> NEW-ID"`` lines to log, or ``[]`` when nothing collided or the file could not be
    read.

    This never raises: a corrupt or missing file is left alone for the gate to report normally.
    """

    target = cwd / OWNER_ACTIONS_RELATIVE
    if not target.is_file():
        return []
    try:
        document = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("owner_actions: nao foi possivel ler %s para dedupe (%s)", target, exc)
        return []
    rows = _extract_rows(document)
    if not rows:
        return []

    remote_rows = _remote_rows(cwd, remote_ref, runner)
    remote_by_id: dict[str, dict[str, Any]] = {}
    for row in remote_rows:
        remote_by_id.setdefault(str(row.get("id", "")), row)

    groups: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        action_id = str(row.get("id", ""))
        if action_id:
            groups.setdefault(action_id, []).append(index)

    used_numbers: set[int] = set()
    for action_id in (*groups, *remote_by_id):
        match = SEQUENTIAL_ID_PATTERN.match(action_id)
        if match:
            used_numbers.add(int(match.group(1)))

    changes: list[str] = []
    for action_id, indices in groups.items():
        if len(indices) < 2:
            continue
        remote_row = remote_by_id.get(action_id)
        keep_index = indices[0]
        if remote_row is not None:
            for index in indices:
                if rows[index] == remote_row:
                    keep_index = index
                    break
        for index in indices:
            if index == keep_index:
                continue
            new_number = max(used_numbers, default=0) + 1
            used_numbers.add(new_number)
            new_id = f"OA-{new_number:03d}"
            rows[index]["id"] = new_id
            changes.append(f"{action_id} -> {new_id}")
            logger.info(
                "owner_actions: %s colide com %s; renumerado para %s", action_id, remote_ref, new_id
            )

    if not changes:
        return []

    target.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return changes


__all__ = ["OWNER_ACTIONS_RELATIVE", "renumber_duplicate_owner_actions"]
