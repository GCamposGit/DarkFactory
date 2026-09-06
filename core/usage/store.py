"""Transactional persistence for usage evidence and durable idempotency."""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class UsageStoreError(RuntimeError):
    """Base error for usage persistence failures."""


class UsageStoreCorruptionError(UsageStoreError):
    """Raised after malformed state has been quarantined."""


class AtomicUsageStore:
    """Serialize read-modify-write transactions across threads and processes."""

    schema_version = 2

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.storage_dir = self.path.parent
        self._thread_lock = threading.RLock()

    @classmethod
    def empty(cls) -> dict[str, Any]:
        return {
            "schema_version": cls.schema_version,
            "events": [],
            "aggregates": {},
            "invocations": {},
        }

    def _normalize(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("ledger root must be an object")
        events = payload.get("events", [])
        aggregates = payload.get("aggregates", {})
        invocations = payload.get("invocations")
        if not isinstance(events, list):
            raise ValueError("events must be an array")
        if not isinstance(aggregates, dict):
            raise ValueError("aggregates must be an object")
        if invocations is None:
            invocations = {
                row["invocation_id"]: row
                for row in events
                if isinstance(row, dict) and isinstance(row.get("invocation_id"), str)
            }
        if not isinstance(invocations, dict) or any(
            not isinstance(key, str) or not isinstance(value, dict)
            for key, value in invocations.items()
        ):
            raise ValueError("invocations must map string ids to evidence objects")
        return {
            "schema_version": self.schema_version,
            "events": events,
            "aggregates": aggregates,
            "invocations": invocations,
        }

    def _quarantine(self) -> Path:
        quarantine = self.path.with_name(
            f"{self.path.stem}.corrupt-{time.time_ns()}{self.path.suffix}"
        )
        try:
            os.replace(self.path, quarantine)
        except OSError as exc:
            raise UsageStoreError(
                f"usage ledger is corrupt and could not be quarantined: {exc}"
            ) from exc
        return quarantine

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return self.empty()
        try:
            raw = self.path.read_text(encoding="utf-8")
            return self._normalize(json.loads(raw))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            quarantine = self._quarantine()
            raise UsageStoreCorruptionError(
                f"usage ledger was quarantined at {quarantine}: {exc}"
            ) from exc
        except OSError as exc:
            raise UsageStoreError(f"unable to read usage ledger {self.path}: {exc}") from exc

    def _save_unlocked(self, payload: dict[str, Any]) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as exc:
            raise UsageStoreError(f"unable to persist usage ledger {self.path}: {exc}") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @contextmanager
    def _process_lock(self) -> Iterator[None]:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(".lock")
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        """Yield state under one cross-process transaction and commit atomically."""
        with self._thread_lock, self._process_lock():
            payload = self._load_unlocked()
            yield payload
            self._save_unlocked(payload)

    def read(self) -> dict[str, Any]:
        """Read and migrate state while holding the same transaction lock."""
        with self.transaction() as payload:
            return json.loads(json.dumps(payload))

