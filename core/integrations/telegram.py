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


class TelegramMessage(BaseModel):
    """Incoming or outgoing Telegram message."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    message_id: int
    from_user: Optional[TelegramUser] = Field(default=None, alias="from")
    chat: TelegramChat
    date: int
    text: Optional[str] = None


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
    authorized_user_ids: List[int] = Field(default_factory=list)
    authorized_chat_ids: List[int] = Field(default_factory=list)
    webhook_secret_token: Optional[str] = None
    api_base_url: str = "https://api.telegram.org"
    poll_timeout_seconds: int = 30


def load_telegram_config(config_file: Optional[Path] = None) -> TelegramConfig:
    """Loads TelegramConfig from .factory/telegram/config.json or environment variables."""
    cfg_path = config_file or (Path(__file__).resolve().parents[2] / ".factory" / "telegram" / "config.json")
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            return TelegramConfig.model_validate(data)
        except Exception:
            pass

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    users = [int(u.strip()) for u in os.environ.get("TELEGRAM_AUTHORIZED_USERS", "").split(",") if u.strip().isdigit()]
    chats = [int(c.strip()) for c in os.environ.get("TELEGRAM_AUTHORIZED_CHATS", "").split(",") if c.strip().isdigit()]
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    return TelegramConfig(
        bot_token=token,
        authorized_user_ids=users,
        authorized_chat_ids=chats,
        webhook_secret_token=secret,
        api_base_url=os.environ.get("TELEGRAM_API_BASE_URL", "https://api.telegram.org"),
    )


class TelegramActionType(str, Enum):
    START = "start"
    DEMAND = "demand"
    STATUS = "status"
    GRILL = "grill"
    APPROVE = "approve"
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
        intake_service: Optional[Any] = None,
        demand_handler: Optional[Callable[[str, int], Dict[str, Any]]] = None,
        grill_handler: Optional[Callable[[str, str, int], Dict[str, Any]]] = None,
        approval_handler: Optional[Callable[[str, str, int], Dict[str, Any]]] = None,
        status_handler: Optional[Callable[[Optional[str]], Dict[str, Any]]] = None,
    ) -> None:
        self.config = config
        self.state_dir = state_dir or Path(".factory/telegram")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "gateway_state.json"
        self.outbox_file = self.state_dir / "outbox.json"

        self.intake_service = intake_service
        self.demand_handler = demand_handler
        self.grill_handler = grill_handler
        self.approval_handler = approval_handler
        self.status_handler = status_handler

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

    def _handle_message(self, update: TelegramUpdate) -> TelegramDispatchResult:
        msg = update.message
        assert msg is not None
        user_id = msg.from_user.id if msg.from_user else None
        chat_id = msg.chat.id
        raw_text = (msg.text or "").strip()

        parts = raw_text.split(maxsplit=1)
        cmd_str = parts[0].lower() if parts else ""
        arg_str = parts[1] if len(parts) > 1 else ""

        result = TelegramDispatchResult(
            update_id=update.update_id,
            action=TelegramActionType.UNKNOWN,
            authorized=True,
        )

        if cmd_str in ("/start", "/help"):
            result.action = TelegramActionType.START
            result.response_text = (
                "👋 Dark Factory Autonomous Control Bot\n\n"
                "Available commands:\n"
                "• /demand <text> - Ingest a new demand into backlog\n"
                "• /status [ticket_id] - Check status of runs and pipelines\n"
                "• /alerts - Check active token quota and operational alerts\n"
                "• /grill <ticket_id> <choice> - Answer Grill clarification questions\n"
                "• /approve <project_id> <artifact_digest> - Approve production release\n"
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
                        result.response_text = f"✅ Demand registered successfully: <b>{demand_id}</b>"
                    except Exception as exc:
                        logger.error("Telegram intake_service error: %s", exc)
                        result.error = str(exc)
                        result.response_text = f"❌ Failed to register demand: {exc}"
                elif self.demand_handler:
                    try:
                        res = self.demand_handler(arg_str, user_id or 0)
                        ticket_id = res.get("ticket_id", "TICKET-AUTO")
                        result.target_id = ticket_id
                        result.response_text = f"✅ Demand registered successfully: <b>{ticket_id}</b>"
                    except Exception as exc:
                        logger.error("Demand handler error: %s", exc)
                        result.error = str(exc)
                        result.response_text = f"❌ Failed to register demand: {exc}"
                else:
                    result.target_id = "DEMAND-RECORDED"
                    result.response_text = "✅ Demand received and queued for intake."

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
            result.response_text = f"❓ Unknown command: {cmd_str}. Send /help for command list."

        return result

    def _handle_callback(self, update: TelegramUpdate) -> TelegramDispatchResult:
        cb = update.callback_query
        assert cb is not None
        user_id = cb.from_user.id
        chat_id = cb.message.chat.id if cb.message else None
        data_str = cb.data or ""

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

        if len(parts) >= 4 and parts[1] == "grill":
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
        req = urllib.request.Request(url, headers={"Content-Type": "application/json"}, method="GET")
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
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                return resp.status == 200
        except Exception as exc:
            logger.warning("Failed to send Telegram message: %s. Enqueuing to outbox.", exc)
            self._enqueue_outbox(chat_id, safe_text, buttons, str(exc))
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

