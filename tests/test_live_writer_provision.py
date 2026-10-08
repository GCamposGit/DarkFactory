"""USR-164: provisioning of the restricted ``local_run_events`` writer role (no real database).

The plan is pure SQL text built from validated identifiers; the CLI runs it through ``psycopg`` which
these tests replace with a recording fake. The privileges are *verified* by a read-only audit query.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from core.line import live_writer
from core.line.local_progress import POSTGRES_DDL, TABLE, WRITE_DATABASE_URL_ENVS

REPO = Path(__file__).resolve().parents[1]
_PASSWORD = "T3st-" + "Passw0rd-" + "0123456789abcdef"  # throwaway value, never a real credential


def _text(plan: list[live_writer.Step]) -> str:
    return "\n".join(step.sql for step in plan)


# ---------------------------------------------------------------- the plan


def test_the_plan_grants_only_connect_usage_and_insert_on_the_progress_table() -> None:
    plan = live_writer.build_plan(
        writer_role="darkfac_live_writer", database="darkfac", create_role=True, password=_PASSWORD
    )
    sql = _text(plan)
    assert 'CREATE ROLE "darkfac_live_writer" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION' in sql
    assert "NOBYPASSRLS" in sql and "CONNECTION LIMIT" in sql
    assert f"PASSWORD '{_PASSWORD}'" in sql
    assert 'GRANT CONNECT ON DATABASE "darkfac" TO "darkfac_live_writer"' in sql
    assert 'GRANT USAGE ON SCHEMA public TO "darkfac_live_writer"' in sql
    assert f'GRANT INSERT ON TABLE {TABLE} TO "darkfac_live_writer"' in sql
    assert f'GRANT USAGE ON SEQUENCE {TABLE}_event_id_seq TO "darkfac_live_writer"' in sql
    assert 'REVOKE CREATE ON SCHEMA public FROM "darkfac_live_writer"' in sql
    # nothing broader than that
    assert "ALL PRIVILEGES" not in sql.upper() and "GRANT SELECT" not in sql.upper()
    assert "GRANT UPDATE" not in sql.upper() and "GRANT DELETE" not in sql.upper()
    # the admin (not the writer) creates the table, so the writer never needs CREATE on the schema
    assert any(f"CREATE TABLE IF NOT EXISTS {TABLE}" in step.sql for step in plan)
    ddl_index = next(i for i, step in enumerate(plan) if "CREATE TABLE" in step.sql)
    grant_index = next(i for i, step in enumerate(plan) if f"GRANT INSERT ON TABLE {TABLE}" in step.sql)
    assert ddl_index < grant_index


def test_the_ddl_in_the_plan_is_the_ddl_the_sink_uses() -> None:
    plan = live_writer.build_plan(writer_role="w_role", database="db", create_role=True, password=_PASSWORD)
    statements = [s.strip() for s in POSTGRES_DDL.split(";") if s.strip()]
    texts = [" ".join(step.sql.split()) for step in plan]
    for statement in statements:
        assert " ".join(statement.split()) in texts


def test_an_existing_role_is_altered_without_touching_the_password_unless_rotating() -> None:
    kept = _text(live_writer.build_plan(writer_role="w_role", database="db", create_role=False, password=None))
    assert "ALTER ROLE" in kept and "CREATE ROLE" not in kept and "PASSWORD" not in kept
    rotated = _text(live_writer.build_plan(writer_role="w_role", database="db", create_role=False, password=_PASSWORD))
    assert f"PASSWORD '{_PASSWORD}'" in rotated and "CREATE ROLE" not in rotated


def test_the_reader_role_gets_select_on_the_progress_table_only() -> None:
    sql = _text(
        live_writer.build_plan(
            writer_role="w_role", database="db", create_role=True, password=_PASSWORD, reader_role="darkhub_ro"
        )
    )
    assert f'GRANT SELECT ON TABLE {TABLE} TO "darkhub_ro"' in sql
    assert "GRANT SELECT" not in _text(
        live_writer.build_plan(writer_role="w_role", database="db", create_role=True, password=_PASSWORD)
    )


def test_redacted_plan_never_shows_the_password() -> None:
    plan = live_writer.build_plan(writer_role="w_role", database="db", create_role=True, password=_PASSWORD)
    shown = "\n".join(step.display for step in plan)
    assert _PASSWORD not in shown and "<SENHA>" in shown


@pytest.mark.parametrize("bad", ['x"; DROP TABLE runs; --', "a b", "", "1abc", "a" * 64, "semi;colon", "quote'"])
def test_unsafe_identifiers_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        live_writer.build_plan(writer_role=bad, database="db", create_role=True, password=_PASSWORD)
    with pytest.raises(ValueError):
        live_writer.build_plan(writer_role="w_role", database=bad, create_role=True, password=_PASSWORD)
    with pytest.raises(ValueError):
        live_writer.build_plan(
            writer_role="w_role", database="db", create_role=True, password=_PASSWORD, reader_role=bad
        )


@pytest.mark.parametrize("bad", ["short", "has space 0123456789", "quote'0123456789abcdef", "back\\slash0123456789abc"])
def test_weak_or_unquotable_passwords_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        live_writer.build_plan(writer_role="w_role", database="db", create_role=True, password=bad)


def test_generated_passwords_are_strong_and_accepted() -> None:
    first, second = live_writer.generate_password(), live_writer.generate_password()
    assert first != second and len(first) >= 32
    live_writer.build_plan(writer_role="w_role", database="db", create_role=True, password=first)


# ---------------------------------------------------------------- audit


def test_audit_passes_when_the_role_can_only_insert_into_the_progress_table() -> None:
    result = live_writer.evaluate_audit(
        schema_create=False, table_insert=True, sequence_usage=True, other_privileged_tables=[], superuser=False
    )
    assert result.ok and result.problems == []


def test_audit_flags_every_way_the_role_is_too_broad_or_too_narrow() -> None:
    result = live_writer.evaluate_audit(
        schema_create=True, table_insert=False, sequence_usage=False,
        other_privileged_tables=["jobs", "runs"], superuser=True,
    )
    assert not result.ok
    joined = " | ".join(result.problems)
    for needle in ("CREATE", "INSERT", "sequence", "jobs", "runs", "superuser"):
        assert needle in joined, joined


def test_the_connection_url_is_built_from_the_admin_url_without_leaking_the_admin_password() -> None:
    admin = "postgresql://admin:AdminSecret123@db.tail1234.ts.net:5433/darkfac?sslmode=require"
    url = live_writer.writer_url(admin, "darkfac_live_writer", "pw-with/chars+and=more0123456789")
    assert url.startswith("postgresql://darkfac_live_writer:")
    assert "@db.tail1234.ts.net:5433/darkfac" in url
    assert "AdminSecret123" not in url and "admin:" not in url
    assert "sslmode=require" in url
    assert "pw-with/chars" not in url  # percent-encoded inside the URL
    assert live_writer.database_name(admin) == "darkfac"


# ---------------------------------------------------------------- the CLI


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _AdminConn:
    def __init__(self, log: list[str], *, role_exists: bool, audit_other: list[str]) -> None:
        self.log = log
        self.role_exists = role_exists
        self.audit_other = audit_other

    def __enter__(self) -> "_AdminConn":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> _Cursor:
        text = " ".join(str(sql).split())
        self.log.append(text)
        if "FROM pg_roles WHERE rolname" in text and "rolsuper" not in text:
            return _Cursor([(1,)] if self.role_exists else [])
        if "has_schema_privilege" in text:
            return _Cursor([(False, True, True, False)])
        if "FROM pg_class" in text:
            return _Cursor([(name,) for name in self.audit_other])
        return _Cursor([])


class _Log(list):  # type: ignore[type-arg]
    state: dict[str, Any]


@pytest.fixture()
def fake_admin(monkeypatch: pytest.MonkeyPatch) -> _Log:
    psycopg = pytest.importorskip("psycopg")
    log = _Log()
    state: dict[str, Any] = {"role_exists": False, "audit_other": []}
    monkeypatch.setattr(
        psycopg, "connect", lambda *a, **k: _AdminConn(log, role_exists=state["role_exists"], audit_other=state["audit_other"])
    )
    log.state = state
    return log


def test_cli_applies_the_plan_audits_and_prints_the_url_once(
    fake_admin: _Log, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = {live_writer.ENV_ADMIN_URL: "postgresql://admin:AdminSecret123@10.0.0.5:5432/darkfac"}
    code = live_writer.main(["--reader-role", "darkhub_ro"], environ=environ)
    out = capsys.readouterr()
    assert code == 0, out.err
    joined = "\n".join(fake_admin)
    assert 'CREATE ROLE "darkfac_live_writer"' in joined
    assert f'GRANT INSERT ON TABLE {TABLE} TO "darkfac_live_writer"' in joined
    assert f'GRANT SELECT ON TABLE {TABLE} TO "darkhub_ro"' in joined
    assert "postgresql://darkfac_live_writer:" in out.out and "@10.0.0.5:5432/darkfac" in out.out
    assert "AdminSecret123" not in out.out + out.err
    assert live_writer.ENV_ADMIN_URL in out.err or "auditoria" in (out.out + out.err).lower()
    assert WRITE_DATABASE_URL_ENVS[0] in out.out  # tells which variable receives the URL


def test_cli_on_an_existing_role_keeps_the_password_and_prints_no_url(
    fake_admin: _Log, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_admin.state["role_exists"] = True
    environ = {live_writer.ENV_ADMIN_URL: "postgresql://admin:AdminSecret123@10.0.0.5:5432/darkfac"}
    assert live_writer.main([], environ=environ) == 0
    out = capsys.readouterr()
    joined = "\n".join(fake_admin)
    assert "ALTER ROLE" in joined and "CREATE ROLE" not in joined and "PASSWORD" not in joined
    assert "postgresql://darkfac_live_writer:" not in out.out


def test_cli_fails_when_the_audit_finds_extra_privileges(fake_admin: _Log, capsys: pytest.CaptureFixture[str]) -> None:
    fake_admin.state["audit_other"] = ["jobs"]
    environ = {live_writer.ENV_ADMIN_URL: "postgresql://admin:AdminSecret123@10.0.0.5:5432/darkfac"}
    assert live_writer.main([], environ=environ) == 1
    assert "jobs" in capsys.readouterr().err


def test_cli_without_the_admin_url_explains_how_to_set_it(capsys: pytest.CaptureFixture[str]) -> None:
    assert live_writer.main([], environ={}) == 2
    assert live_writer.ENV_ADMIN_URL in capsys.readouterr().err


def test_print_sql_needs_no_database_and_hides_the_password(capsys: pytest.CaptureFixture[str]) -> None:
    assert live_writer.main(["--print-sql", "--database", "darkfac"], environ={}) == 0
    out = capsys.readouterr().out
    assert 'GRANT INSERT ON TABLE local_run_events TO "darkfac_live_writer"' in out
    assert "<SENHA>" in out and "AdminSecret" not in out


def test_the_script_entry_point_runs_from_any_directory(tmp_path: Path) -> None:
    script = REPO / "scripts" / "provision_live_writer.py"
    assert script.is_file()
    done = subprocess.run(
        [sys.executable, str(script), "--print-sql", "--database", "darkfac"],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "GRANT INSERT ON TABLE local_run_events" in done.stdout
