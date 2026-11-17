"""Best-effort phase progress of a local ``run_ticket.py`` run for the live line board (USR-140).

The live line (``core.workflow.line_live``) projects the *autonomous line* runs stored in the control
store. A ticket executed by ``run_ticket.py`` on the Desktop/Notebook never enters that store, so the
cloud DarkHub could only show it as "off the line". This module lets the launcher publish one event
per phase transition (preflight, workspace, agent, gate, commit, pr, ci, merge, deploy) into an
append-only ``local_run_events`` table of the *same* control database the Hub already reads
(PostgreSQL in the cloud, ``control.db`` locally).

Design choices
--------------
* A dedicated table, not ``runs``/``jobs``/``claims``: those rows are owned by the coordinator,
  reconciler and workers (pending/running jobs get claimed, leased, re-queued). A foreign
  ``running`` job there could be picked up and executed by the line. An append-only side table cannot.
* The Hub reader folds the events into the existing live contract (``LiveRun``/``LiveStage``), so
  no new endpoint, authentication channel or frontend contract is needed.
* Publishing is strictly best-effort: every public method swallows every error, each write is bounded
  by a wall-clock timeout (longer than the connect plus statement budgets) in a daemon thread, and
  after repeated failures the publisher mutes itself. It can never block or change the outcome of
  ``run_ticket``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import socket
import sqlite3
import sys
import threading
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from core.orchestrator.cloud_db import (
    ResolvedControlDatabase,
    resolve_control_database,
    sanitize_database_url,
)
from core.common.time import utc_now as _utc_now

logger = logging.getLogger("darkfac.local_progress")

LOCAL_RUN_MODE = "run_ticket"
RUN_PHASE = "run"  # terminal marker of a run, not a stage
PHASES: tuple[str, ...] = (
    "preflight",
    "workspace",
    "agent",
    "gate",
    "commit",
    "pr",
    "ci",
    "merge",
    "deploy",
)
PHASE_LABELS: dict[str, str] = {
    "preflight": "Preflight",
    "workspace": "Workspace",
    "agent": "Agente",
    "gate": "Portão",
    "commit": "Commit",
    "pr": "PR",
    "ci": "CI",
    "merge": "Merge",
    "deploy": "Deploy",
}
# Upper bound a running phase may legitimately stay silent (the agent retries up to several times).
PHASE_TIMEOUT_SECONDS: dict[str, int] = {
    "preflight": 900,
    "workspace": 900,
    "agent": 7200,
    "gate": 3600,
    "commit": 900,
    "pr": 900,
    "ci": 3600,
    "merge": 900,
    "deploy": 1800,
}
# Without a terminal event for this long the process is presumed dead (killed, machine off).
ABANDON_AFTER_SECONDS = 6 * 3600

TABLE = "local_run_events"
ENV_RUN_ID = "DARKFAC_LOCAL_RUN_ID"
# "off" disables publishing; "local" keeps the machine's own control.db on purpose (a local Hub reads it).
ENV_SWITCH = "DARKFAC_LOCAL_PROGRESS"
SWITCH_OFF = frozenset({"0", "off", "false", "no"})
SWITCH_LOCAL = "local"
PROVISION_SCRIPT = r"C:\dev\DarkFac\scripts\provision_live_writer.py"
# Set for a delivery subprocess once the parent already warned: one warning per run, not per process.
ENV_WARNED = "DARKFAC_LOCAL_PROGRESS_WARNED"
RUNBOOK = "docs/runbooks/live_progress.md"
# Exact fix for a missing driver (USR-198); same pin as requirements-live.txt and the dokploy Dockerfiles.
PSYCOPG_INSTALL_CMD = 'python -m pip install "psycopg[binary]>=3.2.0,<3.3.0"'
# Progress has its own destination when DARKHUB_LINE_DATABASE_URL is set. The control store reads only
# DARKFAC_HF02_DATABASE_URL, so prefer the explicit progress writer and fall back to the control-store
# URL only when no writer-specific URL is configured. Before USR-455 T1 the control-store URL won even
# when the runbook explicitly configured a separate, reachable Hub writer endpoint.
WRITE_DATABASE_URL_ENVS: tuple[str, ...] = (
    "DARKHUB_LINE_DATABASE_URL",
    "DARKFAC_HF02_DATABASE_URL",
    "DARKHUB_CONTROL_DATABASE_URL",
)
# The old progress sink allowed only 3s to connect while the control store allowed 5s. A network path
# that answered between those bounds therefore worked for reservation reads but timed out on progress.
# Use a longer bounded default while keeping the sink's connection settings independently overridable.
ENV_CONNECT_TIMEOUT_S = "DARKFAC_LOCAL_PROGRESS_CONNECT_TIMEOUT_S"
ENV_STATEMENT_TIMEOUT_S = "DARKFAC_LOCAL_PROGRESS_STATEMENT_TIMEOUT_S"
ENV_WRITE_TIMEOUT_S = "DARKFAC_LOCAL_PROGRESS_WRITE_TIMEOUT_S"
ENV_RETRIES = "DARKFAC_LOCAL_PROGRESS_RETRIES"
DEFAULT_SINK_CONNECT_TIMEOUT_S = 10.0
DEFAULT_SINK_STATEMENT_TIMEOUT_S = 10.0
DEFAULT_WRITE_TIMEOUT_S = 30.0  # wall-clock budget for a publish; greater than connect + statement
MAX_SINK_CONNECT_TIMEOUT_S = 30.0
MAX_SINK_STATEMENT_TIMEOUT_S = 30.0
MAX_WRITE_TIMEOUT_S = 120.0
DEFAULT_SINK_RETRIES = 1
MAX_SINK_RETRIES = 2
READ_COLUMNS = "event_id, run_id, ticket_id, project_id, title, phase, status, harness, worker, cause_code, message, at"

SQLITE_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    harness TEXT,
    worker TEXT,
    cause_code TEXT,
    message TEXT,
    at TEXT NOT NULL
)
"""
POSTGRES_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    event_id BIGSERIAL PRIMARY KEY,
    run_id VARCHAR(160) NOT NULL,
    ticket_id VARCHAR(64) NOT NULL,
    project_id VARCHAR(160) NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    phase VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL,
    harness VARCHAR(64),
    worker VARCHAR(160),
    cause_code VARCHAR(64),
    message TEXT,
    at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_local_run_events_run ON {TABLE} (run_id, event_id)
"""

PhaseStatus = Literal["running", "succeeded", "failed", "skipped", "cancelled"]
_COLUMNS = ("run_id", "ticket_id", "project_id", "title", "phase", "status", "harness", "worker", "cause_code", "message", "at")
_MAX_MESSAGE = 400
_MAX_LOCAL_EVENTS = 200


class LocalRunEvent(BaseModel):
    """One phase transition of a local run (the row stored in ``local_run_events``)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=160)
    ticket_id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=160)
    title: str = ""
    phase: str = Field(min_length=1, max_length=32)
    status: PhaseStatus
    harness: str | None = None
    worker: str | None = None
    cause_code: str | None = None
    message: str = ""
    at: datetime


class ProgressSink(Protocol):
    """Where events go; implementations may raise, the publisher contains every failure."""

    def write(self, event: LocalRunEvent) -> None: ...


def _clean(text: str | None, limit: int = _MAX_MESSAGE) -> str:
    """Single-line, redacted, bounded text (the Hub is internet-facing)."""
    if not text:
        return ""
    try:
        from core.line.agent_cli import redact_secrets

        text = redact_secrets(text)
    except Exception:  # noqa: BLE001 - redaction helper unavailable: fall back to truncation only
        pass
    flat = " ".join(str(text).split())
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


def _row(event: LocalRunEvent) -> tuple[Any, ...]:
    data = event.model_dump()
    return tuple(data[name] for name in _COLUMNS)


class SqliteSink:
    """Appends to an existing local ``control.db`` (never creates the file)."""

    def __init__(self, path: Path, *, timeout_s: float = 2.0) -> None:
        self.path = Path(path)
        self.timeout_s = timeout_s
        self._ready = False

    def write(self, event: LocalRunEvent) -> None:
        with closing(sqlite3.connect(str(self.path), timeout=self.timeout_s)) as conn:
            if not self._ready:
                conn.execute(SQLITE_DDL)
                self._ready = True
            row = list(_row(event))
            row[-1] = event.at.isoformat()
            conn.execute(
                f"INSERT INTO {TABLE} ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' for _ in _COLUMNS)})", row
            )
            conn.commit()


class PostgresSink:
    """Appends to the cloud live Hub database, reusing one connection across every event of a run.

    The sink may use an explicit progress-writer URL distinct from the control store's URL. It uses
    the shared PostgreSQL connection settings model with its own bounded timeout values
    (``resolve_sink_source`` applies them; see ``ENV_CONNECT_TIMEOUT_S`` above). It reuses one
    connection across events and reconnects for a bounded, configurable number of retries before
    reporting the error to ``ProgressPublisher``.

    It inserts first and only runs the DDL when the table is missing (USR-164): the restricted writer
    role (``INSERT`` on ``local_run_events`` only, no ``CREATE`` on the schema) therefore never runs DDL,
    while a privileged role still bootstraps the table on its first write.
    """

    def __init__(self, connection: ResolvedControlDatabase | str, *, retries: int = DEFAULT_SINK_RETRIES) -> None:
        # String input remains useful for explicit test/injection construction. Production resolution
        # passes the same canonical connection object used by PostgresControlStore.
        resolved = connection if isinstance(connection, ResolvedControlDatabase) else resolve_control_database(
            database_url=connection
        )
        if resolved is None:
            raise ValueError("PostgreSQL control database is not configured")
        self.connection = resolved
        self.write_timeout_s = resolved.write_timeout_s
        self.retries = min(MAX_SINK_RETRIES, max(0, int(retries)))
        self._conn: Any = None
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        """The raw connection URL (credentials included): only for building a new connection, never logged."""
        return self.connection.url

    @property
    def connect_timeout_s(self) -> float:
        return float(self.connection.connect_timeout_s)

    def _connect(self) -> Any:
        import psycopg  # type: ignore[import-not-found]

        return psycopg.connect(self.connection.url, autocommit=True, **self.connection.psycopg_kwargs())

    def _insert(self, conn: Any, event: LocalRunEvent) -> None:
        import psycopg  # type: ignore[import-not-found]

        insert = f"INSERT INTO {TABLE} ({', '.join(_COLUMNS)}) VALUES ({', '.join('%s' for _ in _COLUMNS)})"
        try:
            conn.execute(insert, _row(event))
            return
        except psycopg.errors.UndefinedTable:
            pass  # first write ever: create the table (autocommit: the failed INSERT left no open transaction)
        for statement in POSTGRES_DDL.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.execute(insert, _row(event))

    def _discard_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - a connection broken enough to fail may already be gone
                pass
            self._conn = None

    def _write_once(self, event: LocalRunEvent) -> None:
        if self._conn is None:
            self._conn = self._connect()
        self._insert(self._conn, event)

    def write(self, event: LocalRunEvent) -> None:
        with self._lock:
            for attempt in range(self.retries + 1):
                try:
                    self._write_once(event)
                    return
                except Exception:  # noqa: BLE001 - retries are capped and bounded by the publisher
                    self._discard_connection()
                    if attempt >= self.retries:
                        raise

    def close(self) -> None:
        """Release the reused connection, if any (best-effort; the next ``write`` reconnects on demand)."""
        with self._lock:
            self._discard_connection()


CONTROL_DB_SOURCE = "control.db"


def _positive_float_env(
    env: Mapping[str, str], name: str, default: float, *, maximum: float
) -> float:
    """Read a finite positive timeout override, capped so configuration cannot make writes unbounded."""
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return min(value, maximum)


def _retry_count_env(env: Mapping[str, str]) -> int:
    """A bounded retry count; one retry is the default, and two is the hard ceiling."""
    raw = (env.get(ENV_RETRIES) or "").strip()
    if not raw:
        return DEFAULT_SINK_RETRIES
    try:
        retries = int(raw)
    except ValueError:
        return DEFAULT_SINK_RETRIES
    return min(MAX_SINK_RETRIES, max(0, retries))


def resolve_sink_source(environ: Mapping[str, str] | None = None) -> tuple[ProgressSink | None, str | None]:
    """``resolve_sink`` plus which source won: an env var name, ``"control.db"``, or ``None``.

    ``source`` is ``None`` whenever ``sink`` is ``None`` too (publishing off on purpose, or no sink at
    all): a diagnostic tool (``core.line.live_progress_doctor``, USR-455) distinguishes the two cases
    with ``missing_sink_reason``/``local_only_reason``, exactly like ``open_progress`` already does.
    """
    if environ is None:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return None, None
        env: Mapping[str, str] = os.environ
    else:
        env = environ
    switch = env.get(ENV_SWITCH, "").strip().lower()
    if switch in SWITCH_OFF:
        return None, None
    for name in () if switch == SWITCH_LOCAL else WRITE_DATABASE_URL_ENVS:
        value = env.get(name, "").strip()
        if value and not value.startswith("mock"):
            connection = resolve_control_database(env, database_url=value)
            if connection is None:
                continue
            connection = replace(
                connection,
                connect_timeout_s=_positive_float_env(
                    env, ENV_CONNECT_TIMEOUT_S, DEFAULT_SINK_CONNECT_TIMEOUT_S,
                    maximum=MAX_SINK_CONNECT_TIMEOUT_S,
                ),
                statement_timeout_ms=int(
                    _positive_float_env(
                        env, ENV_STATEMENT_TIMEOUT_S, DEFAULT_SINK_STATEMENT_TIMEOUT_S,
                        maximum=MAX_SINK_STATEMENT_TIMEOUT_S,
                    ) * 1000
                ),
                write_timeout_s=_positive_float_env(
                    env, ENV_WRITE_TIMEOUT_S, DEFAULT_WRITE_TIMEOUT_S, maximum=MAX_WRITE_TIMEOUT_S
                ),
            )
            sink = PostgresSink(connection, retries=_retry_count_env(env))
            return sink, name
    try:
        from core.paths import state_root

        local_db = state_root() / "control.db"
    except Exception:  # noqa: BLE001
        return None, None
    return (SqliteSink(local_db), CONTROL_DB_SOURCE) if local_db.is_file() else (None, None)


def resolve_sink(environ: Mapping[str, str] | None = None) -> ProgressSink | None:
    """The sink for this machine, or ``None`` (publishing off).

    With the default environment, ``pytest`` runs never publish (a developer machine may carry the
    production database URL). Tests pass an explicit mapping or inject a sink instead.
    """
    return resolve_sink_source(environ)[0]


def missing_sink_reason(environ: Mapping[str, str] | None = None) -> str | None:
    """Why no sink could be resolved, or ``None`` when publishing is off on purpose.

    Off on purpose: running under ``pytest`` with the default environment, or the explicit
    ``DARKFAC_LOCAL_PROGRESS=off`` switch. Any other absence of a sink is a configuration gap the
    operator should hear about (USR-154).
    """
    if environ is None:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return None
        env: Mapping[str, str] = os.environ
    else:
        env = environ
    if env.get(ENV_SWITCH, "").strip().lower() in SWITCH_OFF:
        return None
    names = " ou ".join(WRITE_DATABASE_URL_ENVS[:2])
    if any(env.get(name, "").strip() for name in WRITE_DATABASE_URL_ENVS):
        return f"as variaveis de banco definidas nao apontam para um banco real (valor mock); defina {names}"
    return f"variavel de banco ausente: defina {names} com um usuario que possa gravar"


def local_only_reason(environ: Mapping[str, str] | None = None) -> str | None:
    """Why a resolved ``SqliteSink`` is a visibility gap (USR-164), or ``None`` when local on purpose.

    The cloud DarkHub never reads a machine's own ``control.db``: a run recorded only there is invisible
    on ``/live``. The only deliberate case is ``DARKFAC_LOCAL_PROGRESS=local`` (a Hub running on this
    machine reads that file).
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    if env.get(ENV_SWITCH, "").strip().lower() == SWITCH_LOCAL:
        return None
    names = " ou ".join(WRITE_DATABASE_URL_ENVS[:2])
    return (
        f"defina {names} com o Postgres da nuvem (usuario restrito a {TABLE}, "
        f"provisionado por {PROVISION_SCRIPT}); se um Hub local le este control.db de proposito, "
        f"use {ENV_SWITCH}=local"
    )


CauseCode = Literal[
    "no_url",
    "missing_driver",
    "dns_error",
    "tcp_timeout",
    "connection_refused",
    "auth_failed",
    "missing_table",
    "read_only",
    "unknown",
]
# Connection-layer causes grouped under the same "banco inalcancavel ou conexao recusada" wording in
# describe_publish_failure (USR-455 T2): distinct enough to diagnose with (classify_connection_cause),
# but close enough in remediation to share the operator-facing sentence that existing warnings already use.
_CONNECTION_CAUSES = frozenset({"dns_error", "tcp_timeout", "connection_refused", "auth_failed"})
CAUSE_FIXES: dict[CauseCode, str] = {
    "no_url": f"defina {' ou '.join(WRITE_DATABASE_URL_ENVS[:2])} com um usuario que possa gravar",
    "missing_driver": f"instale o driver: {PSYCOPG_INSTALL_CMD}",
    "dns_error": "o host da URL nao resolveu via DNS; confira o nome/IP configurado e a tailnet",
    "tcp_timeout": (
        "a rede nao respondeu dentro do tempo de conexao do sink; confira firewall/tailnet"
    ),
    "connection_refused": "a porta esta fechada ou o servico esta fora do ar no host/porta configurados",
    "auth_failed": f"usuario ou senha incorretos na URL de conexao; reprovisione com {PROVISION_SCRIPT}",
    "missing_table": f"tabela {TABLE} ausente e o usuario nao tem CREATE; reprovisione com {PROVISION_SCRIPT}",
    "read_only": f"sem permissao de escrita em {TABLE}; use o papel restrito criado por {PROVISION_SCRIPT}",
    "unknown": "causa nao identificada; verifique o tipo de excecao nos logs",
}


def classify_connection_cause(exc: BaseException) -> CauseCode:
    """Which of a short, closed set of causes explains a sink failure (USR-455 T2).

    USR-454 showed ``local_progress`` warning the operator with one generic "banco inalcancavel"
    bucket for every connectivity problem (DNS failure, TCP timeout, refused connection, bad
    credentials, ...), which gives no actionable next step. This turns the same exception into one of
    a fixed, credential-free set of codes, each with its own remediation in ``CAUSE_FIXES`` -- used by
    both the publisher's own warning (``describe_publish_failure``) and ``live_progress_doctor``.
    """
    name = type(exc).__name__
    text = str(exc).lower()
    if isinstance(exc, ImportError):
        return "missing_driver"
    if (
        name == "InsufficientPrivilege"
        or "permission denied" in text
        or "readonly" in text
        or "read-only" in text
        or "must be owner" in text
    ):
        return "read_only"
    if "undefinedtable" in name.lower() or ("relation" in text and "does not exist" in text):
        return "missing_table"
    if (
        "password authentication failed" in text
        or "authentication failed" in text
        or ("role" in text and "does not exist" in text)
    ):
        return "auth_failed"
    if (
        isinstance(exc, socket.gaierror)
        or "name or service not known" in text
        or "nodename nor servname" in text
        or "getaddrinfo failed" in text
        or "temporary failure in name resolution" in text
        or "could not translate host name" in text
    ):
        return "dns_error"
    # psycopg names its connect failure ConnectionTimeout; the message does not always contain "timeout".
    if isinstance(exc, TimeoutError) or name == "ConnectionTimeout":
        return "tcp_timeout"
    if isinstance(exc, ConnectionRefusedError) or "connection refused" in text or "actively refused" in text:
        return "connection_refused"
    if "timeout" in text or "timed out" in text:
        return "tcp_timeout"
    if isinstance(exc, OSError) or name in {"OperationalError", "InterfaceError"} or "connect" in text:
        return "connection_refused"
    return "unknown"


def describe_publish_failure(exc: BaseException) -> str:
    """Short, credential-free reason for a failed write (never echoes the driver message)."""
    cause = classify_connection_cause(exc)
    if cause == "missing_driver":
        return f"driver psycopg nao instalado neste ambiente: instale com {PSYCOPG_INSTALL_CMD}"
    if cause == "read_only":
        return (
            f"sem permissao de escrita em {TABLE}: use o papel restrito criado por {PROVISION_SCRIPT} "
            f"(ou conceda INSERT e USAGE na sequence ao usuario do banco)"
        )
    if cause == "missing_table":
        return f"tabela {TABLE} ausente e sem privilegio de CREATE: reprovisione com {PROVISION_SCRIPT}"
    if cause in _CONNECTION_CAUSES:
        return f"banco inalcancavel ou conexao recusada (host, porta, credenciais ou rede) - {CAUSE_FIXES[cause]}"
    return f"falha ao gravar ({type(exc).__name__})"


def stderr_warning(text: str) -> None:
    """Default warning channel: stderr only, so ``--json`` consumers keep a clean stdout."""
    try:
        print(text, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 - a closed or odd stderr must not matter
        pass


def _run_bounded(fn: Callable[[], None], timeout_s: float) -> None:
    """Run ``fn`` in a daemon thread; raise on error or if it exceeds ``timeout_s``."""
    outcome: list[BaseException] = []

    def target() -> None:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised to the caller below
            outcome.append(exc)

    thread = threading.Thread(target=target, name="local-progress-write", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise TimeoutError(f"progress write exceeded {timeout_s:g}s")
    if outcome:
        raise outcome[0]


class ProgressPublisher:
    """Publishes the phases of one ``run_ticket`` run. Public methods never raise."""

    def __init__(
        self,
        *,
        ticket_id: str,
        project_id: str = "darkfac",
        title: str = "",
        run_id: str | None = None,
        sink: ProgressSink | None = None,
        worker: str | None = None,
        harness: str | None = None,
        owns_run: bool = True,
        timeout_s: float = DEFAULT_WRITE_TIMEOUT_S,
        max_consecutive_failures: int = 2,
        clock: Callable[[], datetime] = _utc_now,
        warn: Callable[[str], None] | None = None,
        unconfigured_reason: str | None = None,
        local_only_reason: str | None = None,
        active_phase: str | None = None,
    ) -> None:
        self.ticket_id = ticket_id
        self.project_id = project_id or "darkfac"
        self.title = _clean(title, 240)
        self.sink = sink
        self.worker = worker or _hostname()
        self.harness = harness
        self.owns_run = owns_run
        self.timeout_s = timeout_s
        self.max_consecutive_failures = max_consecutive_failures
        self._clock = clock
        self._warn = warn
        self._unconfigured_reason = unconfigured_reason
        self._local_only_reason = local_only_reason
        self.warning: str | None = None  # the single warning of this run, once emitted
        self.warning_reason: str | None = None  # same event, short and without the "[AVISO] ..." wrapper
        self.warning_cause_code: str | None = None  # structured cause of that same warning (USR-455 T2)
        self.warning_phase: str | None = None  # first phase that failed to reach the sink (USR-455 T2)
        self.unpublished_phases = 0  # phase events absent from the cloud Hub; local event history is retained
        self._warning_pending = False
        self._failures = 0
        self._muted = False
        self._finished = False
        self._held = False
        self._discarded = False
        self._buffer: list[LocalRunEvent] = []
        self._outcome_message = ""
        self._outcome_cause: str | None = None
        self._active_phase: str | None = active_phase  # last phase published as running and not yet closed
        self.run_id = run_id or self._new_run_id()
        self.events: list[LocalRunEvent] = []  # what was attempted (capped), for diagnostics and tests
        # Phase observers (USR-208 execution registry): (phase, status, message, cause, harness).
        self._observers: list[Callable[..., None]] = []

    def _new_run_id(self) -> str:
        return f"local-{self.ticket_id}-{self._clock().strftime('%Y%m%dT%H%M%S')}"[:160]

    @property
    def active_phase(self) -> str | None:
        """The phase published as ``running`` and not yet closed (``None`` when none)."""
        return self._active_phase

    @property
    def failures(self) -> int:
        """Consecutive failed writes (0 after any successful one)."""
        return self._failures

    @property
    def enabled(self) -> bool:
        return self.sink is not None and not self._muted and not self._discarded

    @property
    def hub_visible(self) -> bool:
        """Whether every write attempted so far reached a sink the cloud Hub actually reads (USR-455 T3).

        The machine's own ``control.db`` fallback does not count: the cloud Hub never reads it
        (USR-164), so a run recorded only there must report as not visible even with zero failures.
        """
        return (
            self.sink is not None
            and not isinstance(self.sink, SqliteSink)
            and self._failures == 0
            and self.warning is None
        )

    def publish_status(self) -> dict[str, Any]:
        """Credential-free ``{"published": bool, "warning": str | None, "cause_code": str | None}``.

        ``warning`` is the short reason (``warning_reason``), not the full ``[AVISO] Esteira ao vivo``
        sentence already printed once when it happened: a summary line must not repeat that text.
        ``cause_code`` (USR-455 T2) is the structured classification of that same warning (one of
        ``CauseCode``), so a caller can act on it without parsing the free-text reason. The single
        summary ``run_ticket`` (and ``scripts/live_run.py``) report to the operator so a silent publish
        failure is visible in the launcher's own output, never only in a log file.
        """
        status = {
            "published": self.hub_visible,
            "warning": self.warning_reason,
            "cause_code": self.warning_cause_code,
            "unpublished_phases": self.unpublished_phases,
        }
        if self.warning_reason and self.warning_phase and self.warning_cause_code:
            status["failure"] = {
                "phase": self.warning_phase,
                "category": self.warning_cause_code,
                "message": _clean(self.warning_reason),
            }
        return status

    def add_observer(self, observer: Callable[..., None]) -> None:
        """Register a callback for every phase transition (errors are swallowed, never reach the run)."""
        self._observers.append(observer)

    def hold(self) -> None:
        """Buffer events instead of publishing them until ``release`` (or drop them with ``discard``)."""
        self._held = True

    def release(self) -> None:
        """Stop holding and publish whatever was buffered, in order."""
        try:
            self._held = False
            pending, self._buffer = self._buffer, []
            for event in pending:
                self._send(event)
        except Exception as exc:  # noqa: BLE001
            logger.warning("local progress: release failed: %s", type(exc).__name__)

    def discard(self) -> None:
        """The run turned out to be a no-op (dry run): publish nothing, now or later."""
        self._discarded = True
        self._buffer = []

    def set_outcome(self, message: str, cause: str | None = None, *, only_if_unset: bool = False) -> None:
        """Remember why the run failed so ``finish(False)`` can report it."""
        if only_if_unset and (self._outcome_message or self._outcome_cause):
            return
        self._outcome_message = message
        self._outcome_cause = cause

    def phase(
        self,
        phase: str,
        status: PhaseStatus,
        message: str = "",
        *,
        cause: str | None = None,
        harness: str | None = None,
        worker: str | None = None,
    ) -> None:
        """Record a phase transition."""
        try:
            if harness:
                self.harness = harness
            self._publish(phase, status, message, cause, worker=worker)
        except Exception as exc:  # noqa: BLE001 - publishing must never affect the run
            logger.warning("local progress: could not build %s/%s event: %s", phase, status, type(exc).__name__)

    def finish(self, ok: bool, message: str = "", *, cause: str | None = None) -> None:
        """Close the run (only the process that opened it; a delivery subprocess joins and never closes)."""
        if not self.owns_run or self._finished:
            return
        self._finished = True
        if ok:
            self.phase(RUN_PHASE, "succeeded", message)
        else:
            self.phase(
                RUN_PHASE, "failed", message or self._outcome_message, cause=cause or self._outcome_cause
            )
        self._flush_warning()

    def cancel(self, message: str = "", *, cause: str = "owner_cancelled") -> None:
        """Record that the run was cancelled (USR-166): the phase in progress and, if owned, the run.

        The board shows the phase as ``cancelada`` and the run as cancelled. Idempotent; never raises.
        """
        try:
            if self._finished:
                return
            text = message or "execucao cancelada pelo owner"
            if self._active_phase is not None:
                self.phase(self._active_phase, "cancelled", text, cause=cause)
            if self.owns_run:
                self._finished = True
                self.phase(RUN_PHASE, "cancelled", text, cause=cause)
            self._flush_warning()
        except Exception as exc:  # noqa: BLE001
            logger.warning("local progress: cancel failed: %s", type(exc).__name__)

    def _publish(
        self, phase: str, status: PhaseStatus, message: str, cause: str | None, *, worker: str | None = None
    ) -> None:
        if phase != RUN_PHASE:
            if status == "running":
                self._active_phase = phase
            elif self._active_phase == phase:
                self._active_phase = None
        event = LocalRunEvent(
            run_id=self.run_id,
            ticket_id=self.ticket_id,
            project_id=self.project_id,
            title=self.title,
            phase=phase,
            status=status,
            harness=self.harness,
            worker=worker or self.worker,
            cause_code=(cause[:64] if cause else None),
            message=_clean(message),
            at=self._clock(),
        )
        if self._discarded:
            return
        for observer in list(self._observers):
            try:
                observer(phase, status, message, cause, self.harness)
            except Exception as exc:  # noqa: BLE001 - observers must never affect the run
                logger.warning("local progress: observer failed: %s", type(exc).__name__)
        if len(self.events) < _MAX_LOCAL_EVENTS:
            self.events.append(event)
        if self._held:
            self._buffer.append(event)
            return
        self._send(event)

    def _emit_warning(
        self, reason: str, *, local_only: bool = False, cause_code: str | None = None, phase: str | None = None
    ) -> None:
        """Remember the run's degradation; emit it once all phase events have been accounted for."""
        if self.warning is not None or self._warning_pending:
            return
        try:
            self.warning_reason = _clean(reason)
            self.warning_phase = phase if phase in {*PHASES, RUN_PHASE} else "unknown"
            self.warning_cause_code = cause_code or ("local_only" if local_only else None)
            self._warning_pending = True
        except Exception:  # noqa: BLE001
            pass

    def _flush_warning(self) -> None:
        """Emit the unique structured warning after all phases have been accounted for."""
        if not self._warning_pending or self.warning is not None:
            return
        try:
            payload = {
                "event": "live_progress_degraded",
                "ticket_id": self.ticket_id,
                "phase": self.warning_phase,
                "category": self.warning_cause_code,
                "cause_code": self.warning_cause_code,
                "reason": self.warning_reason,
                "unpublished_phases": self.unpublished_phases,
                "doctor": "python -m core.line.live_progress_doctor",
                "runbook": RUNBOOK,
            }
            self.warning = sanitize_database_url(
                "[AVISO] Esteira ao vivo: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
            self._warning_pending = False
            if self._warn is not None:
                self._warn(self.warning)
        except Exception:  # noqa: BLE001
            pass
    def _record_unpublished(self, event: LocalRunEvent) -> None:
        # Events are appended to ``events`` before reaching this point, so the local diagnostic
        # history remains complete even when the remote sink is unavailable or muted.
        self.unpublished_phases += 1

    def _send(self, event: LocalRunEvent) -> None:
        if self._discarded:
            return
        if self.sink is None:
            self._record_unpublished(event)
            if self._unconfigured_reason:
                self._emit_warning(self._unconfigured_reason, cause_code="no_url", phase=event.phase)
            return
        if not self.enabled:
            self._record_unpublished(event)
            return
        if self._local_only_reason:
            self._record_unpublished(event)
            self._emit_warning(self._local_only_reason, local_only=True, phase=event.phase)
        sink = self.sink
        phase, status = event.phase, event.status
        try:
            _run_bounded(lambda: sink.write(event), self.timeout_s)
            self._failures = 0
        except Exception as exc:  # noqa: BLE001 - network, driver, lock, timeout: all contained
            if not self._local_only_reason:
                self._record_unpublished(event)
            self._failures += 1
            cause_code = classify_connection_cause(exc)
            self._emit_warning(describe_publish_failure(exc), cause_code=cause_code, phase=event.phase)
            logger.warning(
                "local progress: publish failed for %s %s/%s (cause=%s exception=%s)",
                self.run_id,
                phase,
                status,
                cause_code,
                type(exc).__name__,
            )
            if self._failures >= self.max_consecutive_failures:
                self._muted = True
                logger.warning("local progress: publishing muted for %s after repeated failures", self.run_id)


def _hostname() -> str:
    try:
        return socket.gethostname() or "unknown-host"
    except Exception:  # noqa: BLE001
        return "unknown-host"


def open_progress(
    ticket_id: str,
    project_id: str = "darkfac",
    title: str = "",
    *,
    sink: ProgressSink | None = None,
    environ: Mapping[str, str] | None = None,
    harness: str | None = None,
    warn: Callable[[str], None] | None | Literal["default"] = "default",
    run_id: str | None = None,
    worker: str | None = None,
    active_phase: str | None = None,
) -> ProgressPublisher:
    """A publisher for ``ticket_id``; joins the parent run when ``DARKFAC_LOCAL_RUN_ID`` is set.

    Never raises: any failure resolving the sink yields a publisher that simply does not publish.
    ``warn`` receives the single "cannot publish" warning of the run (USR-154). By default it goes to
    stderr, except under ``pytest`` or when a parent process already warned; pass ``None`` to silence it.
    The same warning fires when the resolved sink is the machine's own ``control.db`` (USR-164): the
    cloud Hub never reads it. ``run_id``/``worker``/``active_phase`` resume a run opened by another
    process (the interactive-session CLI); they override the inherited parent run.
    """
    try:
        env = os.environ if environ is None else environ
        inherited = env.get(ENV_RUN_ID, "").strip()
        resolved = sink if sink is not None else resolve_sink(environ)
        if warn == "default":
            already = bool(env.get(ENV_WARNED, "").strip())
            under_pytest = environ is None and bool(os.environ.get("PYTEST_CURRENT_TEST"))
            warn = None if already or under_pytest else stderr_warning
        reason = missing_sink_reason(environ) if resolved is None and warn is not None else None
        local_reason = (
            local_only_reason(environ)
            if sink is None and isinstance(resolved, SqliteSink) and warn is not None
            else None
        )
        return ProgressPublisher(
            ticket_id=ticket_id,
            project_id=project_id,
            title=title,
            run_id=run_id or inherited or None,
            sink=resolved,
            worker=worker,
            harness=harness,
            owns_run=not (inherited and not run_id),
            timeout_s=(resolved.write_timeout_s if isinstance(resolved, PostgresSink) else DEFAULT_WRITE_TIMEOUT_S),
            warn=warn,
            unconfigured_reason=reason,
            local_only_reason=local_reason,
            active_phase=active_phase,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("local progress: disabled (%s)", type(exc).__name__)
        return ProgressPublisher(ticket_id=ticket_id or "unknown", project_id=project_id, title=title, sink=None)
