"""Provisioning of the restricted writer role for the live line board (USR-164).

Every machine that runs ``run_ticket.py`` (or an interactive Claude Code session that calls
``scripts/live_run.py``) must publish phase events to the *cloud* control database, because the cloud
DarkHub never reads a machine's local ``control.db``. Handing each machine the full control-store writer
is more privilege than publishing progress needs, so this module builds an idempotent plan for a role
that can do exactly one thing: ``INSERT`` into ``local_run_events``.

* The *administrator* creates the table and the index (the writer therefore never needs ``CREATE`` on the
  schema: ``PostgresSink`` inserts first and only runs DDL when the table is missing).
* The writer gets ``CONNECT`` on the database, ``USAGE`` on the schema, ``INSERT`` on the table and
  ``USAGE`` on its ``BIGSERIAL`` sequence. Nothing else; ``CREATE`` on the schema is explicitly revoked.
* An optional reader role (the Hub's read-only role) gets ``SELECT`` on the table.
* A read-only audit (``has_*_privilege``) proves the result: no schema ``CREATE``, ``INSERT`` on the
  table, and no privilege at all on any other table of the schema.

No credential is ever stored: the administrator URL comes from an environment variable, the writer
password is generated (or supplied by the owner through an environment variable) and the resulting URL is
printed once to the operator's terminal.

Usage (see ``docs/runbooks/live_progress.md``)::

    python C:\\dev\\DarkFac\\scripts\\provision_live_writer.py --print-sql
    python C:\\dev\\DarkFac\\scripts\\provision_live_writer.py --reader-role <papel-de-leitura-do-hub>
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from core.line.local_progress import POSTGRES_DDL, TABLE, WRITE_DATABASE_URL_ENVS

DEFAULT_WRITER_ROLE = "darkfac_live_writer"
ENV_ADMIN_URL = "DARKFAC_ADMIN_DATABASE_URL"
ENV_WRITER_PASSWORD = "DARKFAC_LIVE_WRITER_PASSWORD"
CONNECTION_LIMIT = 10
SEQUENCE = f"{TABLE}_event_id_seq"
RUNBOOK = "docs/runbooks/live_progress.md"

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
# Passwords go into a SQL literal: restrict the alphabet instead of trying to escape everything.
_PASSWORD = re.compile(r"^[A-Za-z0-9_.~+/=-]{16,128}$")
_ROLE_ATTRIBUTES = (
    f"LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT {CONNECTION_LIMIT}"
)


@dataclass(frozen=True)
class Step:
    """One SQL statement of the plan: ``sql`` is executed, ``display`` is what may be printed."""

    description: str
    sql: str
    display: str = ""

    def __post_init__(self) -> None:
        if not self.display:
            object.__setattr__(self, "display", self.sql)


@dataclass
class AuditResult:
    ok: bool
    problems: list[str] = field(default_factory=list)


def generate_password() -> str:
    """A strong throwaway-safe password (URL-safe alphabet, 43 characters)."""
    return secrets.token_urlsafe(32)


def _identifier(name: str, what: str) -> str:
    if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"{what} invalido: use letras, digitos e _ (ate 63 caracteres, sem comecar por digito)")
    return '"' + name + '"'


def _password_literal(password: str) -> str:
    if not _PASSWORD.fullmatch(password):
        raise ValueError("senha invalida: use 16 a 128 caracteres entre letras, digitos e _.~+/=-")
    return "'" + password + "'"


def build_plan(
    *,
    writer_role: str,
    database: str,
    create_role: bool,
    password: str | None,
    reader_role: str | None = None,
) -> list[Step]:
    """The idempotent statements that provision (or realign) the restricted writer.

    ``create_role`` selects ``CREATE ROLE`` (needs ``password``) or ``ALTER ROLE`` (password only when
    given: that is the rotation). Every statement is safe to repeat.
    """
    writer = _identifier(writer_role, "papel de escrita")
    db = _identifier(database, "banco")
    reader = _identifier(reader_role, "papel de leitura") if reader_role is not None else None
    plan: list[Step] = []

    if create_role:
        if password is None:
            raise ValueError("criar o papel exige uma senha")
        literal = _password_literal(password)
        plan.append(
            Step(
                "criar o papel de escrita",
                f"CREATE ROLE {writer} {_ROLE_ATTRIBUTES} PASSWORD {literal}",
                f"CREATE ROLE {writer} {_ROLE_ATTRIBUTES} PASSWORD '<SENHA>'",
            )
        )
    elif password is not None:
        literal = _password_literal(password)
        plan.append(
            Step(
                "realinhar atributos e trocar a senha do papel de escrita",
                f"ALTER ROLE {writer} WITH {_ROLE_ATTRIBUTES} PASSWORD {literal}",
                f"ALTER ROLE {writer} WITH {_ROLE_ATTRIBUTES} PASSWORD '<SENHA>'",
            )
        )
    else:
        plan.append(
            Step("realinhar atributos do papel de escrita", f"ALTER ROLE {writer} WITH {_ROLE_ATTRIBUTES}")
        )

    for statement in (s.strip() for s in POSTGRES_DDL.split(";")):
        if statement:
            plan.append(Step("criar a tabela e o indice como administrador (dono)", statement))
    plan.extend(
        [
            Step("permitir conectar", f"GRANT CONNECT ON DATABASE {db} TO {writer}"),
            Step("permitir resolver nomes no schema", f"GRANT USAGE ON SCHEMA public TO {writer}"),
            Step("permitir somente inserir eventos", f"GRANT INSERT ON TABLE {TABLE} TO {writer}"),
            Step("permitir usar a sequence do event_id", f"GRANT USAGE ON SEQUENCE {SEQUENCE} TO {writer}"),
            Step("garantir que nao cria objetos", f"REVOKE CREATE ON SCHEMA public FROM {writer}"),
        ]
    )
    if reader is not None:
        plan.append(Step("deixar o Hub ler os eventos", f"GRANT SELECT ON TABLE {TABLE} TO {reader}"))
    return plan


def evaluate_audit(
    *,
    schema_create: bool,
    table_insert: bool,
    sequence_usage: bool,
    other_privileged_tables: Sequence[str],
    superuser: bool,
) -> AuditResult:
    """Turn the raw privilege facts into a verdict (pure, so it is testable without a database)."""
    problems: list[str] = []
    if superuser:
        problems.append("o papel e superuser")
    if schema_create:
        problems.append(
            "o papel tem CREATE no schema public (revogue tambem de PUBLIC: "
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC)"
        )
    if not table_insert:
        problems.append(f"o papel nao tem INSERT em {TABLE}")
    if not sequence_usage:
        problems.append(f"o papel nao tem USAGE na sequence {SEQUENCE} (INSERT falharia)")
    if other_privileged_tables:
        problems.append("o papel tem privilegios em outras tabelas: " + ", ".join(sorted(other_privileged_tables)))
    return AuditResult(ok=not problems, problems=problems)


def database_name(admin_url: str) -> str:
    name = urlsplit(admin_url).path.lstrip("/")
    if not name:
        raise ValueError("a URL de administrador precisa nomear o banco (postgresql://usuario@host:5432/banco)")
    return name


def writer_url(admin_url: str, writer_role: str, password: str) -> str:
    """The URL the machines use: host, port, database and options of the admin URL, new credentials."""
    parts = urlsplit(admin_url)
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    query = f"?{parts.query}" if parts.query else ""
    return f"postgresql://{quote(writer_role, safe='')}:{quote(password, safe='')}@{host}{port}/{database_name(admin_url)}{query}"


_AUDIT_SQL = (
    "SELECT has_schema_privilege(%s, 'public', 'CREATE'), "
    f"has_table_privilege(%s, '{TABLE}', 'INSERT'), "
    f"has_sequence_privilege(%s, '{SEQUENCE}', 'USAGE'), "
    "COALESCE((SELECT rolsuper FROM pg_roles WHERE rolname = %s), FALSE)"
)
_OTHER_TABLES_SQL = (
    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
    f"AND c.relname <> '{TABLE}' AND ("
    "has_table_privilege(%s, c.oid, 'SELECT') OR has_table_privilege(%s, c.oid, 'INSERT') "
    "OR has_table_privilege(%s, c.oid, 'UPDATE') OR has_table_privilege(%s, c.oid, 'DELETE')) "
    "ORDER BY c.relname"
)


def audit_role(conn: object, writer_role: str) -> AuditResult:
    """Read-only audit of ``writer_role`` through an admin connection (``conn.execute``)."""
    execute = conn.execute  # type: ignore[attr-defined]
    row = execute(_AUDIT_SQL, (writer_role,) * 4).fetchone()
    others = [r[0] for r in execute(_OTHER_TABLES_SQL, (writer_role,) * 4).fetchall()]
    return evaluate_audit(
        schema_create=bool(row[0]),
        table_insert=bool(row[1]),
        sequence_usage=bool(row[2]),
        superuser=bool(row[3]),
        other_privileged_tables=others,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provision_live_writer.py",
        description=f"Cria/realinha o papel de escrita restrito a {TABLE} (USR-164). Idempotente.",
    )
    parser.add_argument("--writer-role", default=DEFAULT_WRITER_ROLE, help="nome do papel (padrao: %(default)s)")
    parser.add_argument("--reader-role", default=None, help="papel de leitura do Hub que recebe SELECT na tabela")
    parser.add_argument("--database", default=None, help="nome do banco (padrao: o da URL de administrador)")
    parser.add_argument(
        "--rotate-password",
        action="store_true",
        help="troca a senha de um papel que ja existe (por padrao ele e mantido)",
    )
    parser.add_argument("--print-sql", action="store_true", help="so imprime o SQL (senha oculta); nao conecta")
    return parser


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    """CLI. Exit 0 on success, 1 when the audit fails or the database errs, 2 on usage/config errors."""
    env = os.environ if environ is None else environ
    args = _parser().parse_args(list(argv) if argv is not None else None)
    if args.print_sql:
        try:
            plan = build_plan(
                writer_role=args.writer_role,
                database=args.database or "darkfac",
                create_role=True,
                password="X" * 16,  # placeholder: `display` shows <SENHA> instead
                reader_role=args.reader_role,
            )
        except ValueError as exc:
            _err(f"[ERRO] {exc}")
            return 2
        print("-- Execute como administrador do Postgres. Troque <SENHA> por uma senha forte.")
        print("-- Se o papel ja existir, troque CREATE ROLE por ALTER ROLE (ou use o script sem --print-sql).")
        for step in plan:
            print(f"-- {step.description}\n{step.display};")
        return 0

    admin_url = env.get(ENV_ADMIN_URL, "").strip()
    if not admin_url:
        _err(
            f"[ERRO] defina {ENV_ADMIN_URL} com a URL de um administrador do Postgres "
            f"(postgresql://usuario:senha@host:5432/banco) na sessao atual do terminal. Veja {RUNBOOK}."
        )
        return 2
    try:
        database = args.database or database_name(admin_url)
        supplied = env.get(ENV_WRITER_PASSWORD, "").strip() or None
        import psycopg  # type: ignore[import-not-found]

        with psycopg.connect(admin_url, connect_timeout=10, autocommit=True) as conn:
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (args.writer_role,)).fetchone()
            create = exists is None
            password = None
            if create or args.rotate_password:
                password = supplied or generate_password()
            plan = build_plan(
                writer_role=args.writer_role,
                database=database,
                create_role=create,
                password=password,
                reader_role=args.reader_role,
            )
            for step in plan:
                conn.execute(step.sql)
                print(f"[ok] {step.description}", file=sys.stderr, flush=True)
            audit = audit_role(conn, args.writer_role)
    except ValueError as exc:
        _err(f"[ERRO] {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - report the class only: driver messages can echo the URL
        _err(f"[ERRO] falha ao provisionar ({type(exc).__name__}): confira a URL, a rede e o papel de administrador.")
        return 1

    if not audit.ok:
        _err("[ERRO] auditoria de privilegios reprovada:")
        for problem in audit.problems:
            _err(f"  - {problem}")
        return 1
    _err(f"[ok] auditoria: {args.writer_role} so insere em {TABLE} (sem CREATE no schema, sem outras tabelas)")
    if password is None:
        _err("[info] o papel ja existia: senha mantida (use --rotate-password para trocar). Nada a configurar.")
        return 0
    print(f"{WRITE_DATABASE_URL_ENVS[0]}={writer_url(admin_url, args.writer_role, password)}")
    _err(
        f"[info] a linha acima e a URL do papel de escrita: guarde-a agora (nao sera mostrada de novo) e defina "
        f"{WRITE_DATABASE_URL_ENVS[0]} em cada maquina. Passo a passo em {RUNBOOK}."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
