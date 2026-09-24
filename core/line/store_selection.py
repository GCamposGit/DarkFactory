"""HF-27-10 review fix (blocking issue 2) -- pick the same `ControlStore`
backend the cloud coordinator/worker use, instead of a hardcoded SQLite path.

`CloudCoordinator`/`CloudWorker` both build their store as
`PostgresControlStore(database_url=os.environ.get("DARKFAC_HF02_DATABASE_URL"))`,
whose own fallback when that URL is unset is an **ephemeral in-memory**
SQLite (`PostgresControlStore(mock_mode=...)` -> `SQLiteControlStore(db_path=":memory:")`)
-- correct for their tests, wrong for a process that needs a demand it
submitted today to still be there when it re-observes it later (or
tomorrow). `default_control_store()` reuses the coordinator/worker's exact
"Postgres iff DARKFAC_HF02_DATABASE_URL is set" rule, but keeps our own
persistent, file-backed `SQLiteControlStore` as the local/test fallback.
"""

from __future__ import annotations

import os
from pathlib import Path

from core.workflow.control_store import ControlStore, SQLiteControlStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTROL_DB = PROJECT_ROOT / ".factory" / "control.db"


def default_control_store(*, sqlite_db_path: Path | str = DEFAULT_CONTROL_DB) -> ControlStore:
    """Postgres (real cloud line) if `DARKFAC_HF02_DATABASE_URL` is set, else
    a persistent local SQLite file -- never the coordinator/worker's
    in-memory mock, which would silently drop every canary/dogfood demand
    between process invocations."""
    database_url = os.environ.get("DARKFAC_HF02_DATABASE_URL")
    if database_url:
        from core.orchestrator.adapters.control_postgres import PostgresControlStore
        from core.workflow.control_contracts import RuntimeOwner

        return PostgresControlStore(
            database_url=database_url,
            runtime_owner=RuntimeOwner.CLOUD_DBOS_POSTGRES.value,
            lease_duration_sec=300,
        )
    return SQLiteControlStore(db_path=sqlite_db_path)
