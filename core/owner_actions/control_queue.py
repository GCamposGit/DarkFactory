"""Owner-action backlog in the control store (USR-343 S4b).

Writers (Hub answers, mirrors, alerts, CLI) land in the control store instead of editing the
git-tracked ledger and opening ``chore/oa-*`` PRs. ``owner_actions.json`` becomes a daily snapshot
(``scripts/snapshot_queue.py``). Readers merge the control rows over the file (control wins).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("darkfac.owner_actions.control_queue")


def is_default_ledger(path: Path) -> bool:
    from core.owner_actions import store as oa_store

    if os.environ.get(oa_store.ENV_PATH, "").strip():
        return False
    try:
        return path.resolve() == oa_store.DEFAULT_PATH.resolve()
    except OSError:
        return False


def list_control_rows() -> list[dict[str, Any]]:
    """Rows held by the control store; ``[]`` when it does not exist yet or is unreachable."""
    from core.demands.queue_store import control_store
    from core.paths import state_root

    if not os.environ.get("DARKFAC_HF02_DATABASE_URL") and not (state_root() / "control.db").exists():
        return []
    try:
        return [r for r in control_store().list_owner_actions() if isinstance(r, dict)]
    except Exception as exc:  # noqa: BLE001 - readers fall back to the file
        logger.warning("Control store unavailable for owner actions (%s); using the file", exc)
        return []


def write_rows(rows: list[dict[str, Any]]) -> None:
    """Upsert ``rows`` (``to_record`` dicts). Raises when the store is unavailable."""
    from core.demands.queue_store import control_store

    store = control_store()
    now = datetime.now(UTC)
    for row in rows:
        store.upsert_owner_action(str(row["id"]), str(row.get("status") or "open"), row, now)


def merge_rows(file_rows: list[Any], control_rows: list[dict[str, Any]]) -> list[Any]:
    """Control rows replace file rows of the same id (a done file row is never reopened)."""
    merged: dict[str, Any] = {}
    order: list[str] = []
    for raw in file_rows:
        key = str(raw.get("id")) if isinstance(raw, dict) else None
        if key is None:
            continue
        merged[key] = raw
        order.append(key)
    for raw in control_rows:
        key = str(raw.get("id"))
        current = merged.get(key)
        if isinstance(current, dict) and current.get("status") == "done" and raw.get("status") != "done":
            continue
        if key not in merged:
            order.append(key)
        merged[key] = raw
    return [merged[k] for k in order]
