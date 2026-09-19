"""CRM & Lead Capture Engine with n8n Webhook Integration and Telegram Alerts (HF-21).

Governed by HYBRID_WORKFLOW_PLAN_2026-09-08 and Universal Engineering Standards.
Persists captured inbound leads in local SQLite database, forwards payloads to self-hosted n8n,
and sends real-time Telegram alerts to the product owner.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.integrations.n8n import N8nApiClient, load_n8n_config
from core.integrations.telegram import TelegramGateway, load_telegram_config
from .models import LeadCapture, LeadDeliveryResult

logger = logging.getLogger("darkfac.marketing.crm")

DEFAULT_LEADS_DB_DIR = Path(".factory/marketing")
DEFAULT_LEADS_DB_PATH = DEFAULT_LEADS_DB_DIR / "leads.db"


class LeadManager:
    """Manages lead ingestion, SQLite persistence, n8n webhook delivery, and owner alerts."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        n8n_client: Optional[N8nApiClient] = None,
        telegram_gateway: Optional[TelegramGateway] = None,
        telegram_chat_id: Optional[int] = None,
    ) -> None:
        self.db_path = db_path or DEFAULT_LEADS_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.n8n_client = n8n_client or N8nApiClient(config=load_n8n_config())
        self.telegram_gateway = telegram_gateway or TelegramGateway(config=load_telegram_config())
        self.telegram_chat_id = telegram_chat_id
        if self.telegram_chat_id is None and self.telegram_gateway.config.authorized_chat_ids:
            self.telegram_chat_id = self.telegram_gateway.config.authorized_chat_ids[0]

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create the leads table if it doesn't exist."""
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS leads (
                    lead_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    company TEXT,
                    phone TEXT,
                    utm_source TEXT,
                    utm_medium TEXT,
                    utm_campaign TEXT,
                    notes TEXT,
                    created_at TEXT NOT NULL,
                    forwarded_to_n8n INTEGER DEFAULT 0,
                    telegram_dispatched INTEGER DEFAULT 0
                )
                """
            )
            conn.commit()

    def save_lead(self, lead: LeadCapture, forwarded_n8n: bool = False, telegram_sent: bool = False) -> bool:
        """Persist a captured lead into SQLite."""
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO leads (
                        lead_id, project_id, name, email, company, phone,
                        utm_source, utm_medium, utm_campaign, notes, created_at,
                        forwarded_to_n8n, telegram_dispatched
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(lead_id) DO UPDATE SET
                        forwarded_to_n8n = excluded.forwarded_to_n8n,
                        telegram_dispatched = excluded.telegram_dispatched
                    """,
                    (
                        lead.lead_id,
                        lead.project_id,
                        lead.name,
                        lead.email,
                        lead.company,
                        lead.phone,
                        lead.utm_source,
                        lead.utm_medium,
                        lead.utm_campaign,
                        lead.notes,
                        lead.created_at,
                        1 if forwarded_n8n else 0,
                        1 if telegram_sent else 0,
                    ),
                )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("Failed to save lead %s to SQLite: %s", lead.lead_id, exc)
            return False

    def forward_to_n8n(self, lead: LeadCapture, webhook_path: str = "/webhook/lead-capture") -> bool:
        """Forward lead capture payload to n8n webhook."""
        try:
            payload: Dict[str, Any] = {
                "event": "lead_captured",
                "lead_id": lead.lead_id,
                "project_id": lead.project_id,
                "name": lead.name,
                "email": lead.email,
                "company": lead.company,
                "phone": lead.phone,
                "utm": {
                    "source": lead.utm_source,
                    "medium": lead.utm_medium,
                    "campaign": lead.utm_campaign,
                },
                "notes": lead.notes,
                "created_at": lead.created_at,
            }
            res = self.n8n_client.trigger_webhook(path_or_url=webhook_path, payload=payload)
            return bool(res.success)
        except Exception as exc:
            logger.warning("Failed forwarding lead %s to n8n: %s", lead.lead_id, exc)
            return False

    def send_telegram_alert(self, lead: LeadCapture, chat_id: Optional[int] = None) -> bool:
        """Send immediate notification to product owner via Telegram."""
        target_chat = chat_id or self.telegram_chat_id
        if not target_chat:
            logger.warning("No authorized Telegram chat ID configured for lead alerts.")
            return False

        lines = [
            f"🎯 <b>Novo Lead Capturado [{lead.project_id.upper()}]</b>",
            f"👤 <b>Nome:</b> {lead.name}",
            f"✉️ <b>Email:</b> {lead.email}",
        ]
        if lead.company:
            lines.append(f"🏢 <b>Empresa:</b> {lead.company}")
        if lead.phone:
            lines.append(f"📞 <b>Telefone:</b> {lead.phone}")
        if lead.utm_source or lead.utm_campaign:
            lines.append(f"📍 <b>Origem/Campanha:</b> {lead.utm_source or '-'} / {lead.utm_campaign or '-'}")
        if lead.notes:
            lines.append(f"📝 <b>Notas:</b> {lead.notes}")

        text = "\n".join(lines)
        return self.telegram_gateway.send_message(chat_id=target_chat, text=text)

    def capture_lead(
        self,
        lead: LeadCapture,
        forward_n8n: bool = True,
        notify_telegram: bool = True,
        n8n_webhook_path: str = "/webhook/lead-capture",
    ) -> LeadDeliveryResult:
        """Execute full lead capture pipeline: SQLite store, n8n dispatch, and Telegram alert."""
        stored = self.save_lead(lead)
        if not stored:
            return LeadDeliveryResult(
                success=False,
                lead_id=lead.lead_id,
                stored_locally=False,
                forwarded_to_n8n=False,
                telegram_dispatched=False,
                error_message="Failed to persist lead into SQLite database.",
            )

        n8n_ok = False
        if forward_n8n:
            n8n_ok = self.forward_to_n8n(lead, webhook_path=n8n_webhook_path)

        tg_ok = False
        if notify_telegram:
            tg_ok = self.send_telegram_alert(lead)

        # Update DB flags if any downstream deliveries succeeded
        if n8n_ok or tg_ok:
            self.save_lead(lead, forwarded_n8n=n8n_ok, telegram_sent=tg_ok)

        return LeadDeliveryResult(
            success=True,
            lead_id=lead.lead_id,
            stored_locally=True,
            forwarded_to_n8n=n8n_ok,
            telegram_dispatched=tg_ok,
            error_message=None,
        )

    def list_leads(self, project_id: Optional[str] = None, limit: int = 50) -> List[LeadCapture]:
        """Query captured leads from SQLite."""
        query = "SELECT * FROM leads"
        params: List[Any] = []
        if project_id:
            query += " WHERE project_id = ?"
            params.append(project_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        leads: List[LeadCapture] = []
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(query, params)
                for row in cursor.fetchall():
                    leads.append(
                        LeadCapture(
                            lead_id=row["lead_id"],
                            project_id=row["project_id"],
                            name=row["name"],
                            email=row["email"],
                            company=row["company"],
                            phone=row["phone"],
                            utm_source=row["utm_source"],
                            utm_medium=row["utm_medium"],
                            utm_campaign=row["utm_campaign"],
                            notes=row["notes"],
                            created_at=row["created_at"],
                        )
                    )
        except Exception as exc:
            logger.error("Failed to query leads: %s", exc)
        return leads

    def get_lead(self, lead_id: str) -> Optional[LeadCapture]:
        """Fetch a specific lead by ID."""
        try:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,))
                row = cursor.fetchone()
                if row:
                    return LeadCapture(
                        lead_id=row["lead_id"],
                        project_id=row["project_id"],
                        name=row["name"],
                        email=row["email"],
                        company=row["company"],
                        phone=row["phone"],
                        utm_source=row["utm_source"],
                        utm_medium=row["utm_medium"],
                        utm_campaign=row["utm_campaign"],
                        notes=row["notes"],
                        created_at=row["created_at"],
                    )
        except Exception as exc:
            logger.error("Failed to get lead %s: %s", lead_id, exc)
        return None
