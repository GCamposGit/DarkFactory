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
  by a short timeout in a daemon thread, and after repeated failures the publisher mutes itself. It
  can never block, slow down noticeably or change the outcome of ``run_ticket``.
"""

from __future__ import annotations

import logging
import os
import socket
import sqlite3
import sys
import threading
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from core.orchestrator.cloud_db import sanitize_database_url

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
ENV_SWITCH = "DARKFAC_LOCAL_PROGRESS"
# Set for a delivery subprocess once the parent already warned: one warning per run, not per process.
ENV_WARNED = "DARKFAC_LOCAL_PROGRESS_WARNED"
RUNBOOK = "docs/runbooks/live_progress.md"
# Writer-capable URLs first: the Hub's read-only role (DARKHUB_CONTROL_DATABASE_URL) is the last resort.
WRITE_DATABASE_URL_ENVS: tuple[str, ...] = (
    "DARKFAC_HF02_DATABASE_URL",
    "DARKHUB_LINE_DATABASE_URL",
    "DARKHUB_CONTROL_DATABASE_URL",
)
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

PhaseStatus = Literal["running", "succeeded", "failed", "skipped"]
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
    """Appends to the cloud control database with short connect/statement timeouts."""

    def __init__(self, url: str, *, connect_timeout_s: int = 3) -> None:
        self._url = url
        self.connect_timeout_s = connect_timeout_s
        self._ready = False

    def write(self, event: LocalRunEvent) -> None:
        import psycopg  # type: ignore[import-not-found]

        with psycopg.connect(
            self._url,
            connect_timeout=self.connect_timeout_s,
            autocommit=True,
            options="-c statement_timeout=3000",
        ) as conn:
            if not self._ready:
                for statement in POSTGRES_DDL.split(";"):
                    if statement.strip():
                        conn.execute(statement)
                self._ready = True
            conn.execute(
                f"INSERT INTO {TABLE} ({', '.join(_COLUMNS)}) VALUES ({', '.join('%s' for _ in _COLUMNS)})",
                _row(event),
            )


def resolve_sink(environ: Mapping[str, str] | None = None) -> ProgressSink | None:
    """The sink for this machine, or ``None`` (publishing off).

    With the default environment, ``pytest`` runs never publish (a developer machine may carry the
    production database URL). Tests pass an explicit mapping or inject a sink instead.
    """
    if environ is None:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return None
        env: Mapping[str, str] = os.environ
    else:
        env = environ
    if env.get(ENV_SWITCH, "").strip().lower() in {"0", "off", "false", "no"}:
        return None
    for name in WRITE_DATABASE_URL_ENVS:
        value = env.get(name, "").strip()
        if value and not value.startswith("mock"):
            return PostgresSink(value)
    try:
        from core.paths import state_root

        local_db = state_root() / "control.db"
    except Exception:  # noqa: BLE001
        return None
    return SqliteSink(local_db) if local_db.is_file() else None


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
    if env.get(ENV_SWITCH, "").strip().lower() in {"0", "off", "false", "no"}:
        return None
    names = " ou ".join(WRITE_DATABASE_URL_ENVS[:2])
    if any(env.get(name, "").strip() for name in WRITE_DATABASE_URL_ENVS):
        return f"as variaveis de banco definidas nao apontam para um banco real (valor mock); defina {names}"
    return f"variavel de banco ausente: defina {names} com um usuario que possa gravar"


def describe_publish_failure(exc: BaseException) -> str:
    """Short, credential-free reason for a failed write (never echoes the driver message)."""
    name = type(exc).__name__
    text = str(exc).lower()
    if isinstance(exc, ImportError):
        return "driver psycopg nao instalado neste ambiente"
    if (
        name == "InsufficientPrivilege"
        or "permission denied" in text
        or "readonly" in text
        or "read-only" in text
        or "must be owner" in text
    ):
        return f"sem permissao de escrita: o usuario do banco precisa de CREATE e INSERT em {TABLE}"
    if isinstance(exc, (OSError, TimeoutError)) or name in {"OperationalError", "InterfaceError"} or "connect" in text:
        return "banco inalcancavel ou conexao recusada (host, porta, credenciais ou rede)"
    return f"falha ao gravar ({name})"


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
        timeout_s: float = 5.0,
        max_consecutive_failures: int = 2,
        clock: Callable[[], datetime] = _utc_now,
        warn: Callable[[str], None] | None = None,
        unconfigured_reason: str | None = None,
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
        self.warning: str | None = None  # the single warning of this run, once emitted
        self._failures = 0
        self._muted = False
        self._finished = False
        self._held = False
        self._discarded = False
        self._buffer: list[LocalRunEvent] = []
        self._outcome_message = ""
        self._outcome_cause: str | None = None
        self.run_id = run_id or self._new_run_id()
        self.events: list[LocalRunEvent] = []  # what was attempted (capped), for diagnostics and tests

    def _new_run_id(self) -> str:
        return f"local-{self.ticket_id}-{self._clock().strftime('%Y%m%dT%H%M%S')}"[:160]

    @property
    def enabled(self) -> bool:
        return self.sink is not None and not self._muted and not self._discarded

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
    ) -> None:
        """Record a phase transition."""
        try:
            if harness:
                self.harness = harness
            self._publish(phase, status, message, cause)
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

    def _publish(self, phase: str, status: PhaseStatus, message: str, cause: str | None) -> None:
        event = LocalRunEvent(
            run_id=self.run_id,
            ticket_id=self.ticket_id,
            project_id=self.project_id,
            title=self.title,
            phase=phase,
            status=status,
            harness=self.harness,
            worker=self.worker,
            cause_code=(cause[:64] if cause else None),
            message=_clean(message),
            at=self._clock(),
        )
        if self._discarded:
            return
        if len(self.events) < _MAX_LOCAL_EVENTS:
            self.events.append(event)
        if self._held:
            self._buffer.append(event)
            return
        self._send(event)

    def _emit_warning(self, reason: str) -> None:
        """At most one warning per run; never raises and never touches the run result."""
        if self.warning is not None:
            return
        try:
            text = (
                f"[AVISO] Esteira ao vivo: progresso desta execucao nao sera publicado - {reason}. "
                f"A execucao continua normalmente. Veja {RUNBOOK}."
            )
            self.warning = sanitize_database_url(text)
            if self._warn is not None:
                self._warn(self.warning)
        except Exception:  # noqa: BLE001
            pass

    def _send(self, event: LocalRunEvent) -> None:
        if self.sink is None:
            if self._unconfigured_reason and not self._discarded:
                self._emit_warning(self._unconfigured_reason)
            return
        if not self.enabled:
            return
        sink = self.sink
        phase, status = event.phase, event.status
        try:
            _run_bounded(lambda: sink.write(event), self.timeout_s)
            self._failures = 0
        except Exception as exc:  # noqa: BLE001 - network, driver, lock, timeout: all contained
            self._failures += 1
            self._emit_warning(describe_publish_failure(exc))
            logger.warning(
                "local progress: publish failed for %s %s/%s (%s: %s)",
                self.run_id,
                phase,
                status,
                type(exc).__name__,
                sanitize_database_url(_clean(str(exc), 160)),
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
) -> ProgressPublisher:
    """A publisher for ``ticket_id``; joins the parent run when ``DARKFAC_LOCAL_RUN_ID`` is set.

    Never raises: any failure resolving the sink yields a publisher that simply does not publish.
    ``warn`` receives the single "cannot publish" warning of the run (USR-154). By default it goes to
    stderr, except under ``pytest`` or when a parent process already warned; pass ``None`` to silence it.
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
        return ProgressPublisher(
            ticket_id=ticket_id,
            project_id=project_id,
            title=title,
            run_id=inherited or None,
            sink=resolved,
            harness=harness,
            owns_run=not inherited,
            warn=warn,
            unconfigured_reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("local progress: disabled (%s)", type(exc).__name__)
        return ProgressPublisher(ticket_id=ticket_id or "unknown", project_id=project_id, title=title, sink=None)
