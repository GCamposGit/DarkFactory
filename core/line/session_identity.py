"""Identity of a factory session, independent from coordination ownership.

Detecting Claude, Codex, Grok or Antigravity answers who is running this
process. It does not grant the coordinator role and it does not read
``DARKFAC_OPERATING_HARNESS``. That variable is a persisted routing preference
owned by ``core.line.operating_harness``; a value left in the user environment
must not disguise the harness that is actually executing this session.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping


logger = logging.getLogger("darkfac.line.session_identity")

# A harness session without a native id keeps one stable id per harness and host while it stays
# active. After this much idle time (the lease TTL) the next invocation is a new session.
FALLBACK_SESSION_IDLE_SECONDS = 900
_FALLBACK_REGISTRY = ("coordination", "session_ids.json")

_PROCESS_SESSION_ID = str(uuid.uuid4())
_PROCESS_STARTED_AT = datetime.now(timezone.utc)


class Harness(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    GROK = "grok"
    ANTIGRAVITY = "antigravity"
    UNKNOWN = "unknown"


_HARNESS_ALIASES = {
    "claude": Harness.CLAUDE,
    "claude-code": Harness.CLAUDE,  # census: wire-id (harness alias, not a model id)
    "claude_code": Harness.CLAUDE,
    "claudecode": Harness.CLAUDE,
    "codex": Harness.CODEX,
    "openai-codex": Harness.CODEX,
    "openai_codex": Harness.CODEX,
    "grok": Harness.GROK,
    "grok-build": Harness.GROK,  # census: wire-id (harness alias, not a model id)
    "grok_build": Harness.GROK,
    "antigravity": Harness.ANTIGRAVITY,
    "google-antigravity": Harness.ANTIGRAVITY,
    "google_antigravity": Harness.ANTIGRAVITY,
}


@dataclass(frozen=True)
class HarnessDetection:
    """Session signal found in this process. ``harness`` is None when none is reliable."""

    harness: str | None
    signal: str | None
    explicit_override: bool = False

    @property
    def detected(self) -> bool:
        return self.harness is not None


@dataclass(frozen=True)
class SessionIdentity:
    """Stable descriptive identity for one running session.

    No coordination field is present by design. Being detected as a harness
    does not grant authority over the factory.
    """

    session_id: str
    harness: str | None
    model: str | None
    host: str
    started_at: str

    @property
    def timestamp(self) -> str:
        return self.started_at

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


def normalize_harness_name(value: str | Harness | None) -> Harness:
    if isinstance(value, Harness):
        return value
    if value is None:
        return Harness.UNKNOWN
    return _HARNESS_ALIASES.get(value.strip().lower(), Harness.UNKNOWN)


def _present(env: Mapping[str, str], key: str) -> bool:
    value = env.get(key)
    return value is not None and value.strip() != ""


def _equals(env: Mapping[str, str], key: str, expected: str) -> bool:
    value = env.get(key)
    return value is not None and value.strip().lower() == expected


def detect_session_harness(
    env: Mapping[str, str] | None = None,
    *,
    override: str | Harness | None = None,
) -> HarnessDetection:
    """Recognize the harness executing this process.

    ``override`` applies only to this call. It is not written to the
    environment or to shared state. ``DARKFAC_OPERATING_HARNESS`` is ignored:
    a persisted value must not beat a live session signal, and without a
    reliable signal this function does not invent a harness from that value.

    Stronger session markers are checked before terminal or parent-IDE
    variables, because one harness may be launched from another.
    """

    current_env = os.environ if env is None else env
    if override is not None and str(override).strip():
        selected = normalize_harness_name(override)
        if selected is Harness.UNKNOWN:
            raise ValueError(f"unsupported session harness override: {override!r}")
        return HarnessDetection(
            harness=selected.value,
            signal="explicit-invocation-override",
            explicit_override=True,
        )

    checks: tuple[tuple[bool, Harness, str], ...] = (
        (_present(current_env, "CODEX_THREAD_ID"), Harness.CODEX, "CODEX_THREAD_ID"),
        (_present(current_env, "CODEX_SESSION_ID"), Harness.CODEX, "CODEX_SESSION_ID"),
        (_present(current_env, "GROK_SESSION_ID"), Harness.GROK, "GROK_SESSION_ID"),
        (
            _equals(current_env, "GROK_AGENT", "1") or _equals(current_env, "GROK_AGENT", "true"),
            Harness.GROK,
            "GROK_AGENT",
        ),
        (_present(current_env, "GROK_CLI"), Harness.GROK, "GROK_CLI"),
        (_present(current_env, "ANTIGRAVITY_SESSION_ID"), Harness.ANTIGRAVITY, "ANTIGRAVITY_SESSION_ID"),
        (
            _equals(current_env, "CLAUDECODE", "1") or _equals(current_env, "CLAUDECODE", "true"),
            Harness.CLAUDE,
            "CLAUDECODE",
        ),
        (_present(current_env, "CLAUDE_CODE_SESSION_ID"), Harness.CLAUDE, "CLAUDE_CODE_SESSION_ID"),
        (_equals(current_env, "TERM_PROGRAM", "antigravity"), Harness.ANTIGRAVITY, "TERM_PROGRAM"),
        (
            _equals(current_env, "ANTIGRAVITY_AGENT", "1")
            or _equals(current_env, "ANTIGRAVITY_AGENT", "true"),
            Harness.ANTIGRAVITY,
            "ANTIGRAVITY_AGENT",
        ),
        (_present(current_env, "ANTIGRAVITY_CONVERSATION_ID"), Harness.ANTIGRAVITY, "ANTIGRAVITY_CONVERSATION_ID"),
        (_present(current_env, "ANTIGRAVITY_SESSION"), Harness.ANTIGRAVITY, "ANTIGRAVITY_SESSION"),
        (_present(current_env, "CODEX_SANDBOX"), Harness.CODEX, "CODEX_SANDBOX"),
        (
            _present(current_env, "CODEX_SANDBOX_NETWORK_DISABLED"),
            Harness.CODEX,
            "CODEX_SANDBOX_NETWORK_DISABLED",
        ),
        (_present(current_env, "CLAUDE_CODE_ENTRYPOINT"), Harness.CLAUDE, "CLAUDE_CODE_ENTRYPOINT"),
    )
    for matched, harness, signal in checks:
        if matched:
            return HarnessDetection(harness=harness.value, signal=signal)
    return HarnessDetection(harness=None, signal=None)


def _first_nonempty(env: Mapping[str, str], *keys: str) -> str | None:
    for key in keys:
        value = env.get(key)
        if value is not None and value.strip():
            return value.strip()
    return None


def _fallback_registry_path() -> Path:
    from core.paths import state_root

    return state_root().joinpath(*_FALLBACK_REGISTRY)


def _stable_fallback_session(
    harness: str, host: str, now: datetime
) -> tuple[str, str] | None:
    """`(session_id, started_at)` kept across CLI processes for one harness on one host.

    Claude Code only exports CLAUDECODE/CLAUDE_CODE_ENTRYPOINT to its children, so each
    ``python scripts/line_briefing.py`` is a new process of the same session. The id is
    persisted under the state root and renewed on every use; it rolls over after
    ``FALLBACK_SESSION_IDLE_SECONDS`` without use. Returns None when the registry cannot be
    read or written (the caller then uses the per-process id).
    """

    path = _fallback_registry_path()
    key = f"{harness}@{host}"
    moment = now.timestamp()
    try:
        records = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(records, dict):
            records = {}
        record = records.get(key)
        fresh = (
            isinstance(record, dict)
            and isinstance(record.get("id"), str)
            and isinstance(record.get("last_seen"), (int, float))
            and 0 <= moment - float(record["last_seen"]) <= FALLBACK_SESSION_IDLE_SECONDS
        )
        if not fresh:
            record = {"id": f"{harness}-{uuid.uuid4()}", "created_at": moment}
        record["last_seen"] = moment
        records[key] = record
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        created = datetime.fromtimestamp(float(record.get("created_at", moment)), timezone.utc)
        return str(record["id"]), created.isoformat().replace("+00:00", "Z")
    except (OSError, ValueError) as exc:
        logger.warning("session id registry unavailable (%s); using the per-process id", exc)
        return None


def _session_id(
    env: Mapping[str, str], detection: HarnessDetection, host: str, now: datetime
) -> tuple[str, str | None]:
    explicit = _first_nonempty(env, "DARKFAC_SESSION_ID")
    if explicit:
        return explicit, None
    native_keys = {
        Harness.CODEX.value: ("CODEX_THREAD_ID", "CODEX_SESSION_ID"),
        Harness.CLAUDE.value: ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"),
        Harness.ANTIGRAVITY.value: ("ANTIGRAVITY_SESSION_ID",),
        Harness.GROK.value: ("GROK_SESSION_ID",),
    }
    keys = native_keys.get(detection.harness or "", ())
    native = _first_nonempty(env, *keys) if keys else None
    if native:
        return native, None
    if detection.harness:
        stable = _stable_fallback_session(detection.harness, host, now)
        if stable is not None:
            return stable
    return _PROCESS_SESSION_ID, None


def _model(env: Mapping[str, str], harness: str | None) -> str | None:
    explicit = _first_nonempty(env, "DARKFAC_SESSION_MODEL")
    if explicit:
        return explicit
    if harness == Harness.CODEX.value:
        return _first_nonempty(env, "CODEX_MODEL", "OPENAI_MODEL")
    if harness == Harness.CLAUDE.value:
        return _first_nonempty(env, "CLAUDE_MODEL", "ANTHROPIC_MODEL")
    if harness == Harness.ANTIGRAVITY.value:
        return _first_nonempty(env, "ANTIGRAVITY_MODEL", "GOOGLE_MODEL", "GEMINI_MODEL")
    if harness == Harness.GROK.value:
        return _first_nonempty(env, "GROK_MODEL")
    return None


def build_session_identity(
    env: Mapping[str, str] | None = None,
    *,
    harness_override: str | Harness | None = None,
    host: str | None = None,
    started_at: datetime | None = None,
) -> SessionIdentity:
    """Build identity for the current invocation without assigning a role."""

    current_env = os.environ if env is None else env
    detection = detect_session_harness(current_env, override=harness_override)
    effective_host = host or _first_nonempty(current_env, "DARKFAC_SESSION_HOST") or socket.gethostname()
    now = datetime.now(timezone.utc)
    session_id, stable_started = _session_id(current_env, detection, effective_host, now)
    effective_started_at = started_at or (
        datetime.fromisoformat(stable_started.replace("Z", "+00:00")) if stable_started else _PROCESS_STARTED_AT
    )
    if effective_started_at.tzinfo is None:
        effective_started_at = effective_started_at.replace(tzinfo=timezone.utc)
    effective_started_at = effective_started_at.astimezone(timezone.utc)
    return SessionIdentity(
        session_id=session_id,
        harness=detection.harness,
        model=_model(current_env, detection.harness),
        host=effective_host,
        started_at=effective_started_at.isoformat().replace("+00:00", "Z"),
    )


def current_session_identity(
    *,
    harness_override: str | Harness | None = None,
) -> SessionIdentity:
    return build_session_identity(harness_override=harness_override)
