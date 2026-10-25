"""Unified execution registry (USR-208): one id per action and stage, visible in Hub Live.

Every actor of the factory -- ``run_ticket``, the line stage handlers, Telegram commands and (through the
``scripts/execution.py`` CLI, USR-209) interactive sessions and subagents -- registers an *execution*::

    execution_id = start(source, harness, stage, ticket_id=None, parent_execution_id=None)
    heartbeat(execution_id)            # every ~60 s while alive
    finish(execution_id, status, evidence)

It is a thin facade over the live store that already exists: rows go to the append-only
``local_run_events`` table (``core.line.local_progress``: Postgres in production through the restricted
writer role, SQLite in tests / locally) with ``phase = 'exec'`` and ``run_id = execution_id``. No new table,
privilege or HTTP endpoint is needed; ``core.workflow.line_live`` reads the rows back and
:func:`fold_executions` turns them into records.

Contract
--------
* Never raises into the caller: a missing sink, an unreachable database or an unwritable state directory
  only degrade the registry (the session is never blocked by the board).
* Offline resend queue: when a write fails the event is appended to ``<state root>/executions/outbox.jsonl``
  and resent (oldest first, original timestamp) on the next call that can reach the store.
* An execution whose last event (start/heartbeat) is older than :data:`LOST_AFTER_SECONDS` is ``lost`` and
  stops counting as in progress -- there is no eternal ``active``.
* Without a resolved sink (publishing off, or ``pytest`` with the default environment) every call is a no-op
  that still returns a unique id.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.line.local_progress import (
    LocalRunEvent,
    ProgressSink,
    _clean,
    _hostname,
    _run_bounded,
    resolve_sink,
)

logger = logging.getLogger("darkfac.execution_registry")

EXEC_PHASE = "exec"
ENV_EXECUTION_ID = "DARKFAC_EXECUTION_ID"  # parent execution inherited by subprocesses
HEARTBEAT_INTERVAL_SECONDS = 60.0
LOST_AFTER_SECONDS = 180  # > 3 min without heartbeat: lost
RECENT_WINDOW = timedelta(days=7)
STATE_DIRNAME = "executions"
OUTBOX_NAME = "outbox.jsonl"
OUTBOX_MAX_LINES = 1000
ORPHAN_SENDING_MAX_AGE_SECONDS = 600.0
WRITE_TIMEOUT_SECONDS = 3.0
_MESSAGE_BUDGET = 380

KIND_START, KIND_BEAT, KIND_END = "start", "hb", "end"
FINAL_STATUSES = ("succeeded", "failed", "cancelled")
ExecutionState = Literal["active", "lost", "succeeded", "failed", "cancelled"]

_SUCCESS_WORDS = frozenset({"succeeded", "success", "ok", "completed", "done", "passed", "merged"})
_CANCEL_WORDS = frozenset({"cancelled", "canceled", "aborted", "skipped"})
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")
_FIXTURE_TICKET = re.compile(r"(^|[-_])(TESTE|TEST|CANARY|CANARIO|FIXTURE|DEMO)([-_0-9]|$)", re.IGNORECASE)
_FIXTURE_SOURCES = frozenset({"canary", "canario", "test", "teste", "fixture"})
_FIXTURE_TITLE_WORDS = ("teste da esteira",)


# --------------------------------------------------------------------------- records


class ExecutionRecord(BaseModel):
    """One execution as the Hub shows it (folded from the events of ``execution_id``)."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str
    source: str
    stage: str
    harness: str | None = None
    model: str | None = None
    ticket_id: str | None = None
    parent_execution_id: str | None = None
    title: str = ""
    host: str | None = None
    state: ExecutionState
    started_at: str
    last_heartbeat_at: str
    finished_at: str | None = None
    evidence: str = ""
    fixture: bool = False


class ExecutionGroup(BaseModel):
    """Active executions of one ``source`` and ``stage``."""

    model_config = ConfigDict(extra="forbid")

    source: str
    stage: str
    executions: list[ExecutionRecord] = Field(default_factory=list)


def is_fixture(ticket_id: str | None, source: str | None = None, title: str | None = None) -> bool:
    """Test fixtures and canaries (``USR-TESTE``, ``Teste da esteira``...) hidden from /live by default."""
    if source and source.strip().lower() in _FIXTURE_SOURCES:
        return True
    if ticket_id and _FIXTURE_TICKET.search(ticket_id.strip()):
        return True
    lowered = (title or "").lower()
    return any(word in lowered for word in _FIXTURE_TITLE_WORDS)


def is_delivery(record: ExecutionRecord) -> bool:
    """A real delivery for "recently completed": succeeded, not a fixture, a whole unit of work.

    Whole tickets, delivery stages of the line (integration, deploy) and top-level sessions count; the phases
    nested under a ticket run and Telegram chatter do not.
    """
    if record.state != "succeeded" or record.fixture:
        return False
    if record.stage in ("ticket", "integration", "build_deploy"):
        return True
    return record.parent_execution_id is None and record.source != "telegram"


def normalize_status(status: str | None) -> str:
    """Map free-form outcomes (``ok``, ``error``...) to ``succeeded``/``failed``/``cancelled``."""
    word = str(status or "").strip().lower()
    if word in _SUCCESS_WORDS:
        return "succeeded"
    if word in _CANCEL_WORDS:
        return "cancelled"
    return "failed"


# --------------------------------------------------------------------------- folding (read side)


def _as_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _meta(event: Mapping[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(str(event.get("message") or ""))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def fold_executions(
    events: Iterable[Mapping[str, Any]],
    now: datetime,
    *,
    lost_after_seconds: int = LOST_AFTER_SECONDS,
    window: timedelta = RECENT_WINDOW,
) -> list[ExecutionRecord]:
    """Fold ``phase = 'exec'`` rows into one record per execution id (pure; newest activity first).

    ``now`` is injected so the TTL is deterministic: a non-final execution with no event for more than
    ``lost_after_seconds`` is ``lost``. Records whose last activity is older than ``window`` are dropped.
    """
    by_id: dict[str, list[tuple[datetime, Mapping[str, Any]]]] = {}
    for event in events:
        if event.get("phase") != EXEC_PHASE or not event.get("run_id"):
            continue
        at = _as_dt(event.get("at"))
        if at is not None:
            by_id.setdefault(str(event["run_id"]), []).append((at, event))

    cutoff = now - window
    records: list[ExecutionRecord] = []
    for execution_id, items in by_id.items():
        items.sort(key=lambda item: item[0])
        first_at, first = items[0]
        last_at, last = items[-1]
        meta = _meta(last)
        start_meta = next((m for m in (_meta(e) for _, e in items) if m.get("t0")), meta)
        started = _as_dt(start_meta.get("t0")) or first_at
        end_item = next(((a, e) for a, e in reversed(items) if e.get("cause_code") == KIND_END), None)
        finished: datetime | None = None
        evidence = ""
        if end_item is not None:
            finished = end_item[0]
            state: ExecutionState = normalize_status(str(end_item[1].get("status")))  # type: ignore[assignment]
            evidence = str(_meta(end_item[1]).get("e") or "")
        elif (now - last_at).total_seconds() > lost_after_seconds:
            state = "lost"
        else:
            state = "active"
        if last_at < cutoff:
            continue
        ticket = str(start_meta.get("k") or first.get("ticket_id") or "") or None
        if ticket == "-":
            ticket = None
        source = str(start_meta.get("s") or "unknown")
        title = str(first.get("title") or "")
        records.append(
            ExecutionRecord(
                execution_id=execution_id,
                source=source,
                stage=str(start_meta.get("g") or "unknown"),
                harness=(str(first.get("harness") or "") or None),
                model=(str(start_meta.get("m") or "") or None),
                ticket_id=ticket,
                parent_execution_id=(str(start_meta.get("p") or "") or None),
                title=title,
                host=(str(first.get("worker") or "") or None),
                state=state,
                started_at=started.isoformat(),
                last_heartbeat_at=last_at.isoformat(),
                finished_at=finished.isoformat() if finished else None,
                evidence=evidence,
                fixture=is_fixture(ticket, source, title),
            )
        )
    records.sort(key=lambda r: (r.last_heartbeat_at, r.execution_id), reverse=True)
    return records


def group_active(records: Sequence[ExecutionRecord]) -> list[ExecutionGroup]:
    """Active executions grouped by (source, stage), biggest groups last-seen first, stable order."""
    groups: dict[tuple[str, str], ExecutionGroup] = {}
    for record in records:
        if record.state != "active":
            continue
        key = (record.source, record.stage)
        groups.setdefault(key, ExecutionGroup(source=record.source, stage=record.stage)).executions.append(record)
    return [groups[key] for key in sorted(groups)]


# --------------------------------------------------------------------------- registry (write side)

_UNSET: Any = object()


def default_state_dir() -> Path | None:
    try:
        from core.paths import state_root

        return state_root() / STATE_DIRNAME
    except Exception:  # noqa: BLE001
        return None


def _ticket_cancelled(ticket_id: str) -> bool:
    if not ticket_id or ticket_id == "-":
        return False
    try:
        from core.line import local_cancel

        return local_cancel.cancel_requested(ticket_id)
    except Exception:  # noqa: BLE001
        return False


def new_execution_id(clock: Callable[[], datetime]) -> str:
    return f"exec-{clock().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:10]}"


class ExecutionRegistry:
    """Start/heartbeat/finish executions. Public methods never raise."""

    def __init__(
        self,
        *,
        sink: ProgressSink | None = _UNSET,
        state_dir: Path | None = _UNSET,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
        hostname: str | None = None,
        timeout_s: float = WRITE_TIMEOUT_SECONDS,
    ) -> None:
        self._environ = environ
        self._sink = sink
        self._state_dir_arg = state_dir
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._hostname = hostname
        self._timeout_s = timeout_s
        self._lock = threading.Lock()

    # ---- configuration -------------------------------------------------

    @property
    def sink(self) -> ProgressSink | None:
        if self._sink is _UNSET:
            try:
                self._sink = resolve_sink(self._environ)
            except Exception:  # noqa: BLE001
                self._sink = None
        return self._sink

    @property
    def state_dir(self) -> Path | None:
        if self._state_dir_arg is _UNSET:
            self._state_dir_arg = default_state_dir()
        return self._state_dir_arg

    @property
    def outbox_path(self) -> Path | None:
        directory = self.state_dir
        return None if directory is None else directory / OUTBOX_NAME

    # ---- public API ----------------------------------------------------

    def start(
        self,
        source: str,
        harness: str | None,
        stage: str,
        ticket_id: str | None = None,
        parent_execution_id: str | None = None,
        *,
        model: str | None = None,
        title: str = "",
        execution_id: str | None = None,
    ) -> str:
        """Register a new execution and return its unique id (always, even offline)."""
        eid = execution_id or new_execution_id(self._clock)
        try:
            now = self._clock()
            meta = {
                "s": str(source or "unknown")[:40],
                "g": str(stage or "unknown")[:40],
                "k": (ticket_id or "-")[:64],
                "p": (parent_execution_id or "")[:160],
                "m": (model or "")[:60],
                "t0": now.isoformat(),
            }
            ctx = {"title": _clean(title or f"{source}/{stage}", 240), "harness": harness, "meta": meta}
            self._save_context(eid, ctx)
            self._publish(eid, ctx, KIND_START, "running", now)
        except Exception as exc:  # noqa: BLE001 - the session must survive any registry problem
            logger.warning("execution_registry: start failed (%s)", type(exc).__name__)
        return eid

    def heartbeat(self, execution_id: str) -> bool:
        """Prove life. Returns False (and closes the execution as ``cancelled``) when its ticket's run was
        cancelled (USR-326), so a subagent that checks the result stops instead of beating forever."""
        try:
            ctx = self._load_context(execution_id)
            ticket = str((ctx.get("meta") or {}).get("k") or "-")
            if _ticket_cancelled(ticket):
                self.finish(execution_id, "cancelled", "run cancelled (USR-326)")
                return False
            self._publish(execution_id, ctx, KIND_BEAT, "running", self._clock())
        except Exception as exc:  # noqa: BLE001
            logger.warning("execution_registry: heartbeat failed (%s)", type(exc).__name__)
        return True

    def cancel_ticket(self, ticket_id: str) -> list[str]:
        """Finish every locally active execution of ``ticket_id`` as cancelled (parent and subagents)."""
        done: list[str] = []
        directory = self.state_dir
        if directory is None or not ticket_id:
            return done
        try:
            files = sorted((directory / "active").glob("*.json"))
        except OSError:
            return done
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if str((data.get("meta") or {}).get("k")) != ticket_id:
                    continue
            except (OSError, ValueError, AttributeError):
                continue
            self.finish(path.stem, "cancelled", "run cancelled (USR-326)")
            done.append(path.stem)
        return done

    def finish(self, execution_id: str, status: str = "succeeded", evidence: Any = None) -> None:
        try:
            ctx = self._load_context(execution_id)
            final = normalize_status(status)
            self._publish(execution_id, ctx, KIND_END, final, self._clock(), evidence=evidence)
            self._drop_context(execution_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("execution_registry: finish failed (%s)", type(exc).__name__)

    # ---- publishing ----------------------------------------------------

    def _publish(
        self,
        execution_id: str,
        ctx: dict[str, Any],
        kind: str,
        status: str,
        at: datetime,
        *,
        evidence: Any = None,
    ) -> None:
        if self.sink is None:
            return
        meta = dict(ctx.get("meta") or {})
        meta["x"] = kind
        text = self._evidence_text(evidence)
        event = LocalRunEvent(
            run_id=execution_id[:160],
            ticket_id=str(meta.get("k") or "-")[:64],
            project_id="darkfac",
            title=str(ctx.get("title") or "")[:240],
            phase=EXEC_PHASE,
            status=status,  # type: ignore[arg-type]
            harness=(str(ctx.get("harness") or "") or None),
            worker=self._hostname or _hostname(),
            cause_code=kind,
            message=self._encode(meta, text),
            at=at,
        )
        with self._lock:
            if self._flush_outbox():
                try:
                    self._write(event)
                    return
                except Exception as exc:  # noqa: BLE001 - offline: queue it
                    logger.info("execution_registry: store unreachable (%s); queued", type(exc).__name__)
            self._enqueue(event)

    @staticmethod
    def _evidence_text(evidence: Any) -> str:
        if evidence is None or evidence == "":
            return ""
        if isinstance(evidence, str):
            return evidence
        try:
            return json.dumps(evidence, ensure_ascii=False, default=str, separators=(",", ":"))
        except (TypeError, ValueError):
            return str(evidence)

    @staticmethod
    def _encode(meta: dict[str, Any], evidence: str) -> str:
        """Compact JSON that always fits the message column (the evidence is what gets trimmed)."""
        text = " ".join(evidence.split())
        try:
            from core.line.agent_cli import redact_secrets

            text = redact_secrets(text)
        except Exception:  # noqa: BLE001
            pass
        body = dict(meta)
        limit = 160
        while True:
            if text:
                body["e"] = text[:limit]
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) <= _MESSAGE_BUDGET or limit <= 0:
                return encoded[:_MESSAGE_BUDGET] if limit <= 0 else encoded
            limit -= 40

    def _write(self, event: LocalRunEvent) -> None:
        sink = self.sink
        if sink is None:
            return
        _run_bounded(lambda: sink.write(event), self._timeout_s)

    # ---- offline queue ---------------------------------------------------

    def _enqueue(self, event: LocalRunEvent) -> None:
        path = self.outbox_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(event.model_dump_json() + "\n")
            self._trim_outbox(path)
        except OSError as exc:
            logger.warning("execution_registry: could not queue event (%s)", type(exc).__name__)

    @staticmethod
    def _trim_outbox(path: Path) -> None:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= OUTBOX_MAX_LINES:
            return
        newest_beat: dict[str, int] = {}
        for index, line in enumerate(lines):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if data.get("cause_code") == KIND_BEAT:
                newest_beat[str(data.get("run_id"))] = index
        kept = [
            line
            for index, line in enumerate(lines)
            if index in newest_beat.values() or '"cause_code":"hb"' not in line.replace(" ", "")
        ]
        path.write_text("\n".join(kept[-OUTBOX_MAX_LINES:]) + "\n", encoding="utf-8")

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Best-effort liveness check; an uninspectable PID is assumed alive (fail safe)."""
        if pid <= 0:
            return False
        try:
            import psutil

            try:
                return bool(psutil.pid_exists(pid))
            except Exception:  # noqa: BLE001
                return True
        except ImportError:
            pass
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _adopt_orphans(self, path: Path) -> None:
        """Requeue ``outbox.jsonl.<pid>.<hex>.sending`` files left by dead or stale flushers."""
        try:
            candidates = sorted(path.parent.glob(f"{OUTBOX_NAME}.*.sending"))
        except OSError:
            return
        now = time.time()
        for orphan in candidates:
            parts = orphan.name.split(".")
            try:
                pid = int(parts[2])
            except (IndexError, ValueError):
                pid = -1
            try:
                stale = now - orphan.stat().st_mtime > ORPHAN_SENDING_MAX_AGE_SECONDS
            except OSError:
                continue
            if not stale and self._pid_alive(pid):
                continue
            mine = orphan.with_name(f"{OUTBOX_NAME}.{os.getpid()}.{uuid.uuid4().hex[:6]}.sending")
            try:
                orphan.replace(mine)  # atomic: the loser of a race gets OSError and moves on
            except OSError:
                continue
            try:
                text = mine.read_text(encoding="utf-8")
                if text.strip():
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write(text if text.endswith("\n") else text + "\n")
                    self._trim_outbox(path)
                mine.unlink(missing_ok=True)
            except OSError:
                pass

    def _flush_outbox(self) -> bool:
        """Resend queued events oldest first. ``False`` when the store is still unreachable."""
        path = self.outbox_path
        if path is None or self.sink is None:
            return True
        self._adopt_orphans(path)
        try:
            if not path.is_file():
                return True
            claimed = path.with_name(f"{OUTBOX_NAME}.{os.getpid()}.{uuid.uuid4().hex[:6]}.sending")
            path.replace(claimed)  # atomic: concurrent processes never resend the same lines
        except OSError:
            return True
        pending = claimed.read_text(encoding="utf-8").splitlines()
        sent = 0
        reachable = True
        for line in pending:
            if not line.strip():
                sent += 1
                continue
            try:
                event = LocalRunEvent.model_validate_json(line)
            except ValueError:
                sent += 1  # corrupt line: drop it
                continue
            try:
                self._write(event)
            except Exception:  # noqa: BLE001
                reachable = False
                break
            sent += 1
        rest = pending[sent:]
        if rest:
            try:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write("\n".join(rest) + "\n")
            except OSError:
                pass
        try:
            claimed.unlink(missing_ok=True)
        except OSError:
            pass
        return reachable

    # ---- per-execution context (so the CLI can heartbeat/finish by id alone) ----

    def _context_path(self, execution_id: str) -> Path | None:
        directory = self.state_dir
        if directory is None:
            return None
        return directory / "active" / f"{_SAFE_ID.sub('_', execution_id)[:160]}.json"

    def _save_context(self, execution_id: str, ctx: dict[str, Any]) -> None:
        path = self._context_path(execution_id)
        if path is None or self.sink is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(ctx, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _load_context(self, execution_id: str) -> dict[str, Any]:
        path = self._context_path(execution_id)
        try:
            if path is not None:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except (OSError, ValueError):
            pass
        return {"title": "", "harness": None, "meta": {"s": "unknown", "g": "unknown", "k": "-"}}

    def _drop_context(self, execution_id: str) -> None:
        path = self._context_path(execution_id)
        try:
            if path is not None:
                path.unlink(missing_ok=True)
        except OSError:
            pass


# --------------------------------------------------------------------------- module-level facade

_default: ExecutionRegistry | None = None


def get_registry() -> ExecutionRegistry:
    global _default
    if _default is None:
        _default = ExecutionRegistry()
    return _default


def set_registry(registry: ExecutionRegistry | None) -> None:
    """Replace the process-wide registry (tests, embedding)."""
    global _default
    _default = registry


def start(
    source: str,
    harness: str | None,
    stage: str,
    ticket_id: str | None = None,
    parent_execution_id: str | None = None,
    **kwargs: Any,
) -> str:
    return get_registry().start(source, harness, stage, ticket_id, parent_execution_id, **kwargs)


def heartbeat(execution_id: str) -> bool:
    return get_registry().heartbeat(execution_id)


def finish(execution_id: str, status: str = "succeeded", evidence: Any = None) -> None:
    get_registry().finish(execution_id, status, evidence)


class Heartbeat:
    """Daemon thread that heartbeats a set of executions every ``interval`` seconds."""

    def __init__(self, registry: ExecutionRegistry, ids: Callable[[], list[str]], interval: float) -> None:
        self._registry = registry
        self._ids = ids
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="execution-heartbeat", daemon=True)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                for execution_id in self._ids():
                    self._registry.heartbeat(execution_id)
            except Exception:  # noqa: BLE001
                pass

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


class TrackedExecution:
    """Context manager: ``start`` on enter, ``finish`` on exit (failed on exception), heartbeat meanwhile."""

    def __init__(
        self,
        source: str,
        harness: str | None,
        stage: str,
        ticket_id: str | None = None,
        parent_execution_id: str | None = None,
        *,
        model: str | None = None,
        title: str = "",
        registry: ExecutionRegistry | None = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._registry = registry or get_registry()
        self._args = (source, harness, stage, ticket_id, parent_execution_id)
        self._kwargs = {"model": model, "title": title}
        self._interval = heartbeat_interval
        self._beat: Heartbeat | None = None
        self.execution_id = ""
        self.status = "succeeded"
        self.evidence: Any = None

    def __enter__(self) -> TrackedExecution:
        self.execution_id = self._registry.start(*self._args, **self._kwargs)
        if self._registry.sink is not None and self._interval > 0:
            self._beat = Heartbeat(self._registry, lambda: [self.execution_id], self._interval)
            self._beat.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._beat is not None:
            self._beat.stop()
        if exc_type is not None:
            self.status, self.evidence = "failed", self.evidence or exc_type.__name__
        self._registry.finish(self.execution_id, self.status, self.evidence)


class ProgressExecutionTracker:
    """Mirrors the phases of a ``run_ticket`` run as executions (one per ticket and one per phase)."""

    def __init__(
        self,
        ticket_id: str,
        *,
        harness: str | None = None,
        model: str | None = None,
        title: str = "",
        source: str = "run_ticket",
        registry: ExecutionRegistry | None = None,
        parent_execution_id: str | None = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._registry = registry or get_registry()
        self.ticket_id, self.harness, self.model, self.title, self.source = ticket_id, harness, model, title, source
        self._lock = threading.Lock()
        self._closed = False
        self._stage: tuple[str, str] | None = None  # (phase, execution_id)
        inherited = parent_execution_id
        # A delivery subprocess joins the parent run: it only adds stage executions under the parent's id.
        self.root_id: str = inherited or self._registry.start(
            source, harness, "ticket", ticket_id, None, model=model, title=title
        )
        self._owns_root = inherited is None
        self._beat: Heartbeat | None = None
        if self._registry.sink is not None and heartbeat_interval > 0:
            self._beat = Heartbeat(self._registry, self._live_ids, heartbeat_interval)
            self._beat.start()

    def _live_ids(self) -> list[str]:
        with self._lock:
            ids = [self._stage[1]] if self._stage else []
        return ([self.root_id] if self._owns_root else []) + ids

    def observe(self, phase: str, status: str, message: str = "", cause: str | None = None, harness: str | None = None) -> None:
        """``ProgressPublisher`` observer: never raises."""
        try:
            if harness:
                self.harness = harness
            with self._lock:
                if self._closed:
                    return
                if phase == "run":
                    self._close_stage_locked("succeeded" if status == "succeeded" else status, message)
                    self._closed = True
                    done = True
                else:
                    done = False
                    if status == "running":
                        if self._stage and self._stage[0] != phase:
                            self._close_stage_locked("succeeded", "encerrada ao iniciar a proxima fase")
                        if not self._stage:
                            self._stage = (
                                phase,
                                self._registry.start(
                                    self.source, self.harness, phase, self.ticket_id, self.root_id,
                                    model=self.model, title=self.title,
                                ),
                            )
                    elif self._stage and self._stage[0] == phase:
                        self._close_stage_locked(status, message or cause or "")
            if done:
                if self._beat is not None:
                    self._beat.stop()
                if self._owns_root:
                    self._registry.finish(self.root_id, status, message or cause or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("execution_registry: observer failed (%s)", type(exc).__name__)

    def _close_stage_locked(self, status: str, evidence: str) -> None:
        if self._stage:
            self._registry.finish(self._stage[1], status, evidence)
            self._stage = None


def attach_progress(progress: Any, ticket_id: str, **kwargs: Any) -> ProgressExecutionTracker | None:
    """Register ``progress`` (a ``ProgressPublisher``) phases as executions; ``None`` when unavailable."""
    try:
        if "parent_execution_id" not in kwargs:
            kwargs["parent_execution_id"] = os.environ.get(ENV_EXECUTION_ID, "").strip() or None
        tracker = ProgressExecutionTracker(ticket_id, **kwargs)
        progress.add_observer(tracker.observe)
        return tracker
    except Exception as exc:  # noqa: BLE001
        logger.warning("execution_registry: could not attach (%s)", type(exc).__name__)
        return None
