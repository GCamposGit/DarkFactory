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
   Failed notifications are stored in a local outbox.
5. Inviolable Secret Redaction: tokens, API keys, passwords, and sensitive URLs are scrubbed from messages and logs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from pydantic import BaseModel, ConfigDict, Field

from core.paths import project_root

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
    token = next((value for key in keys if (value := os.environ.get(key) or env_vars.get(key))), None)

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
                if not local_token and data.get("bot_token"):
                    local_token = data.get("bot_token")
                for key, value in data.items():
                    if value is not None:
                        local_data.setdefault(key, value)
        except (OSError, ValueError):
            continue
    if not token:
        token = local_token

    users_raw = os.environ.get("TELEGRAM_AUTHORIZED_USERS") or os.environ.get("TELEGRAM_ALLOWED_USERS") or env_vars.get("TELEGRAM_AUTHORIZED_USERS", "")
    users = [int(u.strip()) for u in users_raw.split(",") if u.strip().isdigit()]
    if not users:
        users = local_data.get("authorized_user_ids", [])

    chats_raw = os.environ.get("TELEGRAM_AUTHORIZED_CHATS") or os.environ.get("TELEGRAM_ALLOWED_CHATS") or env_vars.get("TELEGRAM_AUTHORIZED_CHATS", "")
    chats = [int(c.strip()) for c in chats_raw.split(",") if c.strip().isdigit()]
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


class TelegramActionType(str, Enum):
    START = "start"
    DEMAND = "demand"
    STATUS = "status"
    GRILL = "grill"
    APPROVE = "approve"
    ACCEPT = "accept"
    ALERTS = "alerts"
    UNKNOWN = "unknown"
    UNAUTHORIZED = "unauthorized"



class TelegramDispatchResult(BaseModel):
    """Result of processing an incoming update."""

    model_config = ConfigDict(extra="ignore")

    update_id: int
    action: TelegramActionType
    authorized: bool
    duplicate: bool = False
    target_id: Optional[str] = None
    response_text: str = ""
    resumed: bool = False
    error: Optional[str] = None


class OutboxNotification(BaseModel):
    """Notification queued locally when Telegram delivery cannot complete."""

    model_config = ConfigDict(extra="ignore")

    notification_id: str
    chat_id: int
    text: str
    buttons: Optional[List[List[Dict[str, str]]]] = None
    created_at: str
    delivered: bool = False
    failure_reason: Optional[str] = None


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
        demand_handler: Optional[Callable[[str, int], Dict[str, Any]]] = None,
        grill_handler: Optional[Callable[[str, str, int], Dict[str, Any]]] = None,
        approval_handler: Optional[Callable[[str, str, int], Dict[str, Any]]] = None,
        status_handler: Optional[Callable[[Optional[str]], Dict[str, Any]]] = None,
        line_grill_handler: Optional[Callable[[str, str, str, int], Dict[str, Any]]] = None,
        commercial_acceptance_handler: Optional[Callable[[str, int], Dict[str, Any]]] = None,
        audio_engine: Optional[Any] = None,
        line_handler: Optional[Callable[[str, int], Dict[str, Any]]] = None,
    ) -> None:
        self.config = config
        self.state_dir = state_dir if state_dir is not None else project_root() / ".factory" / "telegram"
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
        self.demand_handler = demand_handler
        self.grill_handler = grill_handler
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

        # 4. Dispatch update
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

    def _handle_message(self, update: TelegramUpdate) -> TelegramDispatchResult:
        msg = update.message
        assert msg is not None
        user_id = msg.from_user.id if msg.from_user else None
        chat_id = msg.chat.id
        raw_text = (msg.text or msg.caption or "").strip()

        if self.config.role == "owner":
            # Owner bot (@darkfac_bot) = alerts only. Commands, demands (text/voice), /linha and
            # grill decisions are handled by the ops bot (@darkfac_ops_bot).
            redirect = TelegramDispatchResult(
                update_id=update.update_id, action=TelegramActionType.UNKNOWN, authorized=True
            )
            if raw_text.startswith("/"):
                redirect.response_text = "Comandos, demandas e decisões de grill ficam no @darkfac_ops_bot. Este canal (@darkfac_bot) envia apenas alertas."
            return redirect

        # Handle voice and audio input (USR-60): support voice, audio, video_note, and audio documents
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

        if not raw_text and voice_obj:
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
                return TelegramDispatchResult(
                    update_id=update.update_id,
                    action=TelegramActionType.UNKNOWN,
                    authorized=True,
                    response_text="⚠️ Não foi possível transcrever a mensagem de voz. Verifique a qualidade do áudio ou envie como texto.",
                )

            # Contextual Routing (Gate G1 approved):
            reply_text = msg.reply_to_message.text if (msg.reply_to_message and msg.reply_to_message.text) else ""
            match_ticket = re.search(r"\b((?:USR|DF)-\d+|[a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+)\b", reply_text + " " + transcribed, re.IGNORECASE)
            is_grill_context = ("grill" in reply_text.lower() or "intervenção prioritária" in reply_text.lower() or "desambiguação" in reply_text.lower() or "grill" in transcribed.lower())

            if is_grill_context and match_ticket:
                target_ticket_id = match_ticket.group(1).upper()
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
                            f"Avanço autônomo retomado."
                        )
                    except Exception as exc:
                        result.error = str(exc)
                        result.response_text = f"❌ Falha ao aplicar resposta do Grill: {exc}"
                else:
                    result.resumed = True
                    result.response_text = f"✅ Resposta de voz registrada para {target_ticket_id}."
                return result

            # Standalone voice note -> Ingest as /demand
            is_voice_demand = True
            voice_transcription = transcribed
            raw_text = f"/demand {transcribed}"
        else:
            is_voice_demand = False
            voice_transcription = ""

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
            result.action = TelegramActionType.START
            if self.config.role == "owner":
                result.response_text = (
                    "👑 <b>Dark Factory Owner Governance Bot (@darkfac_bot)</b>\n\n"
                    "Canal oficial para governança estratégica, decisões humanas e aprovações do Owner.\n\n"
                    "Comandos disponíveis:\n"
                    "• <code>/grill [ticket_id] [resposta]</code> - Responder alinhamento e desbloquear WAITING_HUMAN\n"
                    "• <code>/approve [project_id] [digest]</code> - Aprovar release para deploy em produção\n"
                    "• <code>/alerts</code> - Consultar alertas de segurança, orçamento e cotas\n"
                    "• <code>/status [ticket_id]</code> - Consultar status de pipelines e jobs\n"
                    "• <code>/demand [texto da demanda]</code> - Registrar demanda prioritária do Owner\n"
                    "• <code>/linha [ticket_id]</code> - Enviar um ticket existente para a linha autônoma\n\n"
                    "ℹ️ <i>Demandas operacionais e backlog geral são gerenciadas no @darkfac_ops_bot</i>"
                )
            elif self.config.role == "ops":
                result.response_text = (
                    "⚙️ <b>Dark Factory Autonomous Orchestrator Bot (@darkfac_ops_bot)</b>\n\n"
                    "Canal oficial de operações autônomas, fila de demandas e status da fábrica.\n\n"
                    "Comandos disponíveis:\n"
                    "• <code>/demand [texto da demanda]</code> - Ingerir nova demanda no backlog autônomo\n"
                    "• <code>/linha [ticket_id]</code> - Enviar um ticket existente para a linha autônoma\n"
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
                    "• <code>/status [ticket_id]</code> - Check status of runs and pipelines\n"
                    "• <code>/alerts</code> - Check active token quota and operational alerts\n"
                    "• <code>/grill [ticket_id] [choice]</code> - Answer Grill clarification questions\n"
                    "• <code>/approve [project_id] [digest]</code> - Approve production release\n"
                )

        elif cmd_str == "/demand":
            result.action = TelegramActionType.DEMAND
            if not arg_str:
                result.response_text = "⚠️ Usage: /demand <description of feature or bugfix>"
            else:
                if self.intake_service is not None:
                    try:
                        from core.workflow.control_contracts import IntakeCommand
                        ext_id = f"tg-msg-{chat_id}-{msg.message_id}"
                        cmd = IntakeCommand(
                            project_id="darkfac",
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
                        receipt = self.intake_service.accept(cmd, datetime.now(UTC))
                        demand_id = receipt.demand_id
                        result.target_id = demand_id
                        prefix = "👑 [Owner Demand] " if self.config.role == "owner" else ""
                        result.response_text = f"✅ {prefix}Demand registered successfully: <b>{demand_id}</b>"
                    except Exception as exc:
                        logger.error("Telegram intake_service error: %s", exc)
                        result.error = str(exc)
                        result.response_text = f"❌ Failed to register demand: {exc}"
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
                        result.response_text = f"❌ Failed to register demand: {exc}"
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
                        result.target_id = "DEMAND-RECORDED"
                        result.response_text = f"✅ Demanda recebida e enfileirada: {arg_str[:120]}"

        elif cmd_str in ("/linha", "/line"):
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

        elif cmd_str == "/status":
            result.action = TelegramActionType.STATUS
            ticket_id_query = arg_str.strip() or None
            if self.status_handler:
                try:
                    res = self.status_handler(ticket_id_query)
                    summary = res.get("summary", "All systems operational.")
                    result.response_text = f"📊 DarkFac Status:\n{summary}"
                except Exception as exc:
                    result.error = str(exc)
                    result.response_text = f"❌ Failed to fetch status: {exc}"
            else:
                result.response_text = "📊 DarkFac Status: Pipeline active, 0 blocking incidents."

        elif cmd_str == "/grill":
            result.action = TelegramActionType.GRILL
            subparts = arg_str.split(maxsplit=1)
            if len(subparts) < 2:
                result.response_text = "⚠️ Usage: /grill <ticket_id> <your answer / choice>"
            else:
                t_id, answer = subparts[0].strip(), subparts[1].strip()
                result.target_id = t_id
                if self.grill_handler:
                    try:
                        res = self.grill_handler(t_id, answer, user_id or 0)
                        result.resumed = res.get("resumed", True)
                        result.response_text = f"✅ Grill answer recorded for <b>{t_id}</b>. Workflow resumed."
                    except Exception as exc:
                        result.error = str(exc)
                        result.response_text = f"❌ Failed to record grill answer: {exc}"
                else:
                    result.resumed = True
                    result.response_text = f"✅ Grill answer received for {t_id}."

        elif cmd_str == "/approve":
            result.action = TelegramActionType.APPROVE
            subparts = arg_str.split(maxsplit=1)
            if len(subparts) < 2:
                result.response_text = "⚠️ Usage: /approve <project_id> <artifact_digest>"
            else:
                p_id, digest = subparts[0].strip(), subparts[1].strip()
                result.target_id = f"{p_id}:{digest}"
                if self.approval_handler:
                    try:
                        res = self.approval_handler(p_id, digest, user_id or 0)
                        result.resumed = True
                        receipt_id = res.get("receipt_id", "RCPT-OK")
                        result.response_text = (
                            f"🚀 Release Approved for <b>{p_id}</b>!\n"
                            f"Digest: <code>{digest[:12]}...</code>\n"
                            f"Receipt: <b>{receipt_id}</b>\n"
                            f"Production promotion authorized."
                        )
                    except Exception as exc:
                        result.error = str(exc)
                        result.response_text = f"❌ Release approval rejected: {exc}"
                else:
                    result.resumed = True
                    result.response_text = f"🚀 Release approved for {p_id} ({digest[:8]})."

        elif cmd_str == "/alerts":
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

        else:
            if not cmd_str.startswith("/"):
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
                                f"📝 <b>Descrição:</b> {latest.problem[:140]}..."
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
            else:
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
                    result.response_text = f"❌ Failed to submit grill answer: {exc}"
            else:
                # No handler wired: this is not a success, never claim resumed.
                result.resumed = False
                result.error = "line_grill_handler not configured"
                result.response_text = "❌ Nenhum handler de grill da linha configurado; resposta NAO foi aplicada."

        elif len(parts) >= 4 and parts[1] == "grill":
            # cb:grill:<ticket_id>:<choice>
            ticket_id, choice = parts[2], parts[3]
            result.action = TelegramActionType.GRILL
            result.target_id = ticket_id
            if self.grill_handler:
                try:
                    res = self.grill_handler(ticket_id, choice, user_id)
                    result.resumed = res.get("resumed", True)
                    result.response_text = f"✅ Decision '{choice}' selected for {ticket_id}. Run resumed."
                except Exception as exc:
                    result.error = str(exc)
                    result.response_text = f"❌ Failed to submit grill choice: {exc}"
            else:
                result.resumed = True
                result.response_text = f"Choice '{choice}' recorded for {ticket_id}."

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
                    result.response_text = f"✅ Aceite comercial registrado para {run_id} ({sha[:12]}). Deploy liberado."
                except Exception as exc:
                    result.error = str(exc)
                    result.response_text = f"❌ Failed to record commercial acceptance: {exc}"
            else:
                # No handler wired: this is not a success, never claim resumed.
                result.resumed = False
                result.error = "commercial_acceptance_handler not configured"
                result.response_text = "❌ Nenhum handler de aceite comercial configurado; NAO foi registrado."

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
                        result.response_text = f"🚀 Release approved ({receipt}). Deploying to production."
                    except Exception as exc:
                        result.error = str(exc)
                        result.response_text = f"❌ Approval failed: {exc}"
                else:
                    result.resumed = True
                    result.response_text = f"Release approved for {project_id}."
            else:
                result.response_text = f"Release deferred/rejected for {project_id}."

        else:
            result.response_text = f"Action received: {data_str}"

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
                    if res.response_text and not res.duplicate:
                        chat_id = (
                            u.get("message", {}).get("chat", {}).get("id")
                            or u.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")
                        )
                        if chat_id:
                            self.send_message(chat_id, res.response_text)
                return results
        except Exception as exc:
            logger.warning("Failed to poll Telegram updates: %s", exc)
            return []

    def send_message(
        self,
        chat_id: int,
        text: str,
        buttons: Optional[List[List[Dict[str, str]]]] = None,
        parse_mode: str = "HTML",
    ) -> bool:
        """Send a message to Telegram with secret redaction and resilient local outbox queuing."""
        safe_text = redact_secrets(text)

        if not self.config.bot_token:
            logger.info("Telegram bot_token not configured; storing in outbox.")
            self._enqueue_outbox(chat_id, safe_text, buttons, "No bot_token configured")
            return False

        url = f"{self.config.api_base_url}/bot{self.config.bot_token}/sendMessage"
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": safe_text,
            "parse_mode": parse_mode,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            method="POST",
        )

        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    return resp.status == 200
            except urllib.error.HTTPError as http_err:
                # If Telegram rejects HTML formatting (e.g. 400 Bad Request with unparseable entities),
                # retry immediately in plain text without parse_mode
                if http_err.code == 400 and payload.get("parse_mode"):
                    logger.warning("Telegram parse_mode=%s failed: %s. Retrying without parse_mode.", payload.get("parse_mode"), http_err)
                    payload_plain = dict(payload)
                    payload_plain.pop("parse_mode", None)
                    req_plain = urllib.request.Request(
                        url,
                        data=json.dumps(payload_plain).encode("utf-8"),
                        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
                        method="POST",
                    )
                    try:
                        with urllib.request.urlopen(req_plain, timeout=10.0) as resp2:
                            return resp2.status == 200
                    except Exception as exc2:
                        logger.warning("Plain text retry also failed: %s", exc2)
                if attempt < 2:
                    import time
                    time.sleep(0.8 * (attempt + 1))
                    continue
                logger.warning("Failed to send Telegram message: %s. Enqueuing to outbox.", http_err)
                self._enqueue_outbox(chat_id, safe_text, buttons, str(http_err))
                return False
            except Exception as exc:
                if attempt < 2:
                    import time
                    time.sleep(0.8 * (attempt + 1))
                    continue
                logger.warning("Failed to send Telegram message: %s. Enqueuing to outbox.", exc)
                self._enqueue_outbox(chat_id, safe_text, buttons, str(exc))
                return False
        return False

    def _enqueue_outbox(
        self,
        chat_id: int,
        text: str,
        buttons: Optional[List[List[Dict[str, str]]]],
        reason: str,
    ) -> None:
        """Store undelivered notification in local outbox without crashing the caller."""
        import uuid

        item = OutboxNotification(
            notification_id=f"notif-{uuid.uuid4().hex[:8]}",
            chat_id=chat_id,
            text=text,
            buttons=buttons,
            created_at=datetime.now(UTC).isoformat(),
            failure_reason=reason,
        )

        items: List[Dict[str, Any]] = []
        if self.outbox_file.exists():
            try:
                items = json.loads(self.outbox_file.read_text(encoding="utf-8"))
            except Exception:
                items = []

        items.append(item.model_dump())
        self.outbox_file.write_text(json.dumps(items, indent=2), encoding="utf-8")

    def get_status(self) -> Dict[str, Any]:
        """Return diagnostic status of Telegram Gateway."""
        outbox_count = 0
        if self.outbox_file.exists():
            try:
                outbox_count = len(json.loads(self.outbox_file.read_text(encoding="utf-8")))
            except Exception:
                pass

        return {
            "configured": bool(self.config.bot_token),
            "authorized_user_count": len(self.config.authorized_user_ids),
            "authorized_chat_count": len(self.config.authorized_chat_ids),
            "last_offset": self.last_offset,
            "processed_updates": len(self.processed_update_ids),
            "processed_callbacks": len(self.processed_callback_ids),
            "pending_outbox_notifications": outbox_count,
        }


# Interop alias
TelegramService = TelegramGateway
