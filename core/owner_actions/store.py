"""Owner actions backed by the configured control store, or explicit local file mode."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from core.git.safe_show import safe_show
from core.owner_actions.models import (
    SEQUENTIAL_ID_PATTERN,
    ActionKind,
    ActionStatus,
    DecisionAnswer,
    OwnerAction,
    OwnerActionDraft,
    Resolution,
    iso_z,
    redact,
    utc_now,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
ENV_PATH = "DARKFAC_OWNER_ACTIONS_PATH"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO_ROOT / ".factory" / "owner_actions" / "owner_actions.json"

_LOCK_TIMEOUT_S = 10.0
_LOCK_STALE_S = 60.0
_THREAD_LOCK = threading.RLock()


class OwnerActionError(ValueError):
    """Base class for errors raised by write operations."""


class OwnerActionNotFound(OwnerActionError):
    pass


class OwnerActionInvalid(OwnerActionError):
    pass


class OwnerActionStoreCorrupt(OwnerActionError):
    pass


class _LedgerEnvelope(BaseModel):
    """Validated ledger envelope; extra metadata is retained for dynamic ledgers."""

    model_config = ConfigDict(extra="allow")

    schema_version: Literal[SCHEMA_VERSION]
    actions: list[Any]


@dataclass
class LoadResult:
    actions: list[OwnerAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    read_at: str = field(default_factory=lambda: iso_z(utc_now()))


def default_path() -> Path:
    override = os.environ.get(ENV_PATH, "").strip()
    return Path(override) if override else DEFAULT_PATH


@contextlib.contextmanager
def _file_lock(target: Path) -> Iterator[None]:
    """Cross-process mutex built on ``O_EXCL`` (works on Windows and Linux, no extra deps)."""

    lock_path = target.with_name(target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    fd: int | None = None
    with _THREAD_LOCK:
        while fd is None:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > _LOCK_STALE_S:
                        lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise OwnerActionError(f"timeout acquiring lock {lock_path.name}")
                time.sleep(0.05)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                lock_path.unlink()


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> tuple[Any, str | None]:
    """``(payload, warning)``; a missing file is ``(None, None)``."""

    if not path.exists():
        return None, None
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        return None, f"{path.name}: nao foi possivel ler ({exc.__class__.__name__})"
    if not text:
        return None, None
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"{path.name}: JSON corrompido ({exc.msg} na linha {exc.lineno}); backlog tratado como vazio"


_REMOTE_ID_TIMEOUT_S = 5.0


def _run_git_quiet(args: list[str], cwd: Path, timeout: float = _REMOTE_ID_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    """``git`` call that never raises: a missing repo or a hung process is just a miss (USR-255)."""
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args, 1, "", "")


def _raw_actions(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("actions"), list):
        return payload["actions"]
    return []


def _writable_ledger_document(payload: Any) -> dict[str, Any]:
    """Return a current ledger envelope, migrating the historical unversioned object shape.

    Early ledgers stored ``{"actions": [...]}`` before the envelope version was introduced.
    Preserve that supported shape on writes while still validating every explicit version and row.
    """

    if isinstance(payload, list):
        return {"schema_version": SCHEMA_VERSION, "actions": payload}
    if isinstance(payload, dict):
        document = dict(payload)
        document.setdefault("schema_version", SCHEMA_VERSION)
        return document
    return {"schema_version": SCHEMA_VERSION, "actions": []}


def _validate_candidate_document(document: dict[str, Any]) -> None:
    """Reject a write unless the complete candidate ledger conforms to its schema.

    Reads intentionally remain tolerant, but writes must never carry forward a malformed row
    (or malformed envelope) that consumers would silently skip on the next read.
    """

    try:
        envelope = _LedgerEnvelope.model_validate(document)
    except ValidationError as exc:
        raise OwnerActionInvalid(f"envelope de owner actions invalido: {_summarise(exc)}") from exc
    rows = envelope.actions
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        try:
            action = OwnerAction.model_validate(raw)
        except (ValidationError, TypeError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                detail = _summarise(exc)
            else:
                detail = redact(str(exc))
            raise OwnerActionInvalid(f"item #{index} invalido: {detail}") from exc
        if action.id in seen:
            raise OwnerActionInvalid(f"item {action.id} duplicado no documento")
        seen.add(action.id)


def apply_resolution(action: OwnerAction, resolution: Resolution | None) -> OwnerAction:
    """Overlay a resolution on a definition (a definition already ``done`` always wins)."""

    if resolution is None or action.status is ActionStatus.DONE:
        return action
    if resolution.status is not ActionStatus.DONE:
        return action
    answer = resolution.answer
    if action.kind is ActionKind.DECISION:
        valid = {option.id for option in action.options}
        if answer is None or answer.option_id not in valid:
            return action
    elif answer is not None:
        answer = None
    return action.model_copy(
        update={"status": ActionStatus.DONE, "resolved_at": resolution.resolved_at, "answer": answer}
    )


class OwnerActionStore:
    """One configured source for reads and writes; database errors propagate."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        remote_ref: str | None = "origin/main",
        control: bool | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else default_path()
        from core.owner_actions import control_queue

        mode = os.environ.get("DARKFAC_OWNER_ACTIONS_MODE", "").strip().lower()
        if mode not in ("", "file", "control"):
            raise OwnerActionInvalid("DARKFAC_OWNER_ACTIONS_MODE must be file or control")
        self.control = control if control is not None else (
            mode == "control" or (mode != "file" and control_queue.is_default_ledger(self.path) and control_queue.is_configured())
        )
        self.remote_ref = remote_ref
        self.migration_error: str | None = None

    # ------------------------------------------------------------------ read

    def _load_definitions(self) -> LoadResult:
        if self.control:
            from core.owner_actions import control_queue

            if self.migration_error:
                raise control_queue.ControlQueueUnavailable(self.migration_error)
            payload = {"actions": control_queue.list_control_rows()}
            warning = None
        else:
            payload, warning = _read_json(self.path)
        result = LoadResult()
        if warning:
            result.warnings.append(warning)
            logger.warning("owner_actions: %s", warning)
        seen: set[str] = set()
        for index, raw in enumerate(_raw_actions(payload)):
            try:
                action = OwnerAction.model_validate(raw)
            except ValidationError as exc:
                label = raw.get("id") if isinstance(raw, dict) else f"#{index}"
                first = exc.errors()[0]
                where = ".".join(str(part) for part in first.get("loc", ()))
                message = f"item {label} ignorado: {where or 'registro'}: {first.get('msg', 'invalido')}"
                result.warnings.append(redact(message))
                logger.warning("owner_actions: %s", message)
                continue
            if action.id in seen:
                result.warnings.append(f"item {action.id} duplicado ignorado")
                continue
            seen.add(action.id)
            result.actions.append(action)
        return result

    def load(self) -> LoadResult:
        """Read the selected source; never substitute a snapshot for a failed database."""
        return self._load_definitions()

    def migrate_legacy_overlays(self, directory: Path) -> None:
        """Import legacy volume data once; mark only after every row persisted.

        Existing completed control rows are terminal. A partial import is safe to retry.
        The export is consulted only to recover definitions of old resolution-only clicks.
        """
        if not self.control:
            return
        marker = directory / "control_migration_v1.json"
        if marker.exists():
            return
        dynamic, dynamic_warning = _read_json(directory / "dynamic.json")
        resolutions, resolution_warning = _read_json(directory / "resolutions.json")
        if dynamic_warning or resolution_warning:
            raise OwnerActionStoreCorrupt(dynamic_warning or resolution_warning)
        if dynamic is None and resolutions is None:
            return
        if dynamic is not None and (not isinstance(dynamic, dict) or not isinstance(dynamic.get("actions"), list)):
            raise OwnerActionInvalid("invalid legacy dynamic actions")
        if resolutions is not None and not isinstance(resolutions, dict):
            raise OwnerActionInvalid("invalid legacy resolutions")
        resolution_map = resolutions.get("resolutions", {}) if isinstance(resolutions, dict) else {}
        if not isinstance(resolution_map, dict):
            raise OwnerActionInvalid("invalid legacy resolutions")
        export, warning = _read_json(self.path)
        if warning:
            raise OwnerActionStoreCorrupt(warning)
        exported = {a.id: a for a in (OwnerAction.model_validate(r) for r in _raw_actions(export))}
        dynamic_actions = [OwnerAction.model_validate(r) for r in _raw_actions(dynamic)]

        def apply(document: dict[str, Any], rows: list[Any]) -> None:
            by_id = {r["id"]: OwnerAction.model_validate(r) for r in rows}
            for action in dynamic_actions:
                identity = dynamic.get("human_request_identities", {}).get(action.id) if isinstance(dynamic, dict) else None
                if identity:
                    action = action.model_copy(update={"human_request_identity": tuple(identity)})
                existing = by_id.get(action.id)
                if existing is None or (existing.status is not ActionStatus.DONE and action.status is ActionStatus.DONE):
                    by_id[action.id] = action
            for action_id, raw in resolution_map.items():
                action = by_id.get(action_id) or exported.get(action_id)
                if action is None:
                    raise OwnerActionInvalid(f"legacy resolution without definition: {action_id}")
                resolved = apply_resolution(action, Resolution.model_validate(raw))
                if resolved.status is not ActionStatus.DONE:
                    raise OwnerActionInvalid(f"invalid legacy resolution for {action_id}")
                by_id[action_id] = resolved
            rows[:] = [a.to_record() for a in by_id.values()]

        self._mutate_control(apply)
        _atomic_write_json(marker, {"migrated_at": iso_z(utc_now())})

    def sync_human_request(self, request: Any, *, closed: bool) -> OwnerAction:
        """Persist machine HumanRequests through the same mutation path; done is terminal."""
        from core.owner_actions.mirror import human_request_to_action

        action = human_request_to_action(request)

        def apply(document: dict[str, Any], rows: list[Any]) -> OwnerAction:
            for index, raw in enumerate(rows):
                if raw.get("id") == action.id:
                    existing = OwnerAction.model_validate(raw)
                    if (existing.human_request_identity is None
                            and existing.created_by == action.created_by and existing.title == action.title):
                        existing = existing.model_copy(update={"human_request_identity": action.human_request_identity})
                        rows[index] = existing.to_record()
                    if existing.human_request_identity != action.human_request_identity:
                        raise OwnerActionInvalid("human request identity conflict")
                    if existing.status is ActionStatus.DONE or not closed:
                        return existing
                    done = OwnerAction.model_validate({**existing.to_record(), "status": "done"})
                    rows[index] = done.to_record()
                    return done
            saved = OwnerAction.model_validate({**action.to_record(), "status": "done"}) if closed else action
            rows.append(saved.to_record())
            return saved

        return self._mutate_definitions(apply)

    def list_actions(self, *, include_done: bool = True) -> list[OwnerAction]:
        actions = self.load().actions
        return actions if include_done else [a for a in actions if a.status is not ActionStatus.DONE]

    def answered_decisions(self) -> list[OwnerAction]:
        """Decisions already answered in the configured store."""
        return [
            a for a in self.load().actions
            if a.kind is ActionKind.DECISION and a.status is ActionStatus.DONE and a.answer is not None
        ]

    def get(self, action_id: str) -> OwnerAction | None:
        return next((a for a in self.load().actions if a.id == action_id), None)

    def next_action_id(self) -> str:
        """Next free ``OA-NNN`` across every source of this store (USR-411)."""
        return f"OA-{max(self._used_numbers(), default=0) + 1:03d}"

    next_id = next_action_id

    def _used_numbers(self, rows: list[Any] | None = None, remote_reserved: set[int] | None = None) -> set[int]:
        """Numeric ids reserved in the selected store and git history.

        Non-numeric ids (``OA-HR-...``, ``OA-QR-...``) never match ``SEQUENTIAL_ID_PATTERN``.
        """
        ids = [a.id for a in self._load_definitions().actions]
        payload, _ = _read_json(self.path) if not self.control else (None, None)
        raw_rows = list(_raw_actions(payload)) + list(rows or [])
        ids += [str(r.get("id", "")) for r in raw_rows if isinstance(r, dict)]
        used = {int(m.group(1)) for m in map(SEQUENTIAL_ID_PATTERN.match, ids) if m}
        used |= self._git_history_used_numbers()
        return used | (self._remote_reserved_numbers() if remote_reserved is None else remote_reserved)

    def _git_history_used_numbers(self) -> set[int]:
        """Numeric ids previously recorded in git history (USR-434)."""
        directory = self.path.parent
        if not directory.is_dir():
            return set()
        result = _run_git_quiet(["log", "-U0", "-S", "id", "--", self.path.name], cwd=directory)
        if result.returncode != 0:
            return set()
        return {
            int(m.group(1))
            for m in re.finditer(r'["\']OA-(\d+)["\']', result.stdout)
        }

    def _remote_reserved_numbers(self) -> set[int]:
        """``OA-NNN`` numbers already used by ``self.remote_ref``'s copy of the file (USR-255).

        A branch that has not rebased yet only sees its own file, so two branches each adding the
        next free id collide once merged. Looking at the base branch too closes most of that gap;
        it is purely additive and never raises - a missing repo, remote or ref is just an empty set.
        """
        if not self.remote_ref:
            return set()
        directory = self.path.parent
        if not directory.is_dir():
            return set()
        # One local `git show <ref>:./<name>` relative to the file's directory: no rev-parse, no
        # fetch, no network, 5 s cap; outside a repo or without the ref it simply fails fast.
        result = safe_show(self.remote_ref, f"./{self.path.name}", cwd=directory, runner=_run_git_quiet)
        if result.returncode != 0:
            return set()
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return set()
        numbers: set[int] = set()
        for raw in _raw_actions(payload):
            if isinstance(raw, dict):
                match = SEQUENTIAL_ID_PATTERN.match(str(raw.get("id", "")))
                if match:
                    numbers.add(int(match.group(1)))
        return numbers

    # ----------------------------------------------------------------- write

    def _mutate_definitions(self, fn: Callable[[dict[str, Any], list[Any]], Any]) -> Any:
        if self.control:
            return self._mutate_control(fn)
        with _file_lock(self.path):
            payload, warning = _read_json(self.path)
            if warning and self.path.exists():
                raise OwnerActionStoreCorrupt(
                    f"{self.path.name} esta corrompido; corrija o JSON antes de gravar ({warning})"
                )
            document = _writable_ledger_document(payload)
            _validate_candidate_document(document)
            result = fn(document, document["actions"])
            _validate_candidate_document(document)
            _atomic_write_json(self.path, document)
            return result

    def _mutate_control(self, fn: Callable[[dict[str, Any], list[Any]], Any]) -> Any:
        """Run ``fn`` over control rows and persist only the changed rows."""
        import copy

        from core.owner_actions import control_queue

        if self.migration_error:
            raise control_queue.ControlQueueUnavailable(self.migration_error)
        with _THREAD_LOCK:
            control_rows = control_queue.list_control_rows()
            _validate_candidate_document({"schema_version": SCHEMA_VERSION, "actions": control_rows})
            rows = copy.deepcopy(control_rows)
            before = {str(r.get("id")): json.dumps(r, sort_keys=True) for r in rows if isinstance(r, dict)}
            document: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "actions": rows}
            _validate_candidate_document(document)
            result = fn(document, rows)
            _validate_candidate_document(document)
            changed = [
                r for r in rows
                if isinstance(r, dict) and before.get(str(r.get("id"))) != json.dumps(r, sort_keys=True)
            ]
            if changed:
                control_queue.write_rows(changed)
            return result

    def add(self, draft: OwnerActionDraft) -> OwnerAction:
        """Validate and append a new item (sequential ``OA-NNN`` unless the draft brings an id)."""

        # Resolved before the lock: a hung `git show` must not hold the file lock (USR-255).
        remote_reserved = self._remote_reserved_numbers()

        def _append(document: dict[str, Any], rows: list[Any]) -> OwnerAction:
            existing_ids = {str(r.get("id")) for r in rows if isinstance(r, dict)}
            action_id = draft.id or self._next_id_from_rows(rows, remote_reserved)
            if action_id in existing_ids:
                raise OwnerActionInvalid(f"{action_id} ja existe")
            try:
                action = draft.build(action_id)
            except ValidationError as exc:
                raise OwnerActionInvalid(_summarise(exc)) from exc
            rows.append(action.to_record())
            return action

        return self._mutate_definitions(_append)

    def _next_id_from_rows(self, rows: list[Any], remote_reserved: set[int] | None = None) -> str:
        return f"OA-{max(self._used_numbers(rows, remote_reserved), default=0) + 1:03d}"

    def upsert(self, action: OwnerAction) -> tuple[OwnerAction, bool]:
        """Idempotent insert for mirrored items: an existing id is returned untouched."""

        def _insert(document: dict[str, Any], rows: list[Any]) -> tuple[OwnerAction, bool]:
            for raw in rows:
                if isinstance(raw, dict) and raw.get("id") == action.id:
                    try:
                        return OwnerAction.model_validate(raw), False
                    except ValidationError:
                        return action, False
            rows.append(action.to_record())
            return action, True

        return self._mutate_definitions(_insert)

    def resolve(
        self,
        action_id: str,
        *,
        option_id: str | None = None,
        note: str = "",
        answered_by: str = "owner",
    ) -> OwnerAction:
        """Mark done (``action``) or record the answer (``decision``). Idempotent for a repeat."""

        self._validate_write_sources()
        current = self.get(action_id)
        if current is None:
            raise OwnerActionNotFound(f"{action_id} nao existe no backlog")
        if current.kind is ActionKind.DECISION and not option_id:
            raise OwnerActionInvalid(f"{action_id} e uma decisao: informe a opcao escolhida")
        if current.kind is ActionKind.ACTION and option_id:
            raise OwnerActionInvalid(f"{action_id} e uma acao, nao uma decisao")
        answer: DecisionAnswer | None = None
        if current.kind is ActionKind.DECISION:
            assert option_id is not None
            if option_id not in {option.id for option in current.options}:
                valid = ", ".join(option.id for option in current.options)
                raise OwnerActionInvalid(f"opcao {option_id!r} invalida para {action_id}; use uma de: {valid}")
            answer = DecisionAnswer(option_id=option_id, note=note.strip(), answered_by=answered_by)
        if current.status is ActionStatus.DONE:
            if answer is not None and current.answer and current.answer.option_id != answer.option_id:
                raise OwnerActionInvalid(f"{action_id} ja foi respondida com a opcao {current.answer.option_id}")
            return current
        return self._resolve_in_definitions(action_id, answer, note)

    def _validate_write_sources(self) -> None:
        """Fail closed before a mutation if any persisted definition row is invalid."""

        if self.control:
            from core.owner_actions import control_queue

            _validate_candidate_document({"schema_version": SCHEMA_VERSION, "actions": control_queue.list_control_rows()})
            return
        payload, warning = _read_json(self.path)
        if warning:
            raise OwnerActionStoreCorrupt(warning)
        _validate_candidate_document(_writable_ledger_document(payload))

    def reconcile_update(
        self,
        action_id: str,
        *,
        blocks: list[str] | None = None,
        close: bool = False,
        outcome: str,
        note: str,
    ) -> OwnerAction:
        """Apply a reconciliation outcome (USR-307): optionally new ``blocks``/close, always append history.

        Every other field is kept; the trail entry records what was decided and why.
        """

        def _apply(document: dict[str, Any], rows: list[Any]) -> OwnerAction:
            for raw in rows:
                if isinstance(raw, dict) and raw.get("id") == action_id:
                    try:
                        action = OwnerAction.model_validate(raw)
                    except ValidationError as exc:
                        raise OwnerActionInvalid(_summarise(exc)) from exc
                    entry = {"at": iso_z(utc_now()), "outcome": outcome, "note": redact(note)}
                    update: dict[str, Any] = {"history": [*action.history, entry]}
                    if blocks is not None:
                        update["blocks"] = blocks
                    if close and action.status is not ActionStatus.DONE:
                        update.update(status=ActionStatus.DONE, resolved_at=utc_now())
                    updated = OwnerAction.model_validate({**action.model_dump(), **update})
                    raw.clear()
                    raw.update(updated.to_record())
                    return updated
            raise OwnerActionNotFound(f"{action_id} nao existe no backlog")

        return self._mutate_definitions(_apply)

    def _resolve_in_definitions(self, action_id: str, answer: DecisionAnswer | None, note: str) -> OwnerAction:
        def _apply(document: dict[str, Any], rows: list[Any]) -> OwnerAction:
            for raw in rows:
                if isinstance(raw, dict) and raw.get("id") == action_id:
                    try:
                        action = OwnerAction.model_validate(raw)
                    except ValidationError as exc:
                        raise OwnerActionInvalid(_summarise(exc)) from exc
                    resolved = action.model_copy(
                        update={"status": ActionStatus.DONE, "resolved_at": utc_now(), "answer": answer}
                    )
                    raw.clear()
                    raw.update(resolved.to_record())
                    return resolved
            raise OwnerActionNotFound(f"{action_id} nao existe no backlog")

        return self._mutate_definitions(_apply)


def _summarise(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        where = ".".join(str(p) for p in error.get("loc", ()))
        parts.append(f"{where}: {error.get('msg', 'invalido')}" if where else str(error.get("msg", "invalido")))
    return redact("; ".join(parts))


def blocked_by(action: OwnerAction, by_id: dict[str, OwnerAction]) -> list[str]:
    """Unresolved dependencies of ``action`` (a dependency missing from the backlog counts as open)."""

    pending: list[str] = []
    for dep in action.depends_on:
        target = by_id.get(dep)
        if target is None or target.status is not ActionStatus.DONE:
            pending.append(dep)
    return pending


def unblocks(action: OwnerAction, actions: list[OwnerAction]) -> list[str]:
    """Other open items still waiting on ``action`` (none once ``action`` itself is done)."""

    if action.status is ActionStatus.DONE:
        return []
    return [a.id for a in actions if action.id in a.depends_on and a.status is not ActionStatus.DONE]


__all__ = [
    "DEFAULT_PATH",
    "ENV_PATH",
    "LoadResult",
    "OwnerActionError",
    "OwnerActionInvalid",
    "OwnerActionNotFound",
    "OwnerActionStore",
    "OwnerActionStoreCorrupt",
    "apply_resolution",
    "blocked_by",
    "default_path",
    "iso_z",
    "unblocks",
]
