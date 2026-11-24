"""Autonomous Telegram Gateway and Owner Pairing Engine (HF-14).

Governed by HYBRID_WORKFLOW_PLAN_2026-09-08 (Sections 5, 8, 9, 10, lines 266, 299, 300, 303, 305)
and HYBRID_AUTONOMY_REQUIREMENTS (Scenarios G1, G5, G8).

Key Invariants:
1. Strict Owner Authentication: only authorized user IDs and chat IDs can execute commands or approve releases.
2. Inbound Deduplication & Durable Offset Tracking (Scenario G5): duplicate updates/messages are ignored idempotently.
3. Durable Run Resumption (Scenarios G1 & G8):
   - Grill questions can be answered via /grill or inline callback, resuming WAITING_HUMAN jobs.
   - Release approvals (/approve or inline callback) record client acceptance receipts and authorize production deployment.
4. Non-blocking Resilience: Telegram API failures (outages, timeouts, 429s) NEVER crash or cancel active workflow runs.
   Transient failures are stored in a local outbox. Permanent HTTP errors are classified and not retried.
5. Inviolable Secret Redaction: tokens, API keys, passwords, and sensitive URLs are scrubbed from messages and logs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Set

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.paths import project_root, state_root

logger = logging.getLogger("darkfac.integrations.telegram")

# Secret redaction pattern
SECRET_PATTERNS = [
    re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}", re.IGNORECASE),
    re.compile(r"ghp_[A-Za-z0-9]{20,}", re.IGNORECASE),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}", re.IGNORECASE),
    re.compile(r"password=([^\s&]+)", re.IGNORECASE),
    re.compile(r"Bearer\s+([A-Za-z0-9._~+/-]{15,})", re.IGNORECASE),
]


def redact_secrets(text: str) -> str:
    """Scrub sensitive secrets, tokens, and credentials from text."""
    if not text:
        return text
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


# ==============================================================================
# Pydantic Schemas & Data Contracts
# ==============================================================================


class TelegramUser(BaseModel):
    """Telegram user descriptor."""

    model_config = ConfigDict(extra="ignore")

    id: int
    is_bot: bool = False
    first_name: str = ""
    last_name: Optional[str] = None
    username: Optional[str] = None


class TelegramChat(BaseModel):
    """Telegram chat descriptor."""

    model_config = ConfigDict(extra="ignore")

    id: int
    type: str = "private"  # private, group, supergroup, channel
    title: Optional[str] = None
    username: Optional[str] = None


class TelegramVoice(BaseModel):
    """Voice note audio metadata."""

    model_config = ConfigDict(extra="ignore")

    file_id: str
    file_unique_id: str
    duration: int = 0
    mime_type: Optional[str] = None
    file_size: Optional[int] = None


class TelegramAudio(BaseModel):
    """General audio file metadata."""

    model_config = ConfigDict(extra="ignore")

    file_id: str
    file_unique_id: str
    duration: int = 0
    mime_type: Optional[str] = None
    file_size: Optional[int] = None
    file_name: Optional[str] = None


class TelegramDocument(BaseModel):
    """Document/file metadata."""

    model_config = ConfigDict(extra="ignore")

    file_id: str
    file_unique_id: str
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None


class TelegramVideoNote(BaseModel):
    """Round video/audio note metadata."""

    model_config = ConfigDict(extra="ignore")

    file_id: str
    file_unique_id: str
    duration: int = 0
    length: int = 0
    file_size: Optional[int] = None


class TelegramMessage(BaseModel):
    """Incoming or outgoing Telegram message."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    message_id: int
    from_user: Optional[TelegramUser] = Field(default=None, alias="from")
    chat: TelegramChat
    date: int
    text: Optional[str] = None
    voice: Optional[TelegramVoice] = None
    audio: Optional[TelegramAudio] = None
    document: Optional[TelegramDocument] = None
    video_note: Optional[TelegramVideoNote] = None
    caption: Optional[str] = None
    reply_to_message: Optional[TelegramMessage] = None


class TelegramCallbackQuery(BaseModel):
    """Inline keyboard button callback query."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    from_user: TelegramUser = Field(alias="from")
    message: Optional[TelegramMessage] = None
    data: Optional[str] = None


class TelegramUpdate(BaseModel):
    """Incoming Telegram update object."""

    model_config = ConfigDict(extra="ignore")

    update_id: int
    message: Optional[TelegramMessage] = None
    callback_query: Optional[TelegramCallbackQuery] = None


class TelegramConfig(BaseModel):
    """Configuration for the Telegram Gateway."""

    model_config = ConfigDict(extra="forbid")

    bot_token: Optional[str] = None
    role: str = "all"  # "owner", "ops", or "all"
    authorized_user_ids: List[int] = Field(default_factory=list)
    authorized_chat_ids: List[int] = Field(default_factory=list)
    webhook_secret_token: Optional[str] = None
    api_base_url: str = "https://api.telegram.org"
    poll_timeout_seconds: int = 30


def _read_env_fallback(root: Optional[Path] = None) -> dict[str, str]:
    """Reads .env from repository root if present without external dependencies."""
    root_dir = root or project_root()
    env_file = root_dir / ".env"
    env_vars: dict[str, str] = {}
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            pass
    return env_vars


def _token_fp(tok: str | None) -> str:
    if not tok:
        return "none"
    clean = tok.strip()
    if len(clean) > 8:
        return f"{clean[:4]}...{clean[-4:]}"
    return "***"


# Closed set written to logs. Anything else collapses to ``permanent`` so a
# Telegram description, token, chat id or message body cannot reach the log.
ALLOWED_TELEGRAM_CATEGORIES = frozenset({
    "missing_destination",
    "invalid_entities",
    "chat_not_found",
    "bot_blocked",
    "permanent",
    "transient",
    "uncertain",
})
OUTBOX_PENDING = "pending"
OUTBOX_SENDING = "sending"
OUTBOX_DELIVERED = "delivered"
OUTBOX_UNCERTAIN = "uncertain"
OUTBOX_PERMANENT = "permanent"
_OUTBOX_STATUSES = frozenset({
    OUTBOX_PENDING,
    OUTBOX_SENDING,
    OUTBOX_DELIVERED,
    OUTBOX_UNCERTAIN,
    OUTBOX_PERMANENT,
})
_OUTBOX_CLAIM_TTL_SECONDS = 120.0
_OUTBOX_GUARDS: dict[str, threading.Lock] = {}
_OUTBOX_GUARDS_LOCK = threading.Lock()


def _outbox_guard(path: Path) -> threading.Lock:
    """Serialize threads in this process. The file lock covers other processes."""

    try:
        key = str(path.resolve())
    except OSError:
        key = str(path)
    with _OUTBOX_GUARDS_LOCK:
        guard = _OUTBOX_GUARDS.get(key)
        if guard is None:
            guard = threading.Lock()
            _OUTBOX_GUARDS[key] = guard
        return guard
_TELEGRAM_ROLES = frozenset({"owner", "ops", "all"})
_NOTIFICATION_ID_RE = re.compile(r"notif_[0-9a-f]{8,32}")
_CHAT_ID_RE = re.compile(r"-?\d+")
_ENTITY_MARKERS = (
    "parse entities",
    "end of the entity",
    "unsupported start tag",
    "unexpected end tag",
)
_CHAT_NOT_FOUND_MARKERS = (
    "chat not found",
    "user not found",
    "peer_id_invalid",
)
_BOT_BLOCKED_MARKERS = (
    "blocked by the user",
    "bot was blocked",
    "was kicked",
    "bot was kicked",
)


class _SendAttempt(NamedTuple):
    ok: bool
    http_status: int | None
    category: str
    message_id: int | None = None


class _DeliveryGate(NamedTuple):
    action: str
    claim: dict[str, Any] | None


def outbox_item_key(notification_id: str, role: str, chat_id: int) -> str:
    """Stable identity for one alert, one bot role and one destination."""

    safe_role = role if role in _TELEGRAM_ROLES else "unknown"
    return f"{notification_id}|{safe_role}|{int(chat_id)}"


def delivery_fingerprint(text: str) -> str:
    """Short hash of the redacted body. The text and any token stay out of the value."""

    redacted = redact_secrets(text or "")
    return hashlib.sha256(redacted.encode("utf-8")).hexdigest()[:16]


def _is_ambiguous_timeout(exc: BaseException) -> bool:
    """A read timeout may mean Telegram already accepted the message.

    ``URLError("timed out")`` keeps a string reason and stays transient: the
    transport failure is retried. ``TimeoutError`` (and ``socket.timeout``)
    means the socket was already in the request and the result is unknown.
    """

    if isinstance(exc, TimeoutError):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, (TimeoutError, socket.timeout))


def _message_id_from_body(raw: bytes | str) -> int | None:
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        payload = json.loads(raw)
    except (ValueError, TypeError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    message_id = result.get("message_id")
    if isinstance(message_id, int) and not isinstance(message_id, bool):
        return message_id
    return None


def _pid_alive(pid: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
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


def _claimed_at(item: dict[str, Any]) -> datetime | None:
    raw = item.get("claimed_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def claim_is_abandoned(item: dict[str, Any], now: datetime | None = None) -> bool:
    """A crashed claim returns to the queue. A live claim is left alone."""

    if item.get("status") != OUTBOX_SENDING:
        return False
    moment = now or datetime.now(UTC)
    claimed = _claimed_at(item)
    if claimed is None:
        return True
    if (moment - claimed).total_seconds() > _OUTBOX_CLAIM_TTL_SECONDS:
        return True
    pid = item.get("claim_pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return True
    return not _pid_alive(pid)


def parse_telegram_chat_ids(raw: str) -> list[int]:
    """Parse a comma-separated chat list.

    Group and supergroup ids are negative. ``str.isdigit`` rejects the sign, which
    used to drop the only configured destination. Zero is never a destination.
    """

    found: list[int] = []
    for part in raw.split(","):
        token = part.strip().strip("\"'").strip()
        if not token or _CHAT_ID_RE.fullmatch(token) is None:
            continue
        value = int(token)
        if value != 0:
            found.append(value)
    return found


def resolve_telegram_destinations(config: TelegramConfig) -> list[int]:
    """Prefer configured chats, then user ids. Never returns ``0``."""

    chats = [chat_id for chat_id in config.authorized_chat_ids if isinstance(chat_id, int) and chat_id != 0]
    if chats:
        return chats
    return [user_id for user_id in config.authorized_user_ids if isinstance(user_id, int) and user_id != 0]


def _telegram_description(body: str) -> str:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    description = payload.get("description")
    return description if isinstance(description, str) else ""


def classify_telegram_failure(status: int | None, body: str) -> str:
    """Map a Telegram HTTP result to one allowed category. The body is not returned."""

    description = _telegram_description(body).lower()
    if status == 400 and any(marker in description for marker in _CHAT_NOT_FOUND_MARKERS):
        return "chat_not_found"
    if status == 403 and any(marker in description for marker in _BOT_BLOCKED_MARKERS):
        return "bot_blocked"
    if status == 400 and any(marker in description for marker in _ENTITY_MARKERS):
        return "invalid_entities"
    if status is None or status in {408, 409, 429} or status >= 500:
        return "transient"
    if status >= 400:
        return "permanent"
    return "transient"


def log_telegram_delivery(
    *,
    notification_id: str,
    role: str,
    http_status: int | None,
    category: str,
) -> None:
    """Log only the id, role, HTTP status and category."""

    safe_id = notification_id if _NOTIFICATION_ID_RE.fullmatch(notification_id or "") else "notif_unknown"
    safe_role = role if role in _TELEGRAM_ROLES else "unknown"
    safe_category = category if category in ALLOWED_TELEGRAM_CATEGORIES else "permanent"
    if isinstance(http_status, int) and not isinstance(http_status, bool):
        status_text = str(http_status)
    else:
        status_text = "none"
    logger.warning(
        "telegram_delivery notification_id=%s role=%s http_status=%s category=%s",
        safe_id,
        safe_role,
        status_text,
        safe_category,
    )


def _new_notification_id() -> str:
    return f"notif_{uuid.uuid4().hex[:12]}"


def _retry_pause(seconds: float) -> None:
    time.sleep(seconds)


def _http_error_body(error: urllib.error.HTTPError) -> str:
    try:
        raw = error.read()
    except Exception:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw) if raw else ""


def _telegram_payload(
    chat_id: int,
    text: str,
    buttons: Optional[List[List[Dict[str, str]]]],
    parse_mode: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    return payload


def _post_send_message(url: str, payload: Dict[str, Any], timeout: float = 10.0) -> _SendAttempt:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            raw: bytes | str = b""
            reader = getattr(response, "read", None)
            if callable(reader):
                try:
                    raw = reader()
                except Exception:
                    raw = b""
            if status == 200:
                return _SendAttempt(True, 200, "transient", _message_id_from_body(raw))
            http_status = status if isinstance(status, int) else None
            return _SendAttempt(False, http_status, "permanent")
    except urllib.error.HTTPError as http_err:
        status = int(http_err.code)
        category = classify_telegram_failure(status, _http_error_body(http_err))
        return _SendAttempt(False, status, category)
    except Exception as exc:
        if _is_ambiguous_timeout(exc):
            return _SendAttempt(False, None, "uncertain")
        return _SendAttempt(False, None, "transient")


def load_telegram_config(
    config_file: Optional[Path] = None,
    role: str = "ops",
    config_dir: Optional[Path] = None,
) -> TelegramConfig:
    """Load environment credentials first; use local config only as fallback."""
    root_dir = config_dir.parent.parent if config_dir is not None else project_root()
    env_vars = _read_env_fallback(root_dir)
    keys = {
        "owner": ("TELEGRAM_OWNER_BOT_TOKEN",),
        "ops": ("TELEGRAM_OPS_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
    }.get(role, ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OPS_BOT_TOKEN", "TELEGRAM_OWNER_BOT_TOKEN"))

    sources: dict[str, str] = {}
    for key in keys:
        if os.environ.get(key):
            sources[f"env:{key}"] = os.environ[key]
        elif env_vars.get(key):
            sources[f".env:{key}"] = env_vars[key]

    # Whitespace/newlines around a pasted token make Telegram answer 404; the value is never meaningful.
    token = next((value.strip() for key in keys if (value := (os.environ.get(key) or env_vars.get(key) or "").strip())), None)

    cfg_dir = config_dir or (root_dir / ".factory" / "telegram")
    files = (
        [config_file] if config_file is not None else
        [cfg_dir / ("owner_config.json" if role == "owner" else "ops_config.json"), cfg_dir / "config.json"]
        if role in {"owner", "ops"} else [cfg_dir / "config.json"]
    )
    local_data: dict[str, Any] = {}
    local_token: str | None = None
    for candidate in files:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cand_tok = data.get("bot_token")
                if cand_tok:
                    sources[candidate.name] = str(cand_tok)
                    if not local_token:
                        local_token = str(cand_tok)
                for key, value in data.items():
                    if value is not None:
                        local_data.setdefault(key, value)
        except (OSError, ValueError):
            continue

    distinct_tokens = set(sources.values())
    if len(distinct_tokens) > 1:
        # Only the last 4 characters are logged; the first source listed is the one in use (USR-197).
        details = ", ".join(f"{src}=...{t.strip()[-4:]}" for src, t in sources.items())
        logger.warning(
            "Telegram token sources diverge for role=%s (precedencia: env > .env > JSON): %s. "
            "Rode python C:/dev/DarkFac/scripts/telegram_tokens_check.py para ver qual esta revogada.",
            role,
            details,
        )

    if not token:
        token = local_token

    users_raw = os.environ.get("TELEGRAM_AUTHORIZED_USERS") or os.environ.get("TELEGRAM_ALLOWED_USERS") or env_vars.get("TELEGRAM_AUTHORIZED_USERS", "")
    users = [int(u.strip()) for u in users_raw.split(",") if u.strip().isdigit()]
    if not users:
        users = local_data.get("authorized_user_ids", [])

    # The owner bot has a dedicated destination setting used by the Hub and
    # deployment configuration. Prefer it over shared authorized-chat lists so
    # owner alerts cannot accidentally target an ops/group chat.
    owner_chat_raw = os.environ.get("TELEGRAM_OWNER_CHAT_ID") or env_vars.get("TELEGRAM_OWNER_CHAT_ID", "")
    owner_chats = parse_telegram_chat_ids(owner_chat_raw) if role == "owner" else []
    chats_raw = os.environ.get("TELEGRAM_AUTHORIZED_CHATS") or os.environ.get("TELEGRAM_ALLOWED_CHATS") or env_vars.get("TELEGRAM_AUTHORIZED_CHATS", "")
    chats = owner_chats or parse_telegram_chat_ids(chats_raw)
    if not chats:
        chats = local_data.get("authorized_chat_ids", [])

    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET") or env_vars.get("TELEGRAM_WEBHOOK_SECRET")

    return TelegramConfig(
        bot_token=token,
        role=role,
        authorized_user_ids=users,
        authorized_chat_ids=chats,
        webhook_secret_token=secret or local_data.get("webhook_secret_token"),
        api_base_url=os.environ.get("TELEGRAM_API_BASE_URL", local_data.get("api_base_url", "https://api.telegram.org")),
        poll_timeout_seconds=local_data.get("poll_timeout_seconds", 30),
    )


def _tracked_telegram_execution(stage: str, update_id: int) -> Any:
    """Context manager registering one Telegram request as an execution (never raises, never blocks)."""
    from core.workflow.execution_registry import TrackedExecution

    return TrackedExecution("telegram", "gateway", stage, title=f"telegram {stage} update {update_id}", heartbeat_interval=0)


class TelegramActionType(str, Enum):
    START = "start"
    DEMAND = "demand"
    STATUS = "status"
    GRILL = "grill"
    APPROVE = "approve"
    ACCEPT = "accept"
    ALERTS = "alerts"
    TICKETS = "tickets"
    UNKNOWN = "unknown"
    UNAUTHORIZED = "unauthorized"


def _is_missing_git_error(exc: Exception) -> bool:
    detail = str(exc)
    return "git" in detail.lower() and (
        isinstance(exc, FileNotFoundError)
        or "[Errno 2]" in detail
        or "git não foi encontrado" in detail.lower()
    )


def _demand_failure_response(exc: Exception) -> str:
    detail = str(exc)
    if _is_missing_git_error(exc) and "git não foi encontrado" not in detail.lower():
        return "❌ Falha ao registrar demanda: Git não foi encontrado. Instale o Git ou configure o PATH do orquestrador."
    if "git não foi encontrado" in detail.lower():
        return f"❌ Falha ao registrar demanda: {detail}"
    return f"❌ Falha ao registrar demanda: {exc}"



class TelegramDispatchResult(BaseModel):
    """Result of processing an incoming update."""

    model_config = ConfigDict(extra="ignore")

    update_id: int
    action: TelegramActionType
    authorized: bool
    duplicate: bool = False
    target_id: Optional[str] = None
    response_text: str = ""
    # When set, each entry is one Telegram message (used by /tickets so a long
    # list is delivered in parts under the 4096-character cap).
    response_parts: List[str] = Field(default_factory=list)
    resumed: bool = False
    error: Optional[str] = None


def reply_messages(result: TelegramDispatchResult) -> list[str]:
    """Outbound texts for one dispatch. Parts replace ``response_text`` when present."""

    parts = [part for part in (result.response_parts or []) if part]
    if parts:
        return parts
    text = result.response_text or ""
    return [text] if text else []


class OutboxNotification(BaseModel):
    """One Telegram alert in the local outbox.

    ``key`` is ``notification_id|role|chat_id``. Repeated enqueues of that key
    update this row. ``pending`` can be replayed; ``delivered`` and ``uncertain``
    are not sent again. ``sending`` is a claim held by one replay worker.
    """

    model_config = ConfigDict(extra="ignore")

    notification_id: str
    chat_id: int
    role: str = "ops"
    text: str
    buttons: Optional[List[List[Dict[str, str]]]] = None
    created_at: str
    updated_at: Optional[str] = None
    delivered: bool = False
    failure_reason: Optional[str] = None
    status: str = OUTBOX_PENDING
    message_id: Optional[int] = None
    delivered_at: Optional[str] = None
    fingerprint: Optional[str] = None
    key: str = ""
    claim_token: Optional[str] = None
    claim_pid: Optional[int] = None
    claimed_at: Optional[str] = None
    parse_mode: str = "HTML"


def _role_of(role: str) -> str:
    return role if role in _TELEGRAM_ROLES else "ops"


def _outbox_row(
    *,
    notification_id: str,
    role: str,
    chat_id: int,
    text: str,
    buttons: Optional[List[List[Dict[str, str]]]],
    parse_mode: str,
    status: str,
    reason: Optional[str],
    created_at: Optional[str] = None,
) -> dict[str, Any]:
    safe_role = _role_of(role)
    now = datetime.now(UTC).isoformat()
    sending = status == OUTBOX_SENDING
    row = OutboxNotification(
        notification_id=notification_id,
        chat_id=chat_id,
        role=safe_role,
        text=text,
        buttons=buttons,
        created_at=created_at or now,
        updated_at=now,
        delivered=False,
        failure_reason=reason,
        status=status,
        key=outbox_item_key(notification_id, safe_role, chat_id),
        claim_token=uuid.uuid4().hex if sending else None,
        claim_pid=os.getpid() if sending else None,
        claimed_at=now if sending else None,
        parse_mode=parse_mode if parse_mode is not None else "HTML",
    )
    return row.model_dump()


def normalize_outbox_item(entry: dict[str, Any], default_role: str) -> dict[str, Any]:
    """Fill the stable key and a known status for one stored row."""

    role = entry.get("role") if entry.get("role") in _TELEGRAM_ROLES else _role_of(default_role)
    notification_id = str(entry.get("notification_id") or "") or "notif_unknown"
    try:
        chat_id = int(entry.get("chat_id") or 0)
    except (TypeError, ValueError):
        chat_id = 0
    status = entry.get("status")
    if status not in _OUTBOX_STATUSES:
        status = OUTBOX_DELIVERED if entry.get("delivered") else OUTBOX_PENDING
    message_id = entry.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, int):
        message_id = None
    claim_pid = entry.get("claim_pid")
    if isinstance(claim_pid, bool) or not isinstance(claim_pid, int):
        claim_pid = None
    buttons = entry.get("buttons") if isinstance(entry.get("buttons"), list) else None
    created = entry.get("created_at") if isinstance(entry.get("created_at"), str) else ""
    try:
        row = OutboxNotification(
            notification_id=notification_id,
            chat_id=chat_id,
            role=role,
            text=str(entry.get("text") or ""),
            buttons=buttons,
            created_at=created or datetime.now(UTC).isoformat(),
            updated_at=entry.get("updated_at") if isinstance(entry.get("updated_at"), str) else None,
            delivered=status == OUTBOX_DELIVERED,
            failure_reason=entry.get("failure_reason") if isinstance(entry.get("failure_reason"), str) else None,
            status=status,
            message_id=message_id,
            delivered_at=entry.get("delivered_at") if isinstance(entry.get("delivered_at"), str) else None,
            fingerprint=entry.get("fingerprint") if isinstance(entry.get("fingerprint"), str) else None,
            key=outbox_item_key(notification_id, role, chat_id),
            claim_token=entry.get("claim_token") if isinstance(entry.get("claim_token"), str) else None,
            claim_pid=claim_pid,
            claimed_at=entry.get("claimed_at") if isinstance(entry.get("claimed_at"), str) else None,
            parse_mode=entry.get("parse_mode") if isinstance(entry.get("parse_mode"), str) else "HTML",
        )
    except ValidationError:
        row = OutboxNotification(
            notification_id=notification_id,
            chat_id=chat_id,
            role=role,
            text=str(entry.get("text") or ""),
            buttons=None,
            created_at=created or datetime.now(UTC).isoformat(),
            delivered=status == OUTBOX_DELIVERED,
            failure_reason=entry.get("failure_reason") if isinstance(entry.get("failure_reason"), str) else None,
            status=status,
            message_id=message_id,
            fingerprint=entry.get("fingerprint") if isinstance(entry.get("fingerprint"), str) else None,
            key=outbox_item_key(notification_id, role, chat_id),
        )
    return row.model_dump()


# ==============================================================================
# Telegram Gateway Core
# ==============================================================================


class TelegramGateway:
    """Headless Telegram Gateway managing authentication, polling, callbacks, and deduplication."""

    def __init__(
        self,
        config: TelegramConfig,
        state_dir: Optional[Path] = None,
        state_file: Optional[Path] = None,
        intake_service: Optional[Any] = None,
        project_demand_handler: Optional[Callable[[str, str, int], Dict[str, Any]]] = None,
        demand_handler: Optional[Callable[[str, int], Dict[str, Any]]] = None,
        grill_handler: Optional[Callable[[str, str, int], Dict[str, Any]]] = None,
        approval_handler: Optional[Callable[[str, str, int], Dict[str, Any]]] = None,
        status_handler: Optional[Callable[[Optional[str]], Dict[str, Any]]] = None,
        line_grill_handler: Optional[Callable[[str, str, str, int], Dict[str, Any]]] = None,
        commercial_acceptance_handler: Optional[Callable[[str, int], Dict[str, Any]]] = None,
        audio_engine: Optional[Any] = None,
        line_handler: Optional[Callable[[str, int], Dict[str, Any]]] = None,
        cancel_handler: Optional[Callable[[str, int], Dict[str, Any]]] = None,
        grill_text_handler: Optional[Callable[[str, str, int, int], Dict[str, Any]]] = None,
    ) -> None:
        self.config = config
        self.state_dir = state_dir if state_dir is not None else state_root() / "telegram"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if state_file is not None:
            self.state_file = state_file
            self.outbox_file = self.state_dir / f"{state_file.stem}_outbox.json"
        elif self.config.role == "owner":
            self.state_file = self.state_dir / "gateway_state_owner.json"
            self.outbox_file = self.state_dir / "outbox_owner.json"
        elif self.config.role == "ops":
            self.state_file = self.state_dir / "gateway_state_ops.json"
            self.outbox_file = self.state_dir / "outbox_ops.json"
        else:
            self.state_file = self.state_dir / "gateway_state.json"
            self.outbox_file = self.state_dir / "outbox.json"

        self.intake_service = intake_service
        self.project_demand_handler = project_demand_handler
        self.demand_handler = demand_handler
        self.grill_handler = grill_handler
        # USR-353: free-text reply to a Grill message (target, text, update_id, user_id) -> durable record.
        self.grill_text_handler = grill_text_handler
        self.approval_handler = approval_handler
        self.status_handler = status_handler
        # HF-27-08 F: production-line grill answers (cb:grill:<run_id>#<qid>:<idx>,
        # disambiguated from the legacy cb:grill:<ticket_id>:<choice> by the
        # "#") and commercial-acceptance (cb:accept:<run_id>) callbacks.
        self.line_grill_handler = line_grill_handler
        self.commercial_acceptance_handler = commercial_acceptance_handler
        self.audio_engine = audio_engine
        # `/linha <ticket_id>`: push an existing demands.json ticket into the production line.
        self.line_handler = line_handler
        # `/cancelar <ticket_id|run_id>`: cancel an in-flight line run (USR-123).
        self.cancel_handler = cancel_handler

        self.last_offset: int = 0
        self.processed_update_ids: Set[int] = set()
        self.processed_callback_ids: Set[str] = set()
        self.processed_message_ids: Set[str] = set()
        self._load_state()

    def _load_state(self) -> None:
        """Load durable offset and deduplication state from disk."""
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                self.last_offset = int(data.get("last_offset", 0))
                self.processed_update_ids = set(data.get("processed_update_ids", []))
                self.processed_callback_ids = set(data.get("processed_callback_ids", []))
                self.processed_message_ids = set(data.get("processed_message_ids", []))
            except Exception as exc:
                logger.warning("Failed to load Telegram gateway state: %s", exc)

    def _save_state(self) -> None:
        """Persist state to disk safely with atomic replace."""
        data = {
            "last_offset": self.last_offset,
            "processed_update_ids": list(self.processed_update_ids)[-1000:],  # keep last 1000
            "processed_callback_ids": list(self.processed_callback_ids)[-1000:],
            "processed_message_ids": list(self.processed_message_ids)[-1000:],
            "updated_at": datetime.now(UTC).isoformat(),
        }
        temp_path = self.state_file.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp_path.replace(self.state_file)

    def is_authorized(self, user_id: Optional[int], chat_id: Optional[int]) -> bool:
        """Enforce strict authorization: sender must match authorized_user_ids and authorized_chat_ids."""
        if not self.config.authorized_user_ids and not self.config.authorized_chat_ids:
            return False  # Fail-closed if no authorized IDs are configured

        # If authorized_user_ids is configured, user_id must be in it
        if self.config.authorized_user_ids:
            if user_id is None or user_id not in self.config.authorized_user_ids:
                return False

        # If authorized_chat_ids is configured, chat_id must be in it when chat_id is present
        if self.config.authorized_chat_ids:
            if chat_id is not None and chat_id not in self.config.authorized_chat_ids:
                return False
            # If chat_id is not present (e.g. inline callback query without chat), user_id must have been authorized
            if chat_id is None and not self.config.authorized_user_ids:
                return False

        return True

    def process_update(self, update_data: Dict[str, Any]) -> TelegramDispatchResult:
        """Ingest, verify, deduplicate, and route a Telegram update payload."""
        try:
            update = TelegramUpdate.model_validate(update_data)
        except Exception as exc:
            logger.error("Failed to parse Telegram update: %s", exc)
            return TelegramDispatchResult(
                update_id=update_data.get("update_id", 0),
                action=TelegramActionType.UNKNOWN,
                authorized=False,
                error=f"Invalid update schema: {exc}",
            )

        # Determine authorization of sender upfront
        user_id = None
        chat_id = None
        if update.message:
            user_id = update.message.from_user.id if update.message.from_user else None
            chat_id = update.message.chat.id
        elif update.callback_query:
            user_id = update.callback_query.from_user.id
            if update.callback_query.message:
                chat_id = update.callback_query.message.chat.id

        is_auth = self.is_authorized(user_id, chat_id)

        # 1. Deduplication check on update_id
        if update.update_id in self.processed_update_ids:
            return TelegramDispatchResult(
                update_id=update.update_id,
                action=TelegramActionType.UNKNOWN,
                authorized=is_auth,
                duplicate=True,
                response_text="Duplicate update already processed.",
            )

        # 2. Deduplication check on message (chat_id, message_id)
        if update.message:
            msg_key = f"{update.message.chat.id}:{update.message.message_id}"
            if msg_key in self.processed_message_ids:
                return TelegramDispatchResult(
                    update_id=update.update_id,
                    action=TelegramActionType.UNKNOWN,
                    authorized=is_auth,
                    duplicate=True,
                    response_text="Duplicate message already processed.",
                )

        # 3. Strict authorization gate
        if not is_auth:
            logger.warning(
                "Unauthorized Telegram update attempt from user_id=%s, chat_id=%s",
                user_id,
                chat_id,
            )
            self.processed_update_ids.add(update.update_id)
            self._save_state()
            return TelegramDispatchResult(
                update_id=update.update_id,
                action=TelegramActionType.UNAUTHORIZED,
                authorized=False,
                response_text="Access Denied: You are not authorized to command Dark Factory.",
            )

        # 4. Dispatch update (every request/command is an execution visible in Hub Live, USR-208)
        with _tracked_telegram_execution("command" if update.message else "callback", update.update_id) as tracked:
            if update.message:
                result = self._handle_message(update)
            elif update.callback_query:
                result = self._handle_callback(update)
            else:
                result = TelegramDispatchResult(
                    update_id=update.update_id,
                    action=TelegramActionType.UNKNOWN,
                    authorized=True,
                    response_text="Unsupported update event type.",
                )
            tracked.status = "failed" if result.error else "succeeded"
            tracked.evidence = result.error or str(getattr(result.action, "value", result.action))

        # 5. Post-commit persistence: only advance offset and record IDs if transaction succeeded
        if result.error:
            # Transaction failed -> do NOT advance offset or record update as processed
            return result

        self.processed_update_ids.add(update.update_id)
        if update.message:
            self.processed_message_ids.add(f"{update.message.chat.id}:{update.message.message_id}")
        if update.update_id >= self.last_offset:
            self.last_offset = update.update_id + 1
        self._save_state()
        return result

    def download_telegram_file(self, file_id: str, dest_path: Path) -> bool:
        """Download audio/voice file from Telegram Bot API using getFile (USR-60)."""
        if not self.config.bot_token:
            return False
        try:
            get_file_url = f"{self.config.api_base_url}/bot{self.config.bot_token}/getFile?file_id={urllib.parse.quote(file_id)}"
            req = urllib.request.Request(get_file_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not data.get("ok"):
                logger.warning("Telegram getFile returned error: %s", data)
                return False
            file_path = data.get("result", {}).get("file_path")
            if not file_path:
                return False

            download_url = f"{self.config.api_base_url}/file/bot{self.config.bot_token}/{file_path}"
            dl_req = urllib.request.Request(download_url, headers={"User-Agent": "Mozilla/5.0"})
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(dl_req, timeout=30.0) as resp, open(dest_path, "wb") as out_file:
                while chunk := resp.read(65536):
                    out_file.write(chunk)
            return True
        except Exception as exc:
            logger.error("Failed downloading Telegram file %s: %s", file_id, exc)
            return False

    def _route_grill_text(
        self, update: TelegramUpdate, msg: Any, raw_text: str, user_id: Optional[int]
    ) -> Optional[TelegramDispatchResult]:
        """USR-353: a free-text reply to a Grill message is recorded durably on either bot.

        Returns ``None`` when the message is not a Grill reply (commands never are).
        """
        from core.owner_actions.grill_reply import find_grill_target, is_grill_context

        if not raw_text or raw_text.startswith("/"):
            return None
        reply_text = ""
        if msg.reply_to_message is not None:
            reply_text = msg.reply_to_message.text or msg.reply_to_message.caption or ""
        target: Optional[str] = None
        if reply_text and is_grill_context(reply_text):
            target = find_grill_target(reply_text)
        if target is None:
            explicit = re.match(r"^\s*grill\s+((?:USR|DF)-\d+)\b[\s:,-]*(.+)$", raw_text, re.IGNORECASE | re.DOTALL)
            if explicit:
                target = explicit.group(1).upper()
                raw_text = explicit.group(2).strip()
        if target is None:
            return None
        result = TelegramDispatchResult(
            update_id=update.update_id,
            action=TelegramActionType.GRILL,
            authorized=True,
            target_id=target,
        )
        if self.grill_text_handler is None:
            result.error = "grill_text_handler not configured"
            result.response_text = (
                f"❌ Resposta ao Grill de {target} NAO foi registrada: este canal nao tem o gravador configurado. "
                f"Responda no DarkHub ou com /grill {target} &lt;resposta&gt; no @darkfac_ops_bot."
            )
            return result
        try:
            res = self.grill_text_handler(target, raw_text, update.update_id, user_id or 0)
            result.resumed = bool(res.get("resumed", False))
            ref = res.get("action_id", "")
            result.response_text = (
                f"✅ Resposta registrada para o Grill de <b>{target}</b>"
                + (f" (<code>{ref}</code>)" if ref else "")
                + ". Visivel no DarkHub e sincronizada com o repositorio."
            )
        except Exception as exc:
            logger.error("Failed to record Telegram grill reply for %s: %s", target, exc)
            result.error = str(exc)
            result.response_text = f"❌ Falha ao registrar a resposta do Grill de {target}: {exc}"
        return result

    def _handle_message(self, update: TelegramUpdate) -> TelegramDispatchResult:
        msg = update.message
        assert msg is not None
        user_id = msg.from_user.id if msg.from_user else None
        chat_id = msg.chat.id
        raw_text = (msg.text or msg.caption or "").strip()

        grill_reply = self._route_grill_text(update, msg, raw_text, user_id)
        if grill_reply is not None:
            return grill_reply

        redirect = self._redirect_owner_command(update, raw_text)
        if redirect is not None:
            return redirect

        media_result, raw_text, is_voice_demand, voice_transcription = self._ingest_message_media(
            update, msg, user_id, raw_text
        )
        if media_result is not None:
            return media_result

        parts = raw_text.split(maxsplit=1)
        cmd_str = parts[0].lower() if parts else ""
        if "@" in cmd_str:
            cmd_str = cmd_str.split("@")[0]
        arg_str = parts[1] if len(parts) > 1 else ""

        result = TelegramDispatchResult(
            update_id=update.update_id,
            action=TelegramActionType.UNKNOWN,
            authorized=True,
        )

        if cmd_str in ("/start", "/help"):
            return self._handle_start_help(result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription)
        if cmd_str == "/demand":
            return self._handle_demand(result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription)
        if cmd_str == "/newproject":
            return self._handle_newproject(result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription)
        if cmd_str in ("/linha", "/line"):
            return self._handle_line(result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription)
        if cmd_str in ("/cancelar", "/cancel"):
            return self._handle_cancel(result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription)
        if cmd_str == "/tickets":
            return self._handle_tickets(result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription)
        if cmd_str == "/status":
            return self._handle_status(result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription)
        if cmd_str == "/grill":
            return self._handle_grill(result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription)
        if cmd_str == "/approve":
            return self._handle_approve(result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription)
        if cmd_str == "/alerts":
            return self._handle_alerts(result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription)
        if not cmd_str.startswith("/"):
            return self._handle_demand0(result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription)
        return self._handle_command_fallback(result, msg, user_id, chat_id, cmd_str, arg_str, raw_text, is_voice_demand, voice_transcription)

    def _redirect_owner_command(
        self, update: TelegramUpdate, raw_text: str
    ) -> TelegramDispatchResult | None:
        if self.config.role != "owner":
            return None
        # Owner bot (@darkfac_bot) = alerts only. Ops bot handles commands and decisions.
        redirect = TelegramDispatchResult(
            update_id=update.update_id, action=TelegramActionType.UNKNOWN, authorized=True
        )
        if raw_text.startswith("/"):
            redirect.response_text = "Comandos, demandas e decisões de grill ficam no @darkfac_ops_bot. Este canal (@darkfac_bot) envia apenas alertas."
        return redirect

    def _ingest_message_media(
        self, update: TelegramUpdate, msg: TelegramMessage, user_id: int | None, raw_text: str
    ) -> tuple[TelegramDispatchResult | None, str, bool, str]:
        # Handle voice and audio input (USR-60): voice, audio, video_note, and audio documents.
        voice_obj = msg.voice or msg.audio or msg.video_note
        if not voice_obj and msg.document:
            mime = (msg.document.mime_type or "").lower()
            fname = (msg.document.file_name or "").lower()
            if (
                mime.startswith("audio/")
                or mime.startswith("video/")
                or mime in ("application/ogg", "application/octet-stream")
                or any(fname.endswith(ext) for ext in (".ogg", ".oga", ".opus", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".mp4", ".weba"))
            ):
                voice_obj = msg.document

        if raw_text or not voice_obj:
            return None, raw_text, False, ""

        if msg.voice:
            tmp_ext = ".ogg"
        elif msg.video_note:
            tmp_ext = ".mp4"
        else:
            fname = getattr(voice_obj, "file_name", "") or "audio.wav"
            tmp_ext = Path(fname).suffix or ".wav"

        import uuid
        upload_dir = Path(os.environ.get("DARKFAC_AUDIO_TMP", Path(os.environ.get("TEMP", "/tmp")) / "darkfac_audio"))
        upload_dir.mkdir(parents=True, exist_ok=True)
        tmp_audio_path = upload_dir / f"tg_{uuid.uuid4().hex[:8]}{tmp_ext}"
        transcribed = ""
        try:
            downloaded = self.download_telegram_file(voice_obj.file_id, tmp_audio_path)
            if downloaded or tmp_audio_path.is_file():
                if self.audio_engine:
                    t_res = self.audio_engine.transcribe(tmp_audio_path)
                    transcribed = t_res.text.strip()
                else:
                    from core.audio.engine import AudioTranscriptionEngine
                    engine = AudioTranscriptionEngine()
                    t_res = engine.transcribe(tmp_audio_path)
                    transcribed = t_res.text.strip()
        except Exception as exc:
            logger.error("Failed transcribing Telegram voice message: %s", exc)
        finally:
            tmp_audio_path.unlink(missing_ok=True)

        if not transcribed:
            failed = TelegramDispatchResult(
                update_id=update.update_id,
                action=TelegramActionType.UNKNOWN,
                authorized=True,
                response_text="⚠️ Não foi possível transcrever a mensagem de voz. Verifique a qualidade do áudio ou envie como texto.",
            )
            return failed, raw_text, False, ""

        reply_text = msg.reply_to_message.text if (msg.reply_to_message and msg.reply_to_message.text) else ""
        match_ticket = re.search(r"\b((?:USR|DF)-\d+|[a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+)\b", reply_text + " " + transcribed, re.IGNORECASE)
        is_grill_context = ("grill" in reply_text.lower() or "intervenção prioritária" in reply_text.lower() or "desambiguação" in reply_text.lower() or "grill" in transcribed.lower())
        if is_grill_context and match_ticket:
            return self._handle_voice_grill_reply(update, match_ticket.group(1).upper(), transcribed, user_id), raw_text, False, ""

        # Standalone voice note -> Ingest as /demand.
        return None, f"/demand {transcribed}", True, transcribed

    def _handle_voice_grill_reply(
        self, update: TelegramUpdate, target_ticket_id: str, transcribed: str, user_id: int | None
    ) -> TelegramDispatchResult:
        result = TelegramDispatchResult(
            update_id=update.update_id,
            action=TelegramActionType.GRILL,
            authorized=True,
            target_id=target_ticket_id,
        )
        if self.grill_handler:
            try:
                res = self.grill_handler(target_ticket_id, transcribed, user_id or 0)
                result.resumed = res.get("resumed", True)
                result.response_text = (
                    f"✅ Resposta de voz registrada para o Grill de <b>{target_ticket_id}</b>:\n"
                    f"<i>\"{transcribed}\"</i>\n"
                    "Avanço autônomo retomado."
                )
            except Exception as exc:
                result.error = str(exc)
                result.response_text = f"❌ Falha ao aplicar resposta do Grill: {exc}"
        else:
            result.resumed = True
            result.response_text = f"✅ Resposta de voz registrada para {target_ticket_id}."
        return result

    def _handle_start_help(self, result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription) -> TelegramDispatchResult:
        result.action = TelegramActionType.START
        if self.config.role == "owner":
            result.response_text = (
                "👑 <b>Dark Factory Owner Governance Bot (@darkfac_bot)</b>\n\n"
                "Canal oficial para governança estratégica, decisões humanas e aprovações do Owner.\n\n"
                "Comandos disponíveis:\n"
                "• <code>/grill [ticket_id] [resposta]</code> - Responder alinhamento e continuar o desenvolvimento\n"
                "• <code>/approve [project_id] [digest]</code> - Aprovar publicação de versão em produção\n"
                "• <code>/alerts</code> - Consultar avisos do sistema\n"
                "• <code>/status [ticket_id]</code> - Consultar andamento das tarefas e etapas\n"
                "• <code>/demand [texto ou áudio]</code> - Registrar demanda prioritária do Owner\n"
                "• <code>/tickets</code> - Listar tarefas abertas\n"
                "• <code>/linha [ticket_id]</code> - Enviar tarefa para desenvolvimento na linha autônoma\n"
                "• <code>/cancelar [ticket_id|run_id]</code> - Cancelar tarefa da linha autônoma\n\n"
                "ℹ️ <i>Demandas operacionais e backlog geral são gerenciadas no @darkfac_ops_bot</i>"
            )
        elif self.config.role == "ops":
            result.response_text = (
                "⚙️ <b>Dark Factory Autonomous Orchestrator Bot (@darkfac_ops_bot)</b>\n\n"
                "Canal oficial de operações autônomas, fila de demandas e andamento da fábrica.\n\n"
                "Comandos disponíveis:\n"
                "• <code>/demand [texto da demanda]</code> - Ingerir nova demanda no backlog autônomo\n"
                "• <code>/tickets</code> - Listar ID e descrição dos tickets abertos\n"
                "• <code>/linha [ticket_id]</code> - Enviar um ticket existente para a linha autônoma\n"
                "• <code>/cancelar [ticket_id|run_id]</code> - Cancelar run da linha autônoma\n"
                "• <code>/status [ticket_id]</code> - Consultar status de pipelines e jobs\n"
                "• <code>/alerts</code> - Consultar telemetria operacional\n\n"
                "• <code>/grill [ticket_id] [resposta]</code> - Responder alinhamento (Grill)\n"
                "• <code>/approve [projeto] [digest]</code> - Aprovar release\n\n"
                "ℹ️ <i>Alertas críticos chegam pelo @darkfac_bot (somente alertas)</i>"
            )
        else:
            result.response_text = (
                "👋 <b>Dark Factory Autonomous Control Bot</b>\n\n"
                "Available commands:\n"
                "• <code>/demand [text]</code> - Ingest a new demand into backlog\n"
                "• <code>/tickets</code> - List open tickets (ID and description)\n"
                "• <code>/linha [ticket_id]</code> - Send existing ticket to autonomous line\n"
                "• <code>/cancelar [ticket_id|run_id]</code> - Cancel an in-flight autonomous line run\n"
                "• <code>/status [ticket_id]</code> - Check status of runs and pipelines\n"
                "• <code>/alerts</code> - Check active token quota and operational alerts\n"
                "• <code>/grill [ticket_id] [choice]</code> - Answer Grill clarification questions\n"
                "• <code>/approve [project_id] [digest]</code> - Approve production release\n"
            )
        return result

    def _handle_demand(self, result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription) -> TelegramDispatchResult:
        result.action = TelegramActionType.DEMAND
        route = None
        if arg_str:
            from core.intake.project_routing import resolve_demand

            route = resolve_demand(
                arg_str, self.state_dir,
                external_id=f"tg-msg-{chat_id}-{msg.message_id}", requested_by=user_id,
            )
        if not arg_str:
            result.response_text = (
                "⚠️ Usage: /demand [<project_id>:] <description of feature or bugfix> "
                "(sem projeto = darkfac; /newproject <nome>: <texto> para produto novo)"
            )
        elif route.action != "accept":
            result.target_id = route.onboarding_request_id
            result.response_text = route.message
        else:
            demand_project = route.project_id
            arg_str = route.text
            active_intake = self.intake_service
            if active_intake is None and demand_project != "darkfac" and self.project_demand_handler is not None:
                # USR-340: other projects register a ticket with their own prefix (PH-..), never USR-.
                try:
                    res = self.project_demand_handler(demand_project, arg_str, user_id or 0)
                    ticket_id = res.get("ticket_id", "")
                    result.target_id = ticket_id
                    prefix = "👑 [Owner Demand] " if self.config.role == "owner" else ""
                    result.response_text = (
                        f"✅ {prefix}Demanda registrada no projeto {demand_project}: <b>{ticket_id}</b>"
                    )
                except Exception as exc:
                    logger.error("Telegram project demand error: %s", exc)
                    result.error = str(exc)
                    result.response_text = (
                        f"❌ Intake indisponivel para o projeto {demand_project}: nada foi registrado ({exc})."
                    )
            elif demand_project != "darkfac" and active_intake is None:
                result.response_text = (
                    f"Este canal nao encaminha demandas ao projeto {demand_project}: nada foi registrado."
                )
            elif active_intake is not None:
                try:
                    from core.workflow.control_contracts import IntakeCommand
                    ext_id = f"tg-msg-{chat_id}-{msg.message_id}"
                    cmd = IntakeCommand(
                        project_id=demand_project,
                        channel="telegram",
                        external_id=ext_id,
                        payload={
                            "title": arg_str[:80],
                            "problem": arg_str,
                            "journey": [f"Telegram user {user_id} in chat {chat_id}"],
                            "non_goals": [],
                            "criteria": ["Autonomous intake verification"],
                        },
                        mode="autonomous",
                        policy_ref="telegram-policy-v1",
                    )
                    receipt = active_intake.accept(cmd, datetime.now(UTC))
                    demand_id = receipt.demand_id
                    result.target_id = demand_id
                    prefix = "👑 [Owner Demand] " if self.config.role == "owner" else ""
                    result.response_text = f"✅ {prefix}Demand registered successfully: <b>{demand_id}</b>"
                except Exception as exc:
                    logger.error("Telegram intake_service error: %s", exc)
                    result.error = str(exc)
                    result.response_text = _demand_failure_response(exc)
            elif self.demand_handler:
                try:
                    res = self.demand_handler(arg_str, user_id or 0)
                    ticket_id = res.get("ticket_id", "TICKET-AUTO")
                    title = res.get("title", arg_str[:60])
                    status_val = res.get("status", "planned")
                    result.target_id = ticket_id
                    prefix = "👑 [Owner Demand] " if self.config.role == "owner" else ""
                    if is_voice_demand and voice_transcription:
                        result.response_text = (
                            f"✅ {prefix}Demanda por Áudio Registrada com Sucesso!\n\n"
                            f"📋 <b>Ticket:</b> <code>{ticket_id}</code>\n"
                            f"📌 <b>Título:</b> {title}\n"
                            f"📊 <b>Status:</b> {status_val}\n\n"
                            f"🎙️ <b>Transcrição Original do Áudio:</b>\n"
                            f"<i>\"{voice_transcription}\"</i>\n\n"
                            f"🚀 <i>Demanda adicionada ao backlog da Dark Factory.</i>"
                        )
                    else:
                        result.response_text = (
                            f"✅ {prefix}Demanda registrada com sucesso!\n\n"
                            f"📋 <b>Ticket:</b> <code>{ticket_id}</code>\n"
                            f"📌 <b>Título:</b> {title}\n"
                            f"📊 <b>Status:</b> {status_val}\n\n"
                            f"<i>Demanda inserida no backlog da Dark Factory.</i>"
                        )
                    line_message = res.get("line_message")
                    if line_message:
                        result.response_text += f"\n\nLinha autonoma: {line_message}"
                except Exception as exc:
                    logger.error("Demand handler error: %s", exc)
                    result.error = str(exc)
                    result.response_text = _demand_failure_response(exc)
            else:
                try:
                    from core.demands.models import DemandInput
                    from core.demands.service import DemandsService

                    demands_svc = DemandsService()
                    ticket = demands_svc.create_ticket_from_input(
                        DemandInput(
                            title=arg_str[:70],
                            problem_statement=arg_str,
                            project_id="darkfac",
                        ),
                        force_heuristic=True,
                    )
                    result.target_id = ticket.id
                    prefix = "👑 [Owner Demand] " if self.config.role == "owner" else ""
                    if is_voice_demand and voice_transcription:
                        result.response_text = (
                            f"✅ {prefix}Demanda por Áudio Registrada com Sucesso!\n\n"
                            f"📋 <b>Ticket:</b> <code>{ticket.id}</code>\n"
                            f"📌 <b>Título:</b> {ticket.title}\n"
                            f"📊 <b>Status:</b> {ticket.status.value}\n\n"
                            f"🎙️ <b>Transcrição Original do Áudio:</b>\n"
                            f"<i>\"{voice_transcription}\"</i>\n\n"
                            f"🚀 <i>Demanda adicionada ao backlog da Dark Factory.</i>"
                        )
                    else:
                        result.response_text = (
                            f"✅ {prefix}Demanda registrada com sucesso!\n\n"
                            f"📋 <b>Ticket:</b> <code>{ticket.id}</code>\n"
                            f"📌 <b>Título:</b> {ticket.title}\n"
                            f"📊 <b>Status:</b> {ticket.status.value}\n\n"
                            f"<i>Demanda inserida no backlog da Dark Factory.</i>"
                        )
                except Exception as exc:
                    logger.error("Durable demand creation error: %s", exc)
                    if _is_missing_git_error(exc):
                        result.error = str(exc)
                        result.response_text = _demand_failure_response(exc)
                    else:
                        result.target_id = "DEMAND-RECORDED"
                        result.response_text = f"✅ Demanda recebida e enfileirada: {arg_str[:120]}"
        return result

    def _handle_newproject(self, result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription) -> TelegramDispatchResult:
        from core.intake.project_routing import resolve_newproject

        result.action = TelegramActionType.DEMAND
        route = resolve_newproject(
            arg_str, self.state_dir,
            external_id=f"tg-msg-{chat_id}-{msg.message_id}", requested_by=user_id,
        )
        result.target_id = route.onboarding_request_id
        result.response_text = route.message
        return result

    def _handle_line(self, result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription) -> TelegramDispatchResult:
        result.action = TelegramActionType.DEMAND
        target = arg_str.strip().split()[0].upper() if arg_str.strip() else ""
        if not target:
            result.response_text = "Uso: /linha <ticket_id>  (ex.: /linha USR-62)"
        elif self.line_handler is None:
            result.target_id = target
            result.response_text = "Envio para a linha autonoma indisponivel neste canal."
        else:
            result.target_id = target
            try:
                res = self.line_handler(target, user_id or 0)
                result.response_text = str(res.get("message") or f"Ticket {target} processado.")
                if not res.get("ok", True):
                    result.error = result.response_text
            except Exception as exc:
                logger.error("Line handler error: %s", exc)
                result.error = str(exc)
                result.response_text = f"Falha ao enviar {target} para a linha: {exc}"
        return result

    def _handle_cancel(self, result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription) -> TelegramDispatchResult:
        result.action = TelegramActionType.DEMAND
        target = arg_str.strip().split()[0] if arg_str.strip() else ""
        if not target:
            result.response_text = "Uso: /cancelar <ticket_id|run_id>  (ex.: /cancelar USR-62 ou /cancelar run-xxx)"
        elif self.cancel_handler is None:
            result.target_id = target
            result.response_text = "Cancelamento de run indisponivel neste canal."
        else:
            result.target_id = target
            try:
                res = self.cancel_handler(target, user_id or 0)
                result.response_text = str(res.get("message") or f"Alvo {target} processado.")
                if not res.get("ok", True):
                    result.error = result.response_text
            except Exception as exc:
                logger.error("Cancel handler error: %s", exc)
                result.error = str(exc)
                result.response_text = f"Falha ao cancelar {target}: {exc}"
        return result

    def _handle_tickets(self, result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription) -> TelegramDispatchResult:
        result.action = TelegramActionType.TICKETS
        try:
            from core.integrations import telegram_tickets as tickets_mod

            rows = tickets_mod.load_open_tickets()
            parts = tickets_mod.format_open_tickets_messages(rows)
            result.response_parts = parts
            result.response_text = parts[0]
        except Exception as exc:
            logger.error("Telegram /tickets failed: %s", exc)
            result.error = str(exc)
            result.response_text = "❌ Não foi possível listar os tickets abertos."
        return result

    def _handle_status(self, result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription) -> TelegramDispatchResult:
        result.action = TelegramActionType.STATUS
        ticket_id_query = arg_str.strip() or None
        if self.status_handler:
            try:
                res = self.status_handler(ticket_id_query)
                summary = res.get("summary") or "Resumo de status indisponivel."
                result.response_text = f"📊 DarkFac Status:\n{summary}"
            except Exception as exc:
                result.error = str(exc)
                result.response_text = f"❌ Failed to fetch status: {exc}"
        else:
            result.response_text = (
                "📊 DarkFac Status:\nResumo real indisponivel neste bot (sem status_handler). "
                "Use o bot de operacoes ou o DarkHub (/live)."
            )
        return result

    def _handle_grill(self, result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription) -> TelegramDispatchResult:
        result.action = TelegramActionType.GRILL
        subparts = arg_str.split(maxsplit=1)
        if len(subparts) < 2:
            result.response_text = "⚠️ Como usar: /grill <id_do_ticket> <sua resposta ou escolha>"
        else:
            t_id, answer = subparts[0].strip(), subparts[1].strip()
            result.target_id = t_id
            if self.grill_handler:
                try:
                    res = self.grill_handler(t_id, answer, user_id or 0)
                    result.resumed = res.get("resumed", True)
                    result.response_text = f"✅ Resposta registrada para <b>{t_id}</b>. Desenvolvimento retomado."
                except Exception as exc:
                    result.error = str(exc)
                    result.response_text = f"❌ Não foi possível registrar a resposta: {exc}"
            else:
                result.resumed = True
                result.response_text = f"✅ Resposta recebida para {t_id}."
        return result

    def _handle_approve(self, result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription) -> TelegramDispatchResult:
        result.action = TelegramActionType.APPROVE
        subparts = arg_str.split(maxsplit=1)
        if len(subparts) < 2:
            result.response_text = "⚠️ Como usar: /approve <projeto> <versão>"
        else:
            p_id, digest = subparts[0].strip(), subparts[1].strip()
            result.target_id = f"{p_id}:{digest}"
            if self.approval_handler:
                try:
                    res = self.approval_handler(p_id, digest, user_id or 0)
                    result.resumed = True
                    receipt_id = res.get("receipt_id", "RCPT-OK")
                    result.response_text = (
                        f"🚀 Versão aprovada para <b>{p_id}</b>!\n"
                        f"Código: <code>{digest[:12]}...</code>\n"
                        f"Recibo: <b>{receipt_id}</b>\n"
                        f"Publicação em produção autorizada."
                    )
                except Exception as exc:
                    result.error = str(exc)
                    result.response_text = f"❌ Não foi possível aprovar a versão: {exc}"
            else:
                result.resumed = True
                result.response_text = f"🚀 Versão aprovada para {p_id} ({digest[:8]})."
        return result

    def _handle_alerts(self, result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription) -> TelegramDispatchResult:
        result.action = TelegramActionType.ALERTS
        try:
            from core.notifications.models import AlertSeverity
            from core.notifications.store import NotificationStore
            store = NotificationStore()
            events = store.list_notifications(limit=5)
            if not events:
                result.response_text = "✅ <b>Nenhum alerta operacional ativo.</b> Todas as cotas e serviços estão saudáveis."
            else:
                lines = ["🔔 <b>Alertas Operacionais Recentes:</b>\n"]
                for ev in events:
                    icon = "🚨" if ev.severity == AlertSeverity.CRITICAL else ("⚠️" if ev.severity == AlertSeverity.WARNING else "ℹ️")
                    lines.append(f"{icon} <b>[{ev.severity.value.upper()}] {ev.title}</b>\n   {ev.message}")
                result.response_text = "\n\n".join(lines)
        except Exception as exc:
            result.error = str(exc)
            result.response_text = f"❌ Erro ao consultar alertas: {exc}"
        return result

    def _handle_demand0(self, result, msg, user_id, chat_id, arg_str, raw_text, is_voice_demand, voice_transcription) -> TelegramDispatchResult:
        lower_text = raw_text.lower()
        if any(w in lower_text for w in ("ticket", "registrad", "confirm", "numero", "número", "status")):
            try:
                from core.demands.service import DemandsService

                demands_svc = DemandsService()
                tickets = demands_svc.list_tickets("darkfac")
                if tickets:
                    latest = tickets[-1]
                    result.response_text = (
                        f"📋 <b>Último Ticket Registrado:</b>\n\n"
                        f"🆔 <b>ID:</b> <code>{latest.id}</code>\n"
                        f"📌 <b>Título:</b> {latest.title}\n"
                        f"📊 <b>Status:</b> {latest.status.value}\n"
                        f"📝 <b>Descrição:</b> {(latest.problem_statement or latest.title)[:140]}..."
                    )
                else:
                    result.response_text = "ℹ️ Nenhum ticket registrado no momento."
            except Exception as exc:
                result.response_text = "ℹ️ Envie /status ou /help para ver os comandos disponíveis."
        elif not raw_text:
            result.response_text = (
                "ℹ️ <b>Dark Factory Bot:</b> Mensagem recebida sem conteúdo textual ou de áudio reconhecido.\n\n"
                "Para registrar uma nova demanda, envie uma <b>mensagem de voz</b> ou digite:\n"
                "<code>/demand [sua ideia ou requisito]</code>\n\n"
                "Envie <code>/help</code> para ver a lista de comandos."
            )
        else:
            result.response_text = (
                f"💬 Para abrir uma nova demanda com este texto, digite:\n"
                f"<code>/demand {raw_text}</code>\n\n"
                f"Envie /help para ver a lista de comandos."
            )
        return result

    def _handle_command_fallback(self, result, msg, user_id, chat_id, cmd_str, arg_str, raw_text, is_voice_demand, voice_transcription) -> TelegramDispatchResult:
        result.response_text = f"❓ Unknown command: {cmd_str}. Send /help for command list."
        return result

    def _handle_callback(self, update: TelegramUpdate) -> TelegramDispatchResult:
        cb = update.callback_query
        assert cb is not None
        user_id = cb.from_user.id
        chat_id = cb.message.chat.id if cb.message else None
        data_str = cb.data or ""

        if self.config.role == "owner" and cb.id not in self.processed_callback_ids:
            # Owner bot = alerts only: buttons are handled by the ops bot, never here.
            self.processed_callback_ids.add(cb.id)
            return TelegramDispatchResult(
                update_id=update.update_id,
                action=TelegramActionType.UNKNOWN,
                authorized=True,
                response_text="Comandos, demandas e decisões de grill ficam no @darkfac_ops_bot. Este canal (@darkfac_bot) envia apenas alertas.",
            )

        # Check duplicate callback query ID
        if cb.id in self.processed_callback_ids:
            return TelegramDispatchResult(
                update_id=update.update_id,
                action=TelegramActionType.UNKNOWN,
                authorized=True,
                duplicate=True,
                response_text="Callback already handled.",
            )

        # Format: cb:<action>:<arg1>:<arg2>...
        parts = data_str.split(":")
        result = TelegramDispatchResult(
            update_id=update.update_id,
            action=TelegramActionType.UNKNOWN,
            authorized=True,
        )

        if len(parts) >= 4 and parts[1] == "grill" and "#" in parts[2]:
            # HF-27-08 F: cb:grill:<run_id>#<question_id>:<index>, produced
            # by core.line.stage_grill._build_message. Disambiguated from
            # the legacy cb:grill:<ticket_id>:<choice> format by the "#".
            run_id, question_id = parts[2].split("#", 1)
            index = parts[3]
            result.action = TelegramActionType.GRILL
            result.target_id = run_id
            if self.line_grill_handler:
                try:
                    res = self.line_grill_handler(run_id, question_id, index, user_id)
                    result.resumed = res.get("resumed", True)
                    result.response_text = f"✅ Resposta registrada para {run_id} (pergunta {question_id})."
                except Exception as exc:
                    result.error = str(exc)
                    result.response_text = f"❌ Não foi possível enviar a resposta: {exc}"
            else:
                # No handler wired: this is not a success, never claim resumed.
                result.resumed = False
                result.error = "line_grill_handler not configured"
                result.response_text = "❌ Nenhum gravador de alinhamento configurado; a resposta não foi aplicada."

        elif len(parts) >= 4 and parts[1] == "grill":
            # cb:grill:<ticket_id>:<choice>
            ticket_id, choice = parts[2], parts[3]
            result.action = TelegramActionType.GRILL
            result.target_id = ticket_id
            if self.grill_handler:
                try:
                    res = self.grill_handler(ticket_id, choice, user_id)
                    result.resumed = res.get("resumed", True)
                    result.response_text = f"✅ Opção '{choice}' selecionada para {ticket_id}. Desenvolvimento retomado."
                except Exception as exc:
                    result.error = str(exc)
                    result.response_text = f"❌ Não foi possível registrar a escolha: {exc}"
            else:
                result.resumed = True
                result.response_text = f"Opção '{choice}' registrada para {ticket_id}."

        elif len(parts) >= 3 and parts[1] == "accept":
            # HF-27-08 D-g/F: cb:accept:<run_id> -- commercial acceptance.
            run_id = parts[2]
            result.action = TelegramActionType.ACCEPT
            result.target_id = run_id
            if self.commercial_acceptance_handler:
                try:
                    res = self.commercial_acceptance_handler(run_id, user_id)
                    result.resumed = res.get("resumed", True)
                    sha = res.get("sha", "")
                    result.response_text = f"✅ Aceite registrado para {run_id} ({sha[:12]}). Publicação liberada."
                except Exception as exc:
                    result.error = str(exc)
                    result.response_text = f"❌ Não foi possível registrar o aceite: {exc}"
            else:
                # No handler wired: this is not a success, never claim resumed.
                result.resumed = False
                result.error = "commercial_acceptance_handler not configured"
                result.response_text = "❌ Nenhum gravador de aceite configurado; nada foi registrado."

        elif len(parts) >= 4 and parts[1] == "release":
            # cb:release:<project_id>:<digest>:<choice>
            project_id, digest, choice = parts[2], parts[3], parts[4] if len(parts) > 4 else "approved"
            result.action = TelegramActionType.APPROVE
            result.target_id = f"{project_id}:{digest}"
            if choice.lower() in ("approve", "approved", "yes"):
                if self.approval_handler:
                    try:
                        res = self.approval_handler(project_id, digest, user_id)
                        result.resumed = True
                        receipt = res.get("receipt_id", "RCPT-OK")
                        result.response_text = f"🚀 Versão aprovada ({receipt}). Publicando em produção."
                    except Exception as exc:
                        result.error = str(exc)
                        result.response_text = f"❌ Não foi possível aprovar a versão: {exc}"
                else:
                    result.resumed = True
                    result.response_text = f"Publicação aprovada para {project_id}."
            else:
                result.response_text = f"Publicação adiada/rejeitada para {project_id}."

        else:
            result.response_text = f"Ação recebida: {data_str}"

        self.processed_callback_ids.add(cb.id)
        return result

    def handle_webhook(
        self,
        payload: Dict[str, Any],
        secret_token: Optional[str] = None,
    ) -> TelegramDispatchResult:
        """Handle incoming webhook update with secret token verification and durable dispatch."""
        if self.config.webhook_secret_token and secret_token != self.config.webhook_secret_token:
            logger.warning("Rejected webhook update: secret token mismatch")
            return TelegramDispatchResult(
                update_id=payload.get("update_id", 0),
                action=TelegramActionType.UNAUTHORIZED,
                authorized=False,
                error="Invalid webhook secret token",
                response_text="Access Denied: Invalid webhook secret token.",
            )
        return self.process_update(payload)

    def poll_updates(
        self,
        limit: int = 100,
        timeout: Optional[int] = None,
    ) -> List[TelegramDispatchResult]:
        """Poll updates from Telegram using durable last_offset."""
        if not self.config.bot_token:
            logger.debug("Telegram polling skipped: no bot_token configured")
            return []

        timeout_sec = timeout if timeout is not None else self.config.poll_timeout_seconds
        url = f"{self.config.api_base_url}/bot{self.config.bot_token}/getUpdates?offset={self.last_offset}&limit={limit}&timeout={timeout_sec}"
        req = urllib.request.Request(
            url,
            headers={"Content-Type": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec + 5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if not data.get("ok"):
                    logger.warning("Telegram getUpdates returned error: %s", data)
                    return []
                updates = data.get("result", [])
                results: List[TelegramDispatchResult] = []
                for u in updates:
                    res = self.process_update(u)
                    results.append(res)
                    if not res.duplicate:
                        chat_id = (
                            u.get("message", {}).get("chat", {}).get("id")
                            or u.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")
                        )
                        if chat_id:
                            self.deliver_reply(chat_id, res)
                return results
        except Exception as exc:
            status_code = getattr(exc, "code", None)
            if status_code == 401:
                logger.error(
                    "Telegram poll_updates failed: HTTP 401 Unauthorized (bot token is invalid or revoked for role=%s)",
                    self.config.role,
                )
            elif status_code == 409:
                logger.error(
                    "Telegram poll_updates failed: HTTP 409 Conflict (active webhook or concurrent poll for role=%s)",
                    self.config.role,
                )
            else:
                logger.warning(
                    "Failed to poll Telegram updates for role=%s: %s%s",
                    self.config.role,
                    type(exc).__name__,
                    f" (HTTP {status_code})" if isinstance(status_code, int) else "",
                )
            return []

    def deliver_reply(self, chat_id: int, result: TelegramDispatchResult) -> None:
        """Send every part of a dispatch. No-op for duplicates and empty replies."""

        if result.duplicate or not chat_id:
            return
        for text in reply_messages(result):
            self.send_message(chat_id, text)

    def send_message(
        self,
        chat_id: int,
        text: str,
        buttons: Optional[List[List[Dict[str, str]]]] = None,
        parse_mode: str = "HTML",
        *,
        notification_id: Optional[str] = None,
    ) -> bool:
        """Send a message. Returns bool. Permanent Telegram errors are not retried.

        ``invalid_entities`` is the only HTTP 400 that gets one plain-text attempt.
        Transient failures stay ``pending`` in the local outbox. A confirmed receipt
        or an ambiguous timeout blocks another automatic send of the same key.
        ``chat_id=0`` is not a destination and never calls ``sendMessage``.
        """

        safe_text = redact_secrets(text)
        note_id = notification_id or _new_notification_id()
        role = _role_of(self.config.role)
        if chat_id == 0:
            log_telegram_delivery(
                notification_id=note_id,
                role=role,
                http_status=None,
                category="missing_destination",
            )
            return False

        if not self.config.bot_token:
            return self._queue_without_token(chat_id, safe_text, buttons, note_id, role)

        gate = self._begin_delivery(note_id, role, chat_id, safe_text, buttons, parse_mode)
        if gate.action == "delivered":
            return True
        if gate.action != "send" or gate.claim is None:
            return False
        url = f"{self.config.api_base_url}/bot{self.config.bot_token}/sendMessage"
        return self._send_with_classified_retries(
            url=url,
            chat_id=chat_id,
            safe_text=safe_text,
            buttons=buttons,
            parse_mode=parse_mode,
            note_id=note_id,
            role=role,
            claim=gate.claim,
        )

    def _queue_without_token(
        self,
        chat_id: int,
        safe_text: str,
        buttons: Optional[List[List[Dict[str, str]]]],
        note_id: str,
        role: str,
    ) -> bool:
        existing = self._lookup_outbox(note_id, role, chat_id)
        if existing is not None and existing.get("status") == OUTBOX_DELIVERED:
            return True
        if existing is not None and existing.get("status") == OUTBOX_UNCERTAIN:
            return False
        logger.info("Telegram bot_token not configured; storing in outbox.")
        self._enqueue_outbox(chat_id, safe_text, buttons, "No bot_token configured", notification_id=note_id)
        return False

    def _send_with_classified_retries(
        self,
        *,
        url: str,
        chat_id: int,
        safe_text: str,
        buttons: Optional[List[List[Dict[str, str]]]],
        parse_mode: str,
        note_id: str,
        role: str,
        claim: dict[str, Any],
    ) -> bool:
        active_parse_mode = parse_mode
        for attempt in range(3):
            outcome = _post_send_message(
                url,
                _telegram_payload(chat_id, safe_text, buttons, active_parse_mode),
            )
            if outcome.ok:
                return self._close_delivery(claim, OUTBOX_DELIVERED, message_id=outcome.message_id)
            log_telegram_delivery(
                notification_id=note_id,
                role=role,
                http_status=outcome.http_status,
                category=outcome.category,
            )
            if outcome.category == OUTBOX_UNCERTAIN:
                return self._close_delivery(claim, OUTBOX_UNCERTAIN, reason="category=uncertain")
            if outcome.category == "invalid_entities" and active_parse_mode:
                return self._retry_plain(url, chat_id, safe_text, buttons, note_id, role, claim)
            if outcome.category != "transient":
                return self._close_delivery(claim, "drop")
            if attempt >= 2:
                return self._close_delivery(claim, OUTBOX_PENDING, reason="category=transient")
            _retry_pause(0.8 * (attempt + 1))
        return self._close_delivery(claim, OUTBOX_PENDING, reason="category=transient")

    def _retry_plain(
        self,
        url: str,
        chat_id: int,
        safe_text: str,
        buttons: Optional[List[List[Dict[str, str]]]],
        note_id: str,
        role: str,
        claim: dict[str, Any],
    ) -> bool:
        plain = _post_send_message(url, _telegram_payload(chat_id, safe_text, buttons, ""))
        if plain.ok:
            return self._close_delivery(
                claim,
                OUTBOX_DELIVERED,
                message_id=plain.message_id,
                parse_mode="",
            )
        log_telegram_delivery(
            notification_id=note_id,
            role=role,
            http_status=plain.http_status,
            category=plain.category,
        )
        if plain.category == OUTBOX_UNCERTAIN:
            return self._close_delivery(claim, OUTBOX_UNCERTAIN, reason="category=uncertain", parse_mode="")
        if plain.category == "transient":
            return self._close_delivery(claim, OUTBOX_PENDING, reason="category=transient", parse_mode="")
        return self._close_delivery(claim, "drop")

    def _close_delivery(
        self,
        claim: dict[str, Any],
        status: str,
        *,
        message_id: int | None = None,
        reason: str | None = None,
        parse_mode: str | None = None,
    ) -> bool:
        self._finish_delivery(claim, status, message_id=message_id, reason=reason, parse_mode=parse_mode)
        return status == OUTBOX_DELIVERED

    def replay_outbox(self) -> int:
        """Send each pending row once. Delivered and uncertain rows are left untouched."""

        delivered = 0
        touched: set[str] = set()
        while True:
            claim = self._claim_next_pending(touched)
            if claim is None:
                return delivered
            touched.add(str(claim.get("key") or ""))
            outcome = self._post_claimed(claim)
            if outcome is None:
                reason = claim.get("failure_reason") if isinstance(claim.get("failure_reason"), str) else None
                self._finish_delivery(claim, OUTBOX_PENDING, reason=reason or "No bot_token configured")
                continue
            if outcome.ok:
                self._finish_delivery(claim, OUTBOX_DELIVERED, message_id=outcome.message_id)
                delivered += 1
                continue
            log_telegram_delivery(
                notification_id=str(claim.get("notification_id") or ""),
                role=str(claim.get("role") or self.config.role),
                http_status=outcome.http_status,
                category=outcome.category,
            )
            if outcome.category == OUTBOX_UNCERTAIN:
                self._finish_delivery(claim, OUTBOX_UNCERTAIN, reason="category=uncertain")
            elif outcome.category == "transient":
                self._finish_delivery(claim, OUTBOX_PENDING, reason="category=transient")
            else:
                self._finish_delivery(claim, OUTBOX_PERMANENT, reason=f"category={outcome.category}")

    def _post_claimed(self, claim: dict[str, Any]) -> _SendAttempt | None:
        if not self.config.bot_token:
            return None
        try:
            chat_id = int(claim.get("chat_id") or 0)
        except (TypeError, ValueError):
            chat_id = 0
        if chat_id == 0:
            return _SendAttempt(False, None, "permanent")
        url = f"{self.config.api_base_url}/bot{self.config.bot_token}/sendMessage"
        parse_mode = claim.get("parse_mode") if isinstance(claim.get("parse_mode"), str) else "HTML"
        buttons = claim.get("buttons") if isinstance(claim.get("buttons"), list) else None
        return _post_send_message(
            url,
            _telegram_payload(chat_id, str(claim.get("text") or ""), buttons, parse_mode),
        )

    def _enqueue_outbox(
        self,
        chat_id: int,
        text: str,
        buttons: Optional[List[List[Dict[str, str]]]],
        reason: str,
        notification_id: Optional[str] = None,
    ) -> None:
        """Store one undelivered alert. The same key updates the existing row."""

        role = _role_of(self.config.role)
        note_id = notification_id or _new_notification_id()
        key = outbox_item_key(note_id, role, chat_id)

        def mutate(items: list[dict[str, Any]]) -> None:
            now = datetime.now(UTC).isoformat()
            for item in items:
                if item.get("key") != key:
                    continue
                if item.get("status") in {OUTBOX_DELIVERED, OUTBOX_UNCERTAIN, OUTBOX_SENDING}:
                    return
                item["text"] = text
                item["buttons"] = buttons
                item["failure_reason"] = reason
                item["status"] = OUTBOX_PENDING
                item["delivered"] = False
                item["updated_at"] = now
                item["claim_token"] = None
                item["claim_pid"] = None
                item["claimed_at"] = None
                item["message_id"] = None
                item["delivered_at"] = None
                item["fingerprint"] = None
                return
            items.append(
                _outbox_row(
                    notification_id=note_id,
                    role=role,
                    chat_id=chat_id,
                    text=text,
                    buttons=buttons,
                    parse_mode="HTML",
                    status=OUTBOX_PENDING,
                    reason=reason,
                )
            )

        self._update_outbox(mutate)

    def get_status(self) -> Dict[str, Any]:
        """Return diagnostic status of Telegram Gateway."""
        pending = sum(1 for item in self._read_outbox_items() if item.get("status") == OUTBOX_PENDING)
        return {
            "configured": bool(self.config.bot_token),
            "authorized_user_count": len(self.config.authorized_user_ids),
            "authorized_chat_count": len(self.config.authorized_chat_ids),
            "last_offset": self.last_offset,
            "processed_updates": len(self.processed_update_ids),
            "processed_callbacks": len(self.processed_callback_ids),
            "pending_outbox_notifications": pending,
        }

    def _lookup_outbox(self, notification_id: str, role: str, chat_id: int) -> dict[str, Any] | None:
        key = outbox_item_key(notification_id, _role_of(role), chat_id)
        for item in self._read_outbox_items():
            if item.get("key") == key:
                return item
        return None

    def _begin_delivery(
        self,
        note_id: str,
        role: str,
        chat_id: int,
        text: str,
        buttons: Optional[List[List[Dict[str, str]]]],
        parse_mode: str,
    ) -> _DeliveryGate:
        key = outbox_item_key(note_id, _role_of(role), chat_id)
        gate = _DeliveryGate("stop", None)

        def mutate(items: list[dict[str, Any]]) -> None:
            nonlocal gate
            current = next((item for item in items if item.get("key") == key), None)
            status = current.get("status") if current is not None else None
            if current is not None and status == OUTBOX_SENDING and claim_is_abandoned(current):
                status = OUTBOX_PENDING
            if status == OUTBOX_DELIVERED:
                gate = _DeliveryGate("delivered", None)
                return
            if status in {OUTBOX_UNCERTAIN, OUTBOX_SENDING}:
                gate = _DeliveryGate("stop", None)
                return
            created = current.get("created_at") if current is not None else None
            row = _outbox_row(
                notification_id=note_id,
                role=role,
                chat_id=chat_id,
                text=text,
                buttons=buttons,
                parse_mode=parse_mode,
                status=OUTBOX_SENDING,
                reason=None,
                created_at=created if isinstance(created, str) else None,
            )
            if current is None:
                items.append(row)
            else:
                current.clear()
                current.update(row)
            gate = _DeliveryGate("send", dict(row))

        if not self._update_outbox(mutate):
            return _DeliveryGate("stop", None)
        return gate

    def _claim_next_pending(self, skip: set[str]) -> dict[str, Any] | None:
        claimed: dict[str, Any] | None = None

        def mutate(items: list[dict[str, Any]]) -> None:
            nonlocal claimed
            for item in items:
                key = str(item.get("key") or "")
                if key in skip:
                    continue
                status = item.get("status")
                if status == OUTBOX_SENDING and claim_is_abandoned(item):
                    status = OUTBOX_PENDING
                if status != OUTBOX_PENDING:
                    continue
                now = datetime.now(UTC).isoformat()
                item["status"] = OUTBOX_SENDING
                item["claim_token"] = uuid.uuid4().hex
                item["claim_pid"] = os.getpid()
                item["claimed_at"] = now
                item["updated_at"] = now
                item["delivered"] = False
                claimed = dict(item)
                return

        if not self._update_outbox(mutate):
            return None
        return claimed

    def _finish_delivery(
        self,
        claim: dict[str, Any],
        status: str,
        *,
        message_id: int | None = None,
        reason: str | None = None,
        parse_mode: str | None = None,
    ) -> None:
        key = claim.get("key")
        token = claim.get("claim_token")

        def mutate(items: list[dict[str, Any]]) -> None:
            for index, item in enumerate(items):
                if item.get("key") != key or item.get("claim_token") != token:
                    continue
                if status == "drop":
                    items.pop(index)
                    return
                now = datetime.now(UTC).isoformat()
                item["status"] = status
                item["updated_at"] = now
                item["delivered"] = status == OUTBOX_DELIVERED
                item["claim_token"] = None
                item["claim_pid"] = None
                item["claimed_at"] = None
                if parse_mode is not None:
                    item["parse_mode"] = parse_mode
                if status == OUTBOX_DELIVERED:
                    item["message_id"] = message_id
                    item["delivered_at"] = now
                    item["fingerprint"] = delivery_fingerprint(str(item.get("text") or ""))
                    item["failure_reason"] = None
                elif status == OUTBOX_PENDING:
                    item["failure_reason"] = reason
                    item["message_id"] = None
                    item["delivered_at"] = None
                    item["fingerprint"] = None
                else:
                    item["failure_reason"] = reason
                return

        self._update_outbox(mutate)

    def _read_outbox_items(self) -> list[dict[str, Any]]:
        if not self.outbox_file.exists():
            return []
        try:
            raw = json.loads(self.outbox_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return []
        if isinstance(raw, dict):
            raw = raw.get("items")
        if not isinstance(raw, list):
            return []
        items: list[dict[str, Any]] = []
        for entry in raw:
            if isinstance(entry, dict) and entry.get("notification_id"):
                items.append(normalize_outbox_item(entry, self.config.role))
        return items

    def _write_outbox_atomic(self, items: list[dict[str, Any]]) -> None:
        self.outbox_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(items, indent=2, ensure_ascii=False)
        temp = self.outbox_file.with_name(f"{self.outbox_file.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(payload, encoding="utf-8")
        last_error: OSError | None = None
        for _attempt in range(20):
            try:
                temp.replace(self.outbox_file)
                return
            except OSError as exc:
                last_error = exc
                time.sleep(0.01)
        if last_error is not None:
            raise last_error

    def _update_outbox(self, mutator: Callable[[list[dict[str, Any]]], None]) -> bool:
        """Read, change and replace the outbox. A failed replace keeps the previous file."""

        acquired = False
        try:
            with _outbox_guard(self.outbox_file):
                self._acquire_outbox_lock()
                acquired = True
                items = self._read_outbox_items()
                mutator(items)
                self._write_outbox_atomic(items)
                return True
        except (OSError, TimeoutError) as exc:
            logger.warning("telegram outbox write failed: %s", type(exc).__name__)
            return False
        finally:
            if acquired:
                self._release_outbox_lock()

    def _lock_path(self) -> Path:
        return self.outbox_file.with_name(self.outbox_file.name + ".lock")

    def _acquire_outbox_lock(self) -> None:
        path = self._lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 5.0
        while True:
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if time.monotonic() >= deadline:
                    if self._lock_is_stale(path):
                        try:
                            path.unlink()
                        except OSError:
                            pass
                        continue
                    raise TimeoutError("telegram outbox lock")
                if self._lock_is_stale(path):
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                time.sleep(0.01)
                continue
            try:
                os.write(fd, str(os.getpid()).encode("ascii"))
            finally:
                os.close(fd)
            return

    def _lock_is_stale(self, path: Path) -> bool:
        """A lock just created by another thread is not stale, even before the PID is written."""

        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return True
        if age < 1.0:
            return False
        try:
            pid = int(path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return True
        return not _pid_alive(pid)

    def _release_outbox_lock(self) -> None:
        try:
            self._lock_path().unlink(missing_ok=True)
        except OSError:
            return


# Interop alias
TelegramService = TelegramGateway
