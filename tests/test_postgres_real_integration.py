"""Optional integration tests against a REAL, disposable PostgreSQL (USR-167).

Why this file exists
--------------------
``add_job_evidence`` (USR-155) and ``local_run_events`` (USR-140) were only ever exercised on SQLite
and on the in-memory mock of ``PostgresControlStore``. The Postgres-only SQL (``jsonb_build_array``,
``@>``, ``||``, ``BIGSERIAL``, ``TIMESTAMPTZ``) and the privileges the database user needs were read
from the code, never run. These tests run them for real.

How to run (OFF by default)
---------------------------
The tests are marked ``postgres_integration`` and skip cleanly unless ``DARKFAC_TEST_POSTGRES_DSN``
points to a *throwaway* server. The DSN must belong to a role that may ``CREATE ROLE`` and
``CREATE DATABASE`` (the container superuser). Each test creates its own scratch database and its own
least-privilege roles, and drops them afterwards. NEVER point it at production.

    docker run -d --name pg-usr167 -e POSTGRES_PASSWORD=<test-password> -p 127.0.0.1:55432:5432 postgres:16
    $env:DARKFAC_TEST_POSTGRES_DSN = "postgresql://postgres:<test-password>@127.0.0.1:55432/postgres"
    python -m pytest tests/test_postgres_real_integration.py -q -p no:cacheprovider

Use ``127.0.0.1`` or ``localhost`` (the offline test fixture only lets local TCP through). The least
privilege model verified here is documented in ``docs/runbooks/live_progress.md``.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.line.local_progress import (
    READ_COLUMNS,
    TABLE,
    LocalRunEvent,
    PostgresSink,
    ProgressPublisher,
    describe_publish_failure,
)
from core.workflow.control_contracts import IntakeCommand, JobKey

pytestmark = pytest.mark.postgres_integration

ENV_DSN = "DARKFAC_TEST_POSTGRES_DSN"
RUNBOOK = Path(__file__).resolve().parents[1] / "docs" / "runbooks" / "live_progress.md"

# The minimum the writer role needs, exactly as documented in the runbook. {db}/{role} are substituted.
WRITER_GRANTS = (
    "GRANT CONNECT ON DATABASE {db} TO {role}",
    "GRANT USAGE, CREATE ON SCHEMA public TO {role}",
)
# What the Hub's read-only role needs (control store tables + the progress table).
READER_GRANTS = (
    "GRANT CONNECT ON DATABASE {db} TO {role}",
    "GRANT USAGE ON SCHEMA public TO {role}",
)
NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)


# ------------------------------------------------------------------------------ doc drift (offline)


@pytest.mark.parametrize("template", WRITER_GRANTS)
def test_runbook_documents_the_verified_writer_grants(template: str) -> None:
    """Not a database test: keeps the runbook and the verified grants from drifting apart."""
    text = RUNBOOK.read_text(encoding="utf-8")
    normalised = re.sub(r"<banco>|<db>", "{db}", text)
    normalised = re.sub(r"\bescritor\b", "{role}", normalised)
    assert template + ";" in normalised


# ------------------------------------------------------------------------------ fixtures


@dataclass
class ScratchDb:
    """A scratch database plus the DSNs of an admin, a minimal writer and a read-only role."""

    name: str
    admin: str  # superuser DSN pointing at the scratch database
    writer: str
    reader: str
    nobody: str  # CONNECT only: no schema privileges at all
    writer_role: str
    reader_role: str
    nobody_role: str

    def admin_exec(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        import psycopg

        with psycopg.connect(self.admin, autocommit=True) as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []


@pytest.fixture
def admin_dsn() -> str:
    dsn = os.environ.get(ENV_DSN, "").strip()
    if not dsn:
        pytest.skip(f"{ENV_DSN} not set: real-PostgreSQL integration tests are opt-in")
    pytest.importorskip("psycopg")
    return dsn


def _dsn_for(base: str, **overrides: str) -> str:
    from psycopg.conninfo import make_conninfo

    return make_conninfo(base, **overrides)


@pytest.fixture
def scratch(admin_dsn: str) -> Iterator[ScratchDb]:
    import psycopg
    from psycopg import sql

    suffix = secrets.token_hex(4)
    db = f"darkfac_usr167_{suffix}"
    roles = {kind: f"usr167_{kind}_{suffix}" for kind in ("writer", "reader", "nobody")}
    password = secrets.token_hex(16)  # throwaway credential for throwaway roles
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        for role in roles.values():
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db)))
    admin_scratch = _dsn_for(admin_dsn, dbname=db)

    def dsn(role: str) -> str:
        return _dsn_for(admin_dsn, dbname=db, user=role, password=password)

    handle = ScratchDb(
        name=db,
        admin=admin_scratch,
        writer=dsn(roles["writer"]),
        reader=dsn(roles["reader"]),
        nobody=dsn(roles["nobody"]),
        writer_role=roles["writer"],
        reader_role=roles["reader"],
        nobody_role=roles["nobody"],
    )
    try:
        with psycopg.connect(admin_scratch, autocommit=True) as conn:
            for template in WRITER_GRANTS:
                conn.execute(template.format(db=db, role=roles["writer"]))
            for template in READER_GRANTS:
                conn.execute(template.format(db=db, role=roles["reader"]))
            conn.execute(f"GRANT CONNECT ON DATABASE {db} TO {roles['nobody']}")
            # Sanity: the roles really are unprivileged, otherwise the tests prove nothing.
            for role in roles.values():
                row = conn.execute("SELECT rolsuper, rolcreatedb FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
                assert row == (False, False)
        yield handle
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (db,),
            )
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db)))
            for role in roles.values():
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _event(run_id: str, phase: str, status: str, *, offset_s: int = 0, message: str = "") -> LocalRunEvent:
    return LocalRunEvent(
        run_id=run_id,
        ticket_id="USR-167",
        project_id="darkfac",
        title="Teste contra Postgres real",
        phase=phase,
        status=status,  # type: ignore[arg-type]
        harness="claude",
        worker="pytest-host",
        cause_code=None,
        message=message,
        at=NOW + timedelta(seconds=offset_s),
    )


# ------------------------------------------------------------------------------ local_run_events


def test_local_run_events_ddl_insert_and_read_with_minimal_writer_grant(scratch: ScratchDb) -> None:
    import psycopg

    run_id = "local-USR-167-20261008T120000"
    # Two sinks = two processes: the second one re-runs the DDL on an existing table (idempotency).
    PostgresSink(scratch.writer).write(_event(run_id, "preflight", "running"))
    PostgresSink(scratch.writer).write(_event(run_id, "preflight", "succeeded", offset_s=5, message="ok"))

    with psycopg.connect(scratch.admin) as conn:
        rows = conn.execute(
            f"SELECT {READ_COLUMNS} FROM {TABLE} WHERE run_id = %s ORDER BY event_id", (run_id,)
        ).fetchall()
        owner = conn.execute("SELECT tableowner FROM pg_tables WHERE tablename = %s", (TABLE,)).fetchone()
        index = conn.execute("SELECT 1 FROM pg_indexes WHERE indexname = 'idx_local_run_events_run'").fetchone()
    assert [(r[5], r[6]) for r in rows] == [("preflight", "running"), ("preflight", "succeeded")]
    assert rows[0][0] < rows[1][0]  # BIGSERIAL event_id is monotonic
    assert rows[1][10] == "ok"
    assert rows[0][11].tzinfo is not None and rows[0][11] == NOW  # TIMESTAMPTZ round trip
    assert rows[1][11] - rows[0][11] == timedelta(seconds=5)
    assert owner == (scratch.writer_role,)  # the writer created (and owns) the table
    assert index is not None


def test_publisher_writes_a_full_run_through_the_minimal_writer(scratch: ScratchDb) -> None:
    """The real publisher (threads, redaction, buffering) against the real sink and database."""
    publisher = ProgressPublisher(
        ticket_id="USR-167", title="Run completo", run_id="local-USR-167-full", sink=PostgresSink(scratch.writer),
        timeout_s=15.0,
    )
    publisher.phase("preflight", "running")
    publisher.phase("preflight", "succeeded")
    publisher.phase("agent", "running", harness="claude")
    publisher.finish(True, "tudo certo")
    assert publisher.warning is None
    rows = scratch.admin_exec(
        f"SELECT phase, status FROM {TABLE} WHERE run_id = 'local-USR-167-full' ORDER BY event_id"
    )
    assert rows[:3] == [("preflight", "running"), ("preflight", "succeeded"), ("agent", "running")]
    assert rows[-1][0] == "run" and rows[-1][1] == "succeeded"


def test_writer_without_create_cannot_publish_and_failure_is_described(scratch: ScratchDb) -> None:
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege) as caught:
        PostgresSink(scratch.nobody).write(_event("local-USR-167-nobody", "preflight", "running"))
    assert "sem permissao de escrita" in describe_publish_failure(caught.value)


def test_insert_only_role_publishes_because_the_sink_inserts_before_any_ddl(scratch: ScratchDb) -> None:
    """USR-164: an admin pre-creates the table; the writer needs INSERT + sequence USAGE, never CREATE.

    ``PostgresSink`` now inserts first and only runs the DDL when the table is missing, so the
    CREATE-on-schema requirement documented in USR-167 applies to the bootstrap only (UndefinedTable).
    """
    import psycopg

    from core.line.local_progress import POSTGRES_DDL

    with psycopg.connect(scratch.admin, autocommit=True) as conn:
        for statement in POSTGRES_DDL.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.execute(f"GRANT USAGE ON SCHEMA public TO {scratch.nobody_role}")
        conn.execute(f"GRANT INSERT ON {TABLE} TO {scratch.nobody_role}")
        conn.execute(f"GRANT USAGE ON SEQUENCE {TABLE}_event_id_seq TO {scratch.nobody_role}")
    PostgresSink(scratch.nobody).write(_event("local-USR-167-ddl", "preflight", "running"))
    assert scratch.admin_exec(f"SELECT count(*) FROM {TABLE} WHERE run_id = 'local-USR-167-ddl'") == [(1,)]


def test_provisioned_restricted_writer_publishes_and_passes_the_audit(scratch: ScratchDb) -> None:
    """USR-164: the plan of ``scripts/provision_live_writer.py`` yields a role that only inserts events."""
    from urllib.parse import quote

    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    from core.line import live_writer

    role = "usr164_" + scratch.name[-8:]  # cluster-wide: unique per scratch database, dropped below
    try:
        password = live_writer.generate_password()
        plan = live_writer.build_plan(
            writer_role=role, database=scratch.name, create_role=True, password=password, reader_role=scratch.reader_role
        )
        with psycopg.connect(scratch.admin, autocommit=True) as conn:
            for step in plan:
                conn.execute(step.sql)
            audit = live_writer.audit_role(conn, role)
        assert audit.ok, audit.problems

        # `scratch.admin` is a key/value conninfo (make_conninfo); `writer_url` takes the URL the owner exports.
        info = conninfo_to_dict(scratch.admin)
        admin_url = (
            f"postgresql://{quote(str(info['user']), safe='')}:{quote(str(info['password']), safe='')}"
            f"@{info['host']}:{info['port']}/{info['dbname']}"
        )
        url = live_writer.writer_url(admin_url, role, password)
        PostgresSink(url).write(_event("local-USR-164-restricted", "preflight", "running"))
        assert scratch.admin_exec(f"SELECT count(*) FROM {TABLE} WHERE run_id = 'local-USR-164-restricted'") == [(1,)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege), psycopg.connect(url, autocommit=True) as conn:
            conn.execute("SELECT 1 FROM local_run_events")  # the writer cannot read, create or touch anything else
        with pytest.raises(psycopg.errors.InsufficientPrivilege), psycopg.connect(url, autocommit=True) as conn:
            conn.execute("CREATE TABLE usr164_intruder (x int)")
        # idempotent: running the plan again (role now exists) keeps the audit green
        again = live_writer.build_plan(writer_role=role, database=scratch.name, create_role=False, password=None)
        with psycopg.connect(scratch.admin, autocommit=True) as conn:
            for step in again:
                conn.execute(step.sql)
            assert live_writer.audit_role(conn, role).ok
    finally:
        with psycopg.connect(scratch.admin, autocommit=True) as conn:
            conn.execute(f'DROP OWNED BY "{role}"')
            conn.execute(f'DROP ROLE IF EXISTS "{role}"')


# ------------------------------------------------------------------------------ add_job_evidence


def _parked_job(scratch: ScratchDb) -> tuple[Any, JobKey]:
    """A real PostgresControlStore (built by the minimal writer) with one `waiting_human` job."""
    from core.orchestrator.adapters.control_postgres import PostgresControlStore

    store = PostgresControlStore(database_url=scratch.writer)
    assert store.mock_mode is False, "the store silently fell back to the in-memory mock"
    receipt = store.accept(
        IntakeCommand(
            channel="cli",
            external_id=f"usr167-{secrets.token_hex(3)}",
            project_id="darkfac",
            payload={
                "title": "Evidence",
                "problem": "add_job_evidence on a real PostgreSQL",
                "journey": "jsonb idempotency",
                "non_goals": ["none"],
                "criteria": ["idempotent"],
            },
            mode="autonomous",
            policy_ref="policy-v1",
        ),
        NOW,
    )
    assert receipt.run_id is not None
    row = scratch.admin_exec(
        "SELECT ticket_id, plan_version, stage, iteration FROM jobs WHERE run_id = %s", (receipt.run_id,)
    )[0]
    key = JobKey(run_id=receipt.run_id, ticket_id=row[0], plan_version=row[1], stage=row[2], iteration=row[3])
    return store, key


def _evidence(scratch: ScratchDb, key: JobKey) -> list[str]:
    rows = scratch.admin_exec("SELECT evidence_refs FROM jobs WHERE run_id = %s", (key.run_id,))
    assert len(rows) == 1
    return list(rows[0][0])


def test_add_job_evidence_is_idempotent_on_real_postgres(scratch: ScratchDb) -> None:
    store, key = _parked_job(scratch)

    # Not parked yet (pending): untouched, reports False.
    assert store.add_job_evidence(key, "orphan:notified") is False
    assert _evidence(scratch, key) == []

    scratch.admin_exec("UPDATE jobs SET status = 'waiting_human' WHERE run_id = %s", (key.run_id,))
    assert store.add_job_evidence(key, "orphan:notified") is True
    assert store.add_job_evidence(key, "orphan:notified") is True  # second call: idempotent, still True
    assert _evidence(scratch, key) == ["orphan:notified"]  # never duplicated

    # A different ref is appended after the first (`||` keeps order), also idempotently.
    assert store.add_job_evidence(key, "orphan:escalated") is True
    assert store.add_job_evidence(key, "orphan:escalated") is True
    assert _evidence(scratch, key) == ["orphan:notified", "orphan:escalated"]

    # Tricky text stays a jsonb string element (quotes, unicode, jsonb-looking content).
    tricky = 'x:"1",ç[2]{"a":1}'
    assert store.add_job_evidence(key, tricky) is True
    assert store.add_job_evidence(key, tricky) is True
    assert _evidence(scratch, key).count(tricky) == 1

    # The stored column is a real jsonb array and the status read-back agrees.
    assert scratch.admin_exec(
        "SELECT jsonb_typeof(evidence_refs), jsonb_array_length(evidence_refs) FROM jobs WHERE run_id = %s",
        (key.run_id,),
    ) == [("array", 3)]
    status = store.get_run_status(key.run_id)
    assert status is not None
    assert next(j for j in status["jobs"] if j["stage"] == key.stage)["evidence_refs"][0] == "orphan:notified"


def test_add_job_evidence_ignores_unknown_and_non_parked_jobs_on_real_postgres(scratch: ScratchDb) -> None:
    store, key = _parked_job(scratch)
    scratch.admin_exec("UPDATE jobs SET status = 'waiting_human' WHERE run_id = %s", (key.run_id,))
    unknown = JobKey(
        run_id=key.run_id, ticket_id=key.ticket_id, plan_version=key.plan_version, stage=key.stage, iteration=99
    )
    assert store.add_job_evidence(unknown, "x:1") is False

    scratch.admin_exec("UPDATE jobs SET status = 'succeeded' WHERE run_id = %s", (key.run_id,))
    assert store.add_job_evidence(key, "x:1") is False
    assert _evidence(scratch, key) == []


def test_add_job_evidence_tolerates_null_evidence_refs(scratch: ScratchDb) -> None:
    """``COALESCE(evidence_refs, '[]')`` must cover legacy rows where the column is NULL."""
    store, key = _parked_job(scratch)
    scratch.admin_exec("ALTER TABLE jobs ALTER COLUMN evidence_refs DROP NOT NULL")
    scratch.admin_exec(
        "UPDATE jobs SET status = 'waiting_human', evidence_refs = NULL WHERE run_id = %s", (key.run_id,)
    )
    assert store.add_job_evidence(key, "legacy:1") is True
    assert store.add_job_evidence(key, "legacy:1") is True
    assert _evidence(scratch, key) == ["legacy:1"]


def test_add_job_evidence_statement_needs_only_select_and_update_on_jobs(scratch: ScratchDb) -> None:
    """The statement itself (not the store's schema bootstrap) needs SELECT + UPDATE on ``jobs``.

    ``_init_db`` is stubbed out for the restricted role: the bootstrap needs CREATE on the schema and
    is covered by the minimal-writer tests above.
    """
    from unittest.mock import patch

    from core.orchestrator.adapters.control_postgres import PostgresControlStore
    from core.workflow.control_contracts import StoreUnavailableError

    _, key = _parked_job(scratch)  # tables are owned by the writer role
    scratch.admin_exec("UPDATE jobs SET status = 'waiting_human' WHERE run_id = %s", (key.run_id,))
    scratch.admin_exec(f"GRANT USAGE ON SCHEMA public TO {scratch.nobody_role}")
    scratch.admin_exec(f"GRANT SELECT ON jobs TO {scratch.nobody_role}")
    with patch.object(PostgresControlStore, "_init_db", lambda self: None):
        restricted = PostgresControlStore(database_url=scratch.nobody)
    assert restricted.mock_mode is False
    import psycopg

    with pytest.raises(StoreUnavailableError) as caught:
        restricted.add_job_evidence(key, "x:1")  # SELECT alone is not enough
    assert isinstance(caught.value.__cause__, psycopg.errors.InsufficientPrivilege)  # message is locale dependent
    assert _evidence(scratch, key) == []

    scratch.admin_exec(f"GRANT UPDATE ON jobs TO {scratch.nobody_role}")
    assert restricted.add_job_evidence(key, "x:1") is True
    assert restricted.add_job_evidence(key, "x:1") is True
    assert _evidence(scratch, key) == ["x:1"]


# ------------------------------------------------------------------------------ Hub read path


def test_hub_read_only_role_reads_local_run_events_into_the_live_line(scratch: ScratchDb) -> None:
    """End to end: minimal writer publishes, read-only Hub role reads and folds it (USR-140)."""
    from core.workflow.line_live import read_line_live

    store, key = _parked_job(scratch)  # creates the control store tables as the writer
    run_id = "local-USR-167-hub"
    publisher = ProgressPublisher(
        ticket_id="USR-167", title="Visivel no Hub", run_id=run_id, sink=PostgresSink(scratch.writer), timeout_s=15.0,
        clock=lambda: datetime.now(UTC),
    )
    publisher.phase("preflight", "running")
    publisher.phase("agent", "running", harness="claude")
    assert publisher.warning is None

    # Exactly the read-only grants documented for the Hub (docs/DARKHUB_ROADMAP.md + the runbook).
    scratch.admin_exec(
        f"GRANT SELECT ON runs, jobs, claims, intake_commands, {TABLE} TO {scratch.reader_role}"
    )
    snapshot = read_line_live(None, database_url=scratch.reader, now=datetime.now(UTC))
    assert snapshot.source.backend == "postgres" and snapshot.source.status == "ok", snapshot.warnings
    assert snapshot.warnings == []
    by_run = {run.run_id: run for run in snapshot.runs}
    assert run_id in by_run, f"local run missing from the live line: {sorted(by_run)}"
    assert by_run[run_id].ticket_id == "USR-167"
    assert key.run_id in by_run  # the control-store run is projected too
