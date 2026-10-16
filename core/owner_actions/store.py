"""Tolerant-read / atomic-write store for the owner action backlog (USR-190).

Layers
------
* **definitions** - ``.factory/owner_actions/owner_actions.json`` (git-tracked, curated by agents
  through ``scripts/owner_action.py`` and shipped inside the Hub image by ``COPY .factory``).
* **resolutions** (optional overlay) - a small JSON file the *Hub* writes when the owner clicks
  "marcar como feito" / answers a decision. In production it lives on the persistent Hub volume,
  because the definitions inside the container image are read-only in spirit and reset on every
  redeploy. The effective state of an item is its definition overlaid by its resolution.

Reading never raises: a missing file is an empty backlog, a corrupted file is an empty backlog plus
a warning, and an invalid row is skipped with a warning. Writing is atomic (tmp + ``os.replace``),
serialised by a lock file and refuses to overwrite a definitions file it cannot parse.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from pydantic import ValidationError

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
DEFAULT_OVERLAY_PATH = REPO_ROOT / "hub" / "data" / "owner_actions" / "resolutions.json"

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


@dataclass
class LoadResult:
    actions: list[OwnerAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


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
    """File-backed backlog. ``overlay_path`` is optional (the Hub sets it, agents usually do not)."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        overlay_path: Path | str | None = None,
        remote_ref: str | None = "origin/main",
        dynamic_path: Path | str | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else default_path()
        self.dynamic_path = Path(dynamic_path) if dynamic_path is not None else None
        self.overlay_path = Path(overlay_path) if overlay_path is not None else None
        # Best-effort id reservation against a branch's unsynced base (USR-255); None disables it.
        self.remote_ref = remote_ref

    # ------------------------------------------------------------------ read

    def _load_definitions(self) -> LoadResult:
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

    def _load_resolutions(self) -> tuple[dict[str, Resolution], list[str]]:
        if self.overlay_path is None:
            return {}, []
        payload, warning = _read_json(self.overlay_path)
        warnings = [warning] if warning else []
        resolutions: dict[str, Resolution] = {}
        raw_map = payload.get("resolutions") if isinstance(payload, dict) else None
        if isinstance(raw_map, dict):
            for action_id, raw in raw_map.items():
                try:
                    resolutions[str(action_id)] = Resolution.model_validate(raw)
                except ValidationError:
                    warnings.append(f"resolucao de {action_id} ignorada: registro invalido")
        return resolutions, warnings

    def load(self) -> LoadResult:
        """Effective backlog (definitions overlaid by resolutions). Never raises."""

        try:
            result = self._load_definitions()
            if self.dynamic_path is not None:
                dynamic = OwnerActionStore(self.dynamic_path, remote_ref=None)._load_definitions()
                curated_ids = {action.id for action in result.actions}
                result.actions.extend(action for action in dynamic.actions if action.id not in curated_ids)
                result.warnings.extend(dynamic.warnings)
            resolutions, warnings = self._load_resolutions()
            result.warnings.extend(warnings)
            result.actions = [apply_resolution(a, resolutions.get(a.id)) for a in result.actions]
            return result
        except Exception as exc:  # pragma: no cover - last-resort guard: the queue must keep working
            logger.warning("owner_actions: leitura falhou (%s)", exc)
            return LoadResult(warnings=[f"leitura do backlog falhou: {exc.__class__.__name__}"])

    def sync_human_request(self, request: Any, *, closed: bool) -> OwnerAction:
        """Apply a machine event only to dynamic HumanRequests; done is terminal."""
        from core.owner_actions.mirror import human_request_to_action

        if self.dynamic_path is None:
            raise OwnerActionInvalid("dynamic owner action storage is not configured")
        action = human_request_to_action(request)
        if any(item.id == action.id for item in self._load_definitions().actions):
            raise OwnerActionInvalid("machine event conflicts with curated owner action")
        dynamic = OwnerActionStore(self.dynamic_path, remote_ref=None)

        def apply(document: dict[str, Any], rows: list[Any]) -> OwnerAction:
            for index, raw in enumerate(rows):
                if isinstance(raw, dict) and raw.get("id") == action.id:
                    existing = OwnerAction.model_validate(raw)
                    if existing.status is ActionStatus.DONE or not closed:
                        return existing
                    done = OwnerAction.model_validate({**existing.to_record(), "status": "done"})
                    rows[index] = done.to_record()
                    return done
            saved = OwnerAction.model_validate({**action.to_record(), "status": "done"}) if closed else action
            rows.append(saved.to_record())
            return saved

        return dynamic._mutate_definitions(apply)

    def list_actions(self, *, include_done: bool = True) -> list[OwnerAction]:
        actions = self.load().actions
        return actions if include_done else [a for a in actions if a.status is not ActionStatus.DONE]

    def get(self, action_id: str) -> OwnerAction | None:
        return next((a for a in self.load().actions if a.id == action_id), None)

    def next_id(self) -> str:
        highest = 0
        for action in self._load_definitions().actions:
            match = SEQUENTIAL_ID_PATTERN.match(action.id)
            if match:
                highest = max(highest, int(match.group(1)))
        # Also reserve ids of rows that were skipped as invalid, so a repair never collides.
        payload, _ = _read_json(self.path)
        for raw in _raw_actions(payload):
            if isinstance(raw, dict):
                match = SEQUENTIAL_ID_PATTERN.match(str(raw.get("id", "")))
                if match:
                    highest = max(highest, int(match.group(1)))
        highest = max([highest, *self._remote_reserved_numbers()])
        return f"OA-{highest + 1:03d}"

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
        with _file_lock(self.path):
            payload, warning = _read_json(self.path)
            if warning and self.path.exists():
                raise OwnerActionStoreCorrupt(
                    f"{self.path.name} esta corrompido; corrija o JSON antes de gravar ({warning})"
                )
            if isinstance(payload, list):
                document: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "actions": payload}
            elif isinstance(payload, dict):
                document = payload
            else:
                document = {"schema_version": SCHEMA_VERSION, "actions": []}
            if not isinstance(document.get("actions"), list):
                document["actions"] = []
            result = fn(document, document["actions"])
            _atomic_write_json(self.path, document)
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
        highest = 0
        for raw in rows:
            if isinstance(raw, dict):
                match = SEQUENTIAL_ID_PATTERN.match(str(raw.get("id", "")))
                if match:
                    highest = max(highest, int(match.group(1)))
        reserved = self._remote_reserved_numbers() if remote_reserved is None else remote_reserved
        highest = max([highest, *reserved])
        return f"OA-{highest + 1:03d}"

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
        to_overlay: bool = False,
    ) -> OwnerAction:
        """Mark done (``action``) or record the answer (``decision``). Idempotent for a repeat."""

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
        if to_overlay:
            if self.overlay_path is None:
                raise OwnerActionInvalid("overlay de resolucoes nao configurado")
            return self._write_overlay(current, answer, note)
        return self._resolve_in_definitions(action_id, answer, note)

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

    def _write_overlay(self, current: OwnerAction, answer: DecisionAnswer | None, note: str) -> OwnerAction:
        assert self.overlay_path is not None
        resolution = Resolution(status=ActionStatus.DONE, resolved_at=utc_now(), answer=answer, note=note.strip())
        with _file_lock(self.overlay_path):
            payload, warning = _read_json(self.overlay_path)
            if warning and self.overlay_path.exists():
                backup = self.overlay_path.with_name(f"{self.overlay_path.name}.corrupt-{int(time.time())}")
                with contextlib.suppress(OSError):
                    os.replace(self.overlay_path, backup)
                payload = None
            document = payload if isinstance(payload, dict) else {}
            resolutions = document.get("resolutions") if isinstance(document.get("resolutions"), dict) else {}
            resolutions[current.id] = json.loads(resolution.model_dump_json(exclude_none=True))
            document = {"schema_version": SCHEMA_VERSION, "resolutions": resolutions}
            _atomic_write_json(self.overlay_path, document)
        return apply_resolution(current, resolution)


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
    "DEFAULT_OVERLAY_PATH",
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
