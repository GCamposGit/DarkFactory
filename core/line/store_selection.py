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

from core.paths import state_root
from core.workflow.control_store import ControlStore, SQLiteControlStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def default_control_db_path() -> Path:
    return state_root() / "control.db"


DEFAULT_CONTROL_DB = default_control_db_path()


def _explicit_mock_url(url: str) -> bool:
    """True only for the intentional in-memory scheme (``mock:`` / ``mock://``).

    ``PostgresControlStore`` treats every URL that starts with the letters ``mock`` as its
    offline hatch, so ``mockery://`` never dials either. That broader prefix is not an
    authorized escape: a configured URL that merely shares the prefix and comes back with
    ``mock_mode`` is a failed open, not a test double.
    """
    return url.startswith("mock:")


def default_control_store(
    *, sqlite_db_path: Path | str | None = None, database_url: str | None = None
) -> ControlStore:
    """Postgres (real cloud line) if a database URL is configured, else a persistent
    local SQLite file -- never the coordinator/worker's in-memory mock, which would
    silently drop every canary/dogfood demand between process invocations.

    ``database_url`` defaults to ``DARKFAC_HF02_DATABASE_URL`` (the coordinator/worker's
    own resolution); callers that read the same Postgres under a different env-var name
    (e.g. the Hub's ``DARKHUB_LINE_DATABASE_URL`` / ``DARKHUB_CONTROL_DATABASE_URL``, see
    ``core.line.owner_intake.line_database_url``) pass the resolved URL explicitly instead
    of duplicating this function's env lookup.
    """
    effective_path = sqlite_db_path or default_control_db_path()
    url = database_url if database_url is not None else os.environ.get("DARKFAC_HF02_DATABASE_URL")
    if url:
        from core.orchestrator.adapters.control_postgres import PostgresControlStore
        from core.workflow.control_contracts import RuntimeOwner, StoreUnavailableError

        store = PostgresControlStore(
            database_url=url,
            runtime_owner=RuntimeOwner.CLOUD_DBOS_POSTGRES.value,
            lease_duration_sec=300,
        )
        # Missing `mock_mode` must not escape as AttributeError (USR-449). False means the
        # adapter did not degrade to its in-memory mock.
        degraded_to_mock = bool(getattr(store, "mock_mode", False))
        if degraded_to_mock and not _explicit_mock_url(url):
            # A real URL was configured but the connection failed: `PostgresControlStore`
            # degrades to an in-memory mock on its own. Serving that silently would make a
            # Postgres outage look like "no tickets queued" instead of a store failure.
            raise StoreUnavailableError(
                "Postgres control store at the configured URL is unreachable; "
                "refusing its in-memory mock fallback"
            )
        return store
    return SQLiteControlStore(db_path=effective_path)
