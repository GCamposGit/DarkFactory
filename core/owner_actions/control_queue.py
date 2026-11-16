"""Owner-action backlog in the control store (USR-343 S4b).

Writers (Hub answers, mirrors, alerts, CLI) land in the control store instead of editing the
git-tracked ledger and opening ``chore/oa-*`` PRs. ``owner_actions.json`` becomes a daily snapshot
(``scripts/snapshot_queue.py``). In control mode the control store is the only source of truth for reads and writes.
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


class ControlQueueUnavailable(RuntimeError):
    """The control store is configured but cannot be read or written (never masked as an empty queue)."""


def is_configured() -> bool:
    """Whether configuration selects PostgreSQL or an existing local control database."""
    from core.paths import state_root

    return bool(os.environ.get("DARKFAC_HF02_DATABASE_URL", "").strip()) or (state_root() / "control.db").exists()


def list_control_rows() -> list[dict[str, Any]]:
    """Rows held by the control store; configuration and connection failures raise.

    Raises ``ControlQueueUnavailable`` when the store is configured but unreachable.
    """
    from core.demands.queue_store import control_store

    if not is_configured():
        if os.environ.get("DARKFAC_OWNER_ACTIONS_MODE", "").strip().lower() == "control":
            # Deployed service told to read the shared store but given no database: say so.
            raise ControlQueueUnavailable("control store nao configurado")
        return []  # local store not created yet: a genuinely empty queue, not a masked failure
    try:
        return [r for r in control_store().list_owner_actions() if isinstance(r, dict)]
    except Exception as exc:  # noqa: BLE001 - re-raised as a structured error
        logger.warning("Control store unavailable for owner actions (%s)", type(exc).__name__)
        raise ControlQueueUnavailable(f"control store indisponivel: {type(exc).__name__}") from exc


def write_rows(rows: list[dict[str, Any]]) -> None:
    """Upsert ``rows`` (``to_record`` dicts). Raises ``ControlQueueUnavailable`` when the store is unavailable."""
    from core.demands.queue_store import control_store

    try:
        store = control_store()
        now = datetime.now(UTC)
        for row in rows:
            store.upsert_owner_action(str(row["id"]), str(row.get("status") or "open"), row, now)
    except Exception as exc:  # noqa: BLE001 - re-raised as a structured error
        logger.warning("Control store write failed for owner actions (%s)", type(exc).__name__)
        raise ControlQueueUnavailable(f"control store indisponivel: {type(exc).__name__}") from exc
