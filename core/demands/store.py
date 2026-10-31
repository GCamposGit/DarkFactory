"""Durable atomic store for User Demands."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from threading import Lock, RLock
from typing import Any

from core.demands.models import UserTicket
from core.demands.id_allocator import reserve_ticket_id
from core.roadmap.models import DeliveryStatus, utc_now

logger = logging.getLogger(__name__)

DEFAULT_DEMANDS_PATH = Path(".factory/demands/demands.json")


def _choose_delivery_row(local: UserTicket | None, queued: UserTicket | None) -> UserTicket | None:
    """File row wins when it is missing from the queue or already terminal.

    A non-terminal file row yields to the durable queue. Both missing is ``None``.
    """
    from core.demands import queue_store

    if local is not None and (queued is None or queue_store.is_terminal_ticket(local)):
        return local
    return queued


# One mutex per ledger file for the whole process: separate DemandsStore instances (threads, launchers)
# pointing at the same file must serialize their read-modify-write cycles (USR-321).
_PATH_LOCKS: dict[str, "_PathMutex"] = {}
_PATH_LOCKS_GUARD = Lock()
LOCK_TIMEOUT_SECONDS = float(os.environ.get("DARKFAC_DEMANDS_LOCK_TIMEOUT", "30"))


class DemandsLockTimeout(TimeoutError):
    """Raised when the cross-process ledger lock cannot be acquired in time."""


def _try_lock_file(fh: Any) -> bool:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock_file(fh: Any) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class _PathMutex:
    """Reentrant in-process lock plus an inter-process lockfile (``<ledger>.lock``), same API as RLock."""

    def __init__(self, path: Path, timeout: float | None = None) -> None:
        # Outside the repo: a lockfile next to the ledger would dirty every checkout (USR-321).
        digest = hashlib.sha256(str(path).lower().encode("utf-8")).hexdigest()[:16]
        self._lock_path = Path(tempfile.gettempdir()) / "darkfac-ledger-locks" / f"{digest}.lock"
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._timeout = LOCK_TIMEOUT_SECONDS if timeout is None else timeout
        self._rlock = RLock()
        self._depth = 0
        self._fh: Any = None

    def acquire(self) -> bool:
        if not self._rlock.acquire(timeout=self._timeout):
            raise DemandsLockTimeout(f"Timed out after {self._timeout}s waiting for in-process lock on {self._lock_path}")
        try:
            if self._depth == 0:
                self._lock_path.parent.mkdir(parents=True, exist_ok=True)
                fh = open(self._lock_path, "a+b")
                deadline = time.monotonic() + self._timeout
                while not _try_lock_file(fh):
                    if time.monotonic() >= deadline:
                        fh.close()
                        raise DemandsLockTimeout(
                            f"Timed out after {self._timeout}s waiting for ledger lock {self._lock_path} (held by another process)"
                        )
                    time.sleep(0.02)
                self._fh = fh
            self._depth += 1
            return True
        except BaseException:
            self._rlock.release()
            raise

    def release(self) -> None:
        self._depth -= 1
        try:
            if self._depth == 0 and self._fh is not None:
                fh, self._fh = self._fh, None
                try:
                    _unlock_file(fh)
                finally:
                    fh.close()
        finally:
            self._rlock.release()

    def __enter__(self) -> "_PathMutex":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def _lock_for(path: Path) -> _PathMutex:
    key = os.path.normcase(os.path.abspath(path))
    with _PATH_LOCKS_GUARD:
        if key not in _PATH_LOCKS:
            _PATH_LOCKS[key] = _PathMutex(Path(os.path.abspath(path)))
        return _PATH_LOCKS[key]


class DemandsStore:
    """Thread-safe, atomic persistence for user demands in JSON format."""

    def __init__(self, path: Path | str | None = None) -> None:
        override = os.environ.get("DARKFAC_DEMANDS_PATH")
        # USR-415: the implicit cwd-relative default never creates files on its own.
        self._implicit_default = path is None and not override
        if path is not None:
            self.path = Path(path)
        else:
            self.path = Path(override) if override else DEFAULT_DEMANDS_PATH
        self._lock = _lock_for(self.path)
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        if self._implicit_default or self._control_only():
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                self._write_raw([])

    def _control_only(self) -> bool:
        """USR-415: the shared checkout's tracked ledger is never rewritten; writes go to the control store."""
        if os.environ.get("DARKFAC_DEMANDS_PATH"):
            return False
        from core.demands import queue_store
        from core.paths import project_root

        if not queue_store.is_default_ledger(self.path):
            return False
        return (project_root() / ".git").is_dir()

    def _read_raw(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self.path.exists():
                return []
            try:
                content = self.path.read_text(encoding="utf-8").strip()
                if not content:
                    return []
                payload = json.loads(content)
                if isinstance(payload, list):
                    return payload
                if isinstance(payload, dict) and "demands" in payload:
                    return payload["demands"]
                return []
            except Exception as exc:
                logger.error(f"Failed to read demands from {self.path}: {exc}")
                return []

    def _write_raw(self, items: list[dict[str, Any]]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
            content = json.dumps(items, indent=2, ensure_ascii=False)
            tmp_path.write_text(content, encoding="utf-8")
            tmp_path.replace(self.path)

    def list_tickets(
        self,
        project_id: str | None = None,
        status: DeliveryStatus | None = None,
    ) -> list[UserTicket]:
        with self._lock:
            raw_items = self._read_raw()
            tickets: list[UserTicket] = []
            for raw in raw_items:
                try:
                    ticket = UserTicket.model_validate(raw)
                    if project_id and ticket.project_id != project_id:
                        continue
                    if status and ticket.status != status:
                        continue
                    tickets.append(ticket)
                except Exception as exc:
                    logger.warning(f"Skipping corrupted demand row in {self.path}: {exc}")
            tickets = self._with_control_queue(tickets, project_id, status)
            return sorted(tickets, key=lambda t: t.id)

    def _with_control_queue(
        self, tickets: list[UserTicket], project_id: str | None, status: DeliveryStatus | None
    ) -> list[UserTicket]:
        """USR-343: the control store is read first for the project ledger; demands.json is the fallback."""
        from core.demands import queue_store

        if os.environ.get("DARKFAC_DEMANDS_PATH") or not queue_store.is_default_ledger(self.path):
            return tickets
        merged = queue_store.merge_tickets(tickets, queue_store.list_queue_tickets())
        return [
            t for t in merged
            if (not project_id or t.project_id == project_id) and (not status or t.status == status)
        ]

    def get_ticket(self, ticket_id: str) -> UserTicket | None:
        with self._lock:
            tickets = self.list_tickets()
            for ticket in tickets:
                if ticket.id == ticket_id:
                    return ticket
            return None

    def materialize_ticket(self, ticket_id: str) -> UserTicket | None:
        """Copy the durable-queue ticket into this store under the same id, once."""
        return self.resolve_for_delivery(ticket_id)

    def canonical_ticket(self, ticket_id: str) -> UserTicket | None:
        """Read-only row delivery may close. Does not create a ledger or queue line.

        A terminal row in this file wins. A non-terminal row yields to the
        durable queue. ``None`` when both are missing.
        """
        with self._lock:
            local, queued = self._delivery_pair(ticket_id)
            return _choose_delivery_row(local, queued)

    def resolve_for_delivery(self, ticket_id: str) -> UserTicket | None:
        """Resolve ``ticket_id`` from the durable queue into this delivery store.

        The written row keeps ``ticket_id``; this never calls ``next_ticket_id``.
        A second call does not add a row. A local ``completed`` or ``cancelled``
        row is returned as parsed once and is not rewritten when the queue row
        is still open, so a missing timestamp cannot mint a second version and
        close the queue. The shared checkout's control-only ledger is not
        rewritten (USR-415). An id missing from both places yields ``None`` and
        writes nothing.
        """
        with self._lock:
            local, queued = self._delivery_pair(ticket_id)
            chosen = _choose_delivery_row(local, queued)
            if chosen is None or self._control_only():
                return chosen
            # ``chosen is local`` covers a terminal row and a ledger-only row.
            # One parse: a second read would fill created_at/updated_at again and
            # the mismatch would rewrite the file and mirror it onto the queue.
            if local is not None and (
                chosen is local or local.model_dump(mode="json") == chosen.model_dump(mode="json")
            ):
                return local
            logger.info("Materialized %s into %s from the durable queue", chosen.id, self.path)
            return self.save_ticket(chosen)

    def _delivery_pair(self, ticket_id: str) -> tuple[UserTicket | None, UserTicket | None]:
        """``(file row, queue row)``. The queue read does not create ``control.db``."""
        from core.demands import queue_store

        return self._file_ticket(ticket_id), queue_store.get_ticket(ticket_id)

    def _file_ticket(self, ticket_id: str) -> UserTicket | None:
        """The row stored in this ledger file, ignoring the control-store merge."""
        for raw in self._read_raw():
            if raw.get("id") != ticket_id:
                continue
            try:
                return UserTicket.model_validate(raw)
            except Exception as exc:
                logger.warning("Skipping corrupted demand row %s in %s: %s", ticket_id, self.path, exc)
                return None
        return None

    def save_ticket(self, ticket: UserTicket) -> UserTicket:
        if ticket.status == DeliveryStatus.COMPLETED and not (ticket.delivery_evidence or "").strip():
            raise ValueError(f"Completed ticket {ticket.id} requires delivery_evidence")
        if ticket.status == DeliveryStatus.COMPLETED and ticket.is_live_deploy:
            evidence = (ticket.delivery_evidence or "").strip()
            if not any(marker in evidence for marker in ("live_converged", "live_verified", "live_provisional")):
                raise ValueError(
                    f"Completed ticket {ticket.id} with live deployment requirements requires live convergence proof in delivery_evidence"
                )
        with self._lock:
            if self._control_only():
                from core.demands import queue_store

                queue_store.register_ticket(ticket)
                return ticket
            raw_items = self._read_raw()
            updated = False
            ticket_dump = json.loads(ticket.model_dump_json())
            for i, raw in enumerate(raw_items):
                if raw.get("id") == ticket.id:
                    raw_items[i] = ticket_dump
                    updated = True
                    break
            if not updated:
                raw_items.append(ticket_dump)
            self._write_raw(raw_items)
            self._mirror_to_control(ticket)
            return ticket

    def _inserts_missing_queue_rows(self) -> bool:
        """True when a save may insert a queue row this file alone knows about.

        The shared default ledger does (USR-343). A linked worktree — ``.git`` is
        a file, and its ``demands.json`` is the default ledger of that checkout —
        and any other ledger path only update a ``(project_id, ticket_id)`` that
        is already in the queue (USR-424).
        """
        from core.demands import queue_store
        from core.paths import project_root

        if not queue_store.is_default_ledger(self.path):
            return False
        return not (project_root() / ".git").is_file()

    def _mirror_to_control(self, ticket: UserTicket) -> None:
        """Keep an existing control-store row aligned with this ledger write.

        The default project ledger mirrors every save (USR-343). A worktree ledger
        still closes the same ``(project_id, ticket_id)`` when that row is already
        in the queue (USR-424) and does not create one for a ledger-only ticket.
        ``DARKFAC_DEMANDS_PATH`` stays file-only.
        """
        from core.demands import queue_store

        if os.environ.get("DARKFAC_DEMANDS_PATH"):
            return
        queued = queue_store.get_ticket(ticket.id)
        pair_present = queued is not None and queued.project_id == ticket.project_id
        if not pair_present and not self._inserts_missing_queue_rows():
            return
        try:
            queue_store.register_ticket(ticket)
        except Exception as exc:  # noqa: BLE001 - the ledger write already succeeded
            logger.warning("Control store not updated for %s: %s", ticket.id, exc)

    def next_ticket_id(self, project_id: str = "darkfac") -> str:
        with self._lock:
            from core.projects.registry import get_project_registry

            prefix = get_project_registry().get_ticket_prefix(project_id)
            return reserve_ticket_id(self.path, prefix)

    def update_status(
        self,
        ticket_id: str,
        status: DeliveryStatus,
        notes: str | None = None,
        delivery_evidence: str | None = None,
    ) -> UserTicket:
        with self._lock:
            ticket = self.get_ticket(ticket_id)
            if not ticket:
                raise KeyError(f"Ticket '{ticket_id}' not found")
            now = utc_now()
            updates: dict[str, Any] = {
                "status": status,
                "updated_at": now,
            }
            if delivery_evidence is not None:
                updates["delivery_evidence"] = delivery_evidence
            updated = ticket.model_copy(update=updates)
            self.save_ticket(updated)
            return updated

    def requeue_ticket(
        self, ticket_id: str, reason: str = "", *, reopen: bool = False
    ) -> tuple[UserTicket, bool]:
        """Single mutex-protected path to put a ticket back on the active queue (status ``planned``).

        Returns ``(ticket, changed)``. Terminal tickets (completed/cancelled) are never reopened here
        and an already planned ticket is left untouched (idempotent under concurrency).
        With ``reopen=True`` (USR-337) a completed ticket is explicitly reopened: ``reason`` is required, the
        delivery evidence is cleared and the reason is appended to ``problem_statement`` (history preserved).
        Cancelled tickets are never reopened.
        """
        with self._lock:
            ticket = self.get_ticket(ticket_id)
            if not ticket:
                raise KeyError(f"Ticket '{ticket_id}' not found")
            if reopen and ticket.status == DeliveryStatus.COMPLETED:
                if not reason.strip():
                    raise ValueError(f"Reopening {ticket_id} requires a reason")
                updated = ticket.model_copy(
                    update={
                        "status": DeliveryStatus.PLANNED,
                        "delivery_evidence": None,
                        "problem_statement": (ticket.problem_statement or "") + "\n\n" + reason,
                        "updated_at": utc_now(),
                    }
                )
                self.save_ticket(updated)
                logger.info("Reopened %s (completed -> planned) %s", ticket_id, reason)
                return updated, True
            if ticket.status in (DeliveryStatus.COMPLETED, DeliveryStatus.CANCELLED):
                raise ValueError(f"Ticket {ticket_id} is {ticket.status.value}; refusing to requeue")
            if ticket.status == DeliveryStatus.PLANNED:
                return ticket, False
            updated = ticket.model_copy(update={"status": DeliveryStatus.PLANNED, "updated_at": utc_now()})
            self.save_ticket(updated)
            logger.info("Requeued %s (%s -> planned) %s", ticket_id, ticket.status.value, reason)
            return updated, True
