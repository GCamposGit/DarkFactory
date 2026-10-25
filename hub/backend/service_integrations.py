"""Hub service domain: webhooks, Telegram, n8n, HF15 acceptance and notifications."""

import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.paths import state_root
from hub.backend.webhooks import (
    CloudGatewayStatus,
    DokployDeployClient,
    DokployDeployTrigger,
    WebhookEngine,
    WebhookEventRecord,
)
from core.integrations.telegram import TelegramGateway, reply_messages
from core.integrations.n8n import N8nProbe, N8nApiClient
from core.orchestrator.release_pipeline import ReleasePipelineService
from core.acceptance.environment import HF15EnvironmentManager
from core.acceptance.observability import HF15ObservabilityTracker
from core.acceptance.rollback import HF15RollbackCoordinator


logger = logging.getLogger("darkhub.service")


class IntegrationsMixin:
    # ---------------------------------------------------------------------------
    # Cloud Gateway & Autonomous Webhooks Service Methods (USR-18 / INFRA-09 / DF-20)
    # ---------------------------------------------------------------------------

    def process_github_webhook(
        self,
        event_type: str,
        delivery_id: str,
        payload: Dict[str, Any],
        signature_header: Optional[str] = None,
        secret: Optional[str] = None,
        require_secret: bool = True,
    ) -> WebhookEventRecord:
        """Processes an incoming GitHub webhook through WebhookEngine."""
        engine = WebhookEngine(project_root=self.project_root)
        webhook_secret = secret or os.getenv("GITHUB_WEBHOOK_SECRET")
        record = engine.process_webhook(
            event_type=event_type,
            delivery_id=delivery_id,
            payload=payload,
            signature_header=signature_header,
            secret=webhook_secret,
            require_secret=require_secret,
        )
        if record.status == "processed":
            self.handle_protected_publication_event(event_type, payload)
        return record

    def get_cloud_gateway_status(self) -> CloudGatewayStatus:
        """Returns the operational status of the Cloud 24/7 Dokploy Gateway."""
        engine = WebhookEngine(project_root=self.project_root)
        return engine.get_gateway_status()

    def get_webhook_events(
        self,
        limit: int = 50,
        event_type: Optional[str] = None,
    ) -> List[WebhookEventRecord]:
        """Returns recent audited webhook events."""
        engine = WebhookEngine(project_root=self.project_root)
        return engine.audit_store.get_events(limit=limit, event_type=event_type)

    def trigger_dokploy_deployment(
        self,
        service_name: str = "darkhub",
        custom_url: Optional[str] = None,
    ) -> DokployDeployTrigger:
        """Triggers Dokploy PaaS auto-deploy webhook (INFRA-09)."""
        client = DokployDeployClient()
        return client.trigger_deploy(service_name=service_name, custom_url=custom_url)

    def telegram_status_summary(self) -> str:
        """Short, real factory status for the Telegram ``/status`` command (USR-197).

        Every block reads cheap, already-cached data and fails in isolation (see ``telegram_status``).
        """
        from core.integrations.telegram_status import build_status_message, read_convergence_snapshot

        def _owner_actions() -> list[dict[str, Any]]:
            report = self.get_owner_actions()
            return [
                {
                    "id": item.action.get("id"),
                    "title": item.action.get("title"),
                    "priority": item.action.get("priority"),
                }
                for item in report.items
            ]

        def _runs() -> list[dict[str, Any]]:
            snapshot = self.get_line_live()
            rows: list[dict[str, Any]] = []
            for run in snapshot.runs:
                if run.state not in ("running", "queued", "attention"):
                    continue
                waiting = run.current_stage_status == "waiting_human"
                label = snapshot.stage_labels.get(run.current_stage or "", run.current_stage or "-")
                rows.append(
                    {
                        "ticket_id": run.ticket_id or run.demand_id,
                        "title": run.title,
                        "stage": label,
                        "state": run.state,
                        "waiting_human": waiting,
                    }
                )
            return rows

        return build_status_message(
            owner_actions=_owner_actions,
            tickets=lambda: self.list_demand_tickets("darkfac"),
            runs=_runs,
            convergence=lambda: read_convergence_snapshot(state_root()),
        )

    def _build_telegram_gateway(self, role: str = "ops") -> TelegramGateway:
        """Constructs TelegramGateway with bound handlers to DarkHub core services.

        `role="ops"` is the Orchestrator bot (@darkfac_ops_bot): demands, /linha, grill and every
        inline-button callback. `role="owner"` is the Owner bot (@darkfac_bot): alerts only, so it
        gets no handlers and the gateway answers commands with a pointer to the ops bot.
        """
        from core.integrations.telegram import load_telegram_config

        is_custom_data_dir = self.data_dir != Path(__file__).resolve().parent.parent / "data"
        if is_custom_data_dir:
            telegram_state_dir = self.data_dir / "telegram"
        elif self.project_root.resolve() != Path(__file__).resolve().parents[2]:
            telegram_state_dir = self.project_root / ".factory" / "telegram"
        else:
            telegram_state_dir = state_root() / "telegram"

        if role == "owner":
            users_raw = os.environ.get("TELEGRAM_AUTHORIZED_USERS") or os.environ.get("TELEGRAM_ALLOWED_USERS", "")
            chats_raw = os.environ.get("TELEGRAM_AUTHORIZED_CHATS") or os.environ.get("TELEGRAM_ALLOWED_CHATS", "")
            owner_updates: Dict[str, Any] = {}
            users = [int(u.strip()) for u in users_raw.split(",") if u.strip().isdigit()]
            chats = [int(c.strip()) for c in chats_raw.split(",") if c.strip().isdigit()]
            if users:
                owner_updates["authorized_user_ids"] = users
            if chats:
                owner_updates["authorized_chat_ids"] = chats
            secret_owner = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
            if secret_owner:
                owner_updates["webhook_secret_token"] = secret_owner
            owner_config = load_telegram_config(role="owner", config_dir=self.project_root / ".factory" / "telegram")
            if owner_updates:
                owner_config = owner_config.model_copy(update=owner_updates)
            return TelegramGateway(
                config=owner_config,
                state_dir=telegram_state_dir,
                grill_text_handler=self.record_telegram_grill_reply,
            )

        token = os.environ.get("TELEGRAM_OPS_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        users_raw = os.environ.get("TELEGRAM_AUTHORIZED_USERS") or os.environ.get("TELEGRAM_ALLOWED_USERS", "")
        chats_raw = os.environ.get("TELEGRAM_AUTHORIZED_CHATS") or os.environ.get("TELEGRAM_ALLOWED_CHATS", "")
        users = [int(u.strip()) for u in users_raw.split(",") if u.strip().isdigit()]
        chats = [int(c.strip()) for c in chats_raw.split(",") if c.strip().isdigit()]
        secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")

        config = load_telegram_config(role="ops", config_dir=self.project_root / ".factory" / "telegram")
        updates: Dict[str, Any] = {}
        if token:
            updates["bot_token"] = token
        if users:
            updates["authorized_user_ids"] = users
        if chats:
            updates["authorized_chat_ids"] = chats
        if secret:
            updates["webhook_secret_token"] = secret
        if updates:
            config = config.model_copy(update=updates)

        def _handle_demand(text: str, user_id: int) -> Dict[str, Any]:
            from core.line.owner_intake import submit_owner_demand

            outcome = submit_owner_demand(
                text,
                source="telegram",
                demands_service=self.demands_service,
                title_prefix="Telegram: ",
                submit_to_line=self.submit_ticket_to_line,
            )
            response: Dict[str, Any] = {"ticket_id": outcome.ticket_id, "title": outcome.title, "status": outcome.status}
            if outcome.line_message is not None:
                response["line_message"] = outcome.line_message
            if outcome.line_run_id:
                response["line_run_id"] = outcome.line_run_id
                response["status"] = self.get_demand_ticket(outcome.ticket_id).status.value
            return response

        def _handle_project_demand(project_id: str, text: str, user_id: int) -> Dict[str, Any]:
            from core.line.owner_intake import submit_owner_demand

            outcome = submit_owner_demand(
                text,
                source="telegram",
                demands_service=self.demands_service,
                project_id=project_id,
                title_prefix="Telegram: ",
            )
            return {"ticket_id": outcome.ticket_id, "title": outcome.title, "status": outcome.status}

        def _handle_line(ticket_id: str, user_id: int) -> Dict[str, Any]:
            submission = self.submit_ticket_to_line(ticket_id)
            return submission.model_dump()

        def _handle_status(ticket_id: Optional[str]) -> Dict[str, Any]:
            if ticket_id:
                ticket = self.get_demand_ticket(ticket_id)
                if ticket:
                    return {"summary": f"Ticket {ticket.id}: status={ticket.status.value}, title={ticket.title}"}
                return {"summary": f"Ticket '{ticket_id}' não encontrado no backlog."}
            return {"summary": self.telegram_status_summary()}

        def _handle_grill(ticket_id: str, choice: str, user_id: int) -> Dict[str, Any]:
            # USR-353: record durably first; the old refine call passed a list where a dict is
            # required, failed silently and the gateway still told the owner it was recorded.
            recorded = self.record_telegram_grill_reply(ticket_id, choice, 0, user_id)
            return {"resumed": bool(recorded.get("resumed")), "ticket_id": ticket_id, **recorded}

        def _handle_approval(project_id: str, digest: str, user_id: int) -> Dict[str, Any]:
            pipeline = ReleasePipelineService(storage_path=state_root() / "releases" / "release_registry.json")
            receipt = pipeline.record_client_acceptance(
                project_id=project_id,
                artifact_digest=digest,
                client_id="telegram-owner",
                approved_by=f"telegram-user-{user_id}",
            )
            return {"receipt_id": receipt.receipt_id, "project_id": project_id, "digest": digest}

        # HF-27-08 F (review item 5): wire the production-line grill-answer
        # and commercial-acceptance callbacks to the same ControlStore the
        # rest of this service uses, so a resumed job is visible immediately.
        try:
            from core.line.human import (
                build_telegram_commercial_acceptance_handler,
                build_telegram_line_grill_handler,
            )

            # Resolved lazily against the LINE store (Postgres in production), never the Hub's
            # local SQLite: answers and wake-ups must reach the store the workers claim from.
            def line_grill_handler(run_id: str, question_id: str, index: str, user_id: int) -> Dict[str, Any]:
                return build_telegram_line_grill_handler(self._line_control_store())(
                    run_id, question_id, index, user_id
                )

            def commercial_acceptance_handler(run_id: str, user_id: int) -> Dict[str, Any]:
                return build_telegram_commercial_acceptance_handler(self._line_control_store())(run_id, user_id)
        except Exception as exc:  # pragma: no cover - defensive, must never break the bot
            logger.warning("Failed to wire production-line Telegram handlers: %s", exc)
            line_grill_handler = None
            commercial_acceptance_handler = None

        def _handle_cancel(target: str, user_id: int) -> Dict[str, Any]:
            submission = self.cancel_line_run(target, user_id=user_id)
            return submission.model_dump()

        return TelegramGateway(
            config=config,
            state_dir=telegram_state_dir,
            demand_handler=_handle_demand,
            project_demand_handler=_handle_project_demand,
            line_handler=_handle_line,
            cancel_handler=_handle_cancel,
            status_handler=_handle_status,
            grill_handler=_handle_grill,
            approval_handler=_handle_approval,
            line_grill_handler=line_grill_handler,
            commercial_acceptance_handler=commercial_acceptance_handler,
            grill_text_handler=self.record_telegram_grill_reply,
        )

    def record_telegram_grill_reply(
        self, target: str, text: str, update_id: int, user_id: int = 0
    ) -> Dict[str, Any]:
        """Persist a free-text Grill answer from Telegram (USR-353).

        Visible in ``GET /api/owner-actions/answers`` (repo sync) and annotated on the blocked ticket.
        """
        from core.owner_actions.grill_reply import record_grill_reply

        action, annotated = record_grill_reply(
            self.owner_action_store,
            self.demands_service.store,
            target=target,
            text=text,
            update_id=update_id,
            user_id=user_id,
        )
        return {"recorded": True, "action_id": action.id, "annotated_tickets": annotated, "resumed": bool(annotated)}

    def _heal_telegram_webhooks(self) -> None:
        """Re-push the webhook secret in the background (at most every 5 min) if updates arrive without it.

        Covers a failed registration at startup: Telegram retries rejected updates, and the retries
        carry the secret once `setWebhook` succeeds.
        """
        now = time.time()
        if now - getattr(self, "_last_webhook_heal_at", 0.0) < 300:
            return
        self._last_webhook_heal_at = now
        from core.integrations.telegram_webhooks import register_telegram_webhooks

        threading.Thread(target=register_telegram_webhooks, daemon=True, name="telegram-webhook-heal").start()

    def process_telegram_webhook(
        self,
        payload: Dict[str, Any],
        secret_token_header: Optional[str] = None,
        role: str = "ops",
    ) -> Dict[str, Any]:
        """Processes an incoming Telegram webhook update for the given bot role (ops or owner)."""
        from core.integrations.telegram_webhooks import effective_webhook_secret

        expected_secret = effective_webhook_secret()
        if expected_secret and not secrets.compare_digest(secret_token_header or "", expected_secret):
            self._heal_telegram_webhooks()
            return {
                "update_id": payload.get("update_id", 0),
                "action": "unauthorized",
                "authorized": False,
                "error": "Invalid X-Telegram-Bot-Api-Secret-Token",
            }
        if self._is_publication_callback(payload):
            return self.process_publication_callback(payload["callback_query"])
        gateway = self._build_telegram_gateway(role=role)
        result = gateway.process_update(payload)
        if not result.duplicate and reply_messages(result):
            msg_obj = payload.get("message") or payload.get("callback_query", {}).get("message")
            chat_id = msg_obj.get("chat", {}).get("id") if isinstance(msg_obj, dict) else None
            if not chat_id:
                chat_id = (payload.get("callback_query", {}) or {}).get("from", {}).get("id") or (payload.get("message", {}) or {}).get("from", {}).get("id")
            if chat_id:
                gateway.deliver_reply(chat_id, result)
        return result.model_dump()

    @staticmethod
    def _is_publication_callback(payload: Dict[str, Any]) -> bool:
        from core.governance.publication import is_publication_callback

        cb = payload.get("callback_query")
        return isinstance(cb, dict) and is_publication_callback(cb)

    def _publication_state(self) -> tuple[Path, Path]:
        base = state_root() / "protected_publication"
        return base / "pending.json", base / "audit.jsonl"

    def process_publication_callback(self, callback: Dict[str, Any]) -> Dict[str, Any]:
        """USR-343 S3: owner tap (Aprovar/Rejeitar) on a protected-file PR; signing happens here, never in agents."""
        from core.governance import publication as pub

        owner = (os.environ.get(pub.OWNER_CHAT_ENV) or "").strip()
        pending, audit = self._publication_state()
        try:
            result = pub.handle_callback(
                callback,
                owner_chat_id=int(owner) if owner.lstrip("-").isdigit() else None,
                port=pub.RestGitHubPort(),
                key_loader=pub.load_signing_key,
                pending_file=pending,
                audit_log=audit,
            )
            if result.get("ok") and result.get("decision") == "approved":
                pub.sweep_pending(pub.RestGitHubPort(), pending, audit)
        except pub.PublicationError as exc:
            result = {"ok": False, "reason": str(exc)}
        shown = {k: v for k, v in result.items() if k != "record"}
        return {"action": "protected_publication", "authorized": bool(result.get("ok")), **shown}

    def handle_protected_publication_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Notify the owner when a PR touching protected files opens/updates; sweep pending merges on check events."""
        from core.governance import publication as pub

        owner = (os.environ.get(pub.OWNER_CHAT_ENV) or "").strip()
        _, audit = self._publication_state()
        if not owner.lstrip("-").isdigit():
            if event_type == "pull_request":
                logger.error("protected publication: %s missing/invalid; owner not notified", pub.OWNER_CHAT_ENV)
                pub._audit(audit, {"event": "notify_failed", "reason": f"{pub.OWNER_CHAT_ENV} missing or invalid"})
            return
        try:
            port = pub.RestGitHubPort()
            if event_type == "pull_request" and payload.get("action") in {"opened", "synchronize", "reopened"}:
                gateway = self._build_telegram_gateway(role="ops")
                number = int((payload.get("pull_request") or {}).get("number") or payload.get("number"))

                def _send(chat: int, text: str, buttons: list) -> None:
                    if not gateway.send_message(chat, text, buttons):
                        raise pub.PublicationError("telegram sendMessage falhou (token do bot ops/chat id?); ficou no outbox")

                pub.notify_owner(port, number, _send, int(owner))
            elif event_type in {"check_suite", "check_run", "status"}:
                pending, audit = self._publication_state()
                pub.sweep_pending(port, pending, audit)
        except Exception as exc:  # noqa: BLE001 - webhook processing must not fail because of this side channel
            logger.error("protected publication event failed: %s: %s", type(exc).__name__, exc, exc_info=True)
            try:
                pub._audit(audit, {"event": "notify_failed", "type": event_type, "reason": f"{type(exc).__name__}: {exc}"})
            except Exception:  # noqa: BLE001
                logger.error("protected publication: could not write audit entry")

    def get_telegram_gateway_status(self) -> Dict[str, Any]:
        """Returns diagnostic status of Telegram Gateway (HF-14 / USR-110)."""
        gateway = self._build_telegram_gateway()
        status = gateway.get_status()
        from core.integrations.telegram_webhooks import get_webhook_registration_status

        status["webhooks"] = get_webhook_registration_status()
        return status

    def get_n8n_status(self, target_url: Optional[str] = None) -> Dict[str, Any]:
        """Probes n8n endpoint and returns health/instance report."""
        url = target_url or os.environ.get("N8N_URL", "https://n8n.ggcampos.com")
        probe = N8nProbe()
        report = probe.probe(target_url=url)
        return report.model_dump()

    def get_n8n_workflows(self, limit: int = 50) -> Dict[str, Any]:
        """Lists active workflows from remote n8n instance."""
        client = N8nApiClient()
        res = client.list_workflows(limit=limit)
        return res.model_dump()

    def sync_n8n_workflows(self, custom_path: Optional[str] = None, activate: bool = True) -> Dict[str, Any]:
        """Synchronizes workflow definitions to remote n8n instance."""
        workflows_root = (self.project_root / ".factory" / "n8n" / "workflows").resolve()
        requested_path = Path(custom_path) if custom_path else workflows_root
        if not requested_path.is_absolute():
            requested_path = self.project_root / requested_path
        try:
            p = requested_path.resolve()
            p.relative_to(workflows_root)
        except (OSError, RuntimeError, ValueError):
            return {
                "success": False,
                "results": {},
                "error": "custom_path must be inside the n8n workflows directory",
            }

        if not p.exists():
            return {
                "success": False,
                "results": {},
                "error": "n8n workflow path does not exist",
            }

        client = N8nApiClient()
        if p.is_file():
            res = client.sync_workflow_file(p, activate=activate)
            return {"success": res.success, "results": {p.name: res.model_dump()}}
        if p.is_dir():
            results = client.sync_all_workflows(p, activate=activate)
            return {
                "success": all(r.success for r in results.values()) if results else False,
                "results": {k: v.model_dump() for k, v in results.items()},
            }
        return {"success": False, "results": {}, "error": "n8n workflow path is not a file or directory"}

    def trigger_n8n_webhook(self, path_or_url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatches an autonomous event to an n8n webhook."""
        client = N8nApiClient()
        res = client.trigger_webhook(path_or_url=path_or_url, payload=payload)
        return res.model_dump()

    def get_hf15_status(self) -> Dict[str, Any]:
        """Returns HF-15 acceptance environment status and preflight report."""
        env_mgr = HF15EnvironmentManager()
        root = env_mgr.provision_environment()
        preflight = env_mgr.run_preflights()
        tracker = HF15ObservabilityTracker(
            ledger_path=root / "telemetry" / "observability_ledger.jsonl"
        )
        metrics = tracker.get_metrics_summary()
        return {
            "environment": {
                "sandbox_root": str(root),
                "mode": preflight.environment_mode,
                "all_preflights_passed": preflight.all_passed,
                "checks": [c.model_dump() for c in preflight.checks],
            },
            "metrics": metrics.model_dump(mode="json"),
        }

    def get_hf15_metrics(self) -> Dict[str, Any]:
        """Returns HF-15 metrics summary and SLA compliance."""
        env_mgr = HF15EnvironmentManager()
        root = env_mgr.provision_environment()
        tracker = HF15ObservabilityTracker(
            ledger_path=root / "telemetry" / "observability_ledger.jsonl"
        )
        return tracker.get_metrics_summary().model_dump(mode="json")

    def trigger_hf15_rollback_drill(self, project_id: str = "proj-drill-01") -> Dict[str, Any]:
        """Triggers a verified rollback drill and returns the execution record."""
        env_mgr = HF15EnvironmentManager()
        root = env_mgr.provision_environment()
        coordinator = HF15RollbackCoordinator(backup_root=root / "backups")
        tracker = HF15ObservabilityTracker(
            ledger_path=root / "telemetry" / "observability_ledger.jsonl"
        )
        with tracker.measure_latency("G8", "rollback_drill_invoked"):
            record = coordinator.run_rollback_drill(
                project_id=project_id,
                sandbox_dir=root / "backups" / "drill_workspace",
            )
        tracker.record_event(
            scenario_id="G8",
            event_type="rollback_completed",
            duration_ms=record.rto_seconds * 1000.0,
            details={
                "rollback_id": record.rollback_id,
                "rto_seconds": record.rto_seconds,
                "rpo_seconds": record.rpo_seconds,
                "success": record.success,
            },
        )
        return record.model_dump(mode="json")

    def list_notifications(
        self,
        limit: int = 50,
        severity: Optional[str] = None,
        unread_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """Returns recent operational notifications from the store."""
        from core.notifications.models import AlertSeverity
        from core.notifications.store import NotificationStore
        store = NotificationStore()
        sev = AlertSeverity(severity) if severity else None
        events = store.list_notifications(limit=limit, severity=sev, unread_only=unread_only)
        return [e.model_dump(mode="json") for e in events]

    def acknowledge_notification(self, notification_id: str) -> bool:
        """Marks an operational notification as acknowledged."""
        from core.notifications.store import NotificationStore
        store = NotificationStore()
        return store.mark_acknowledged(notification_id)

    def check_token_quotas(self, force: bool = False) -> List[Dict[str, Any]]:
        """Inspects all connected provider accounts and emits alerts for critical limits."""
        from core.notifications.token_watcher import TokenQuotaWatcher
        watcher = TokenQuotaWatcher()
        emitted = watcher.check_all_quotas(force=force)
        return [e.model_dump(mode="json") for e in emitted]
