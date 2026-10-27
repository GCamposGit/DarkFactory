"""Hub service domain: priority interventions and owner-action backlog."""

import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any

from core.roadmap.models import DeliveryStatus
from hub.backend.models import (
    OwnerActionResolveResult,
    OwnerActionsReport,
    OwnerActionView,
    PriorityInterventionItem,
    PriorityInterventionKind,
    PriorityInterventionsReport,
)
from core.integrations.telegram import TelegramGateway
from core.owner_actions.models import ActionStatus, OwnerAction, PRIORITY_RANK
from core.owner_actions.mirror import mirrored_id
from core.owner_actions.propagate import annotate_blocked_tickets
from core.owner_actions.store import blocked_by as owner_action_blocked_by, unblocks as owner_action_unblocks


logger = logging.getLogger("darkhub.service")

# USR-407: a run in one of these states can no longer be answered by the owner.
_TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled", "expired", "succeeded"})
PLANNING_ONLY_TAG = "planning-only"


def _queue_text(task: Any, *names: str) -> str:
    """First non-empty field on a dashboard row (dict or model)."""
    for name in names:
        value = task.get(name) if isinstance(task, dict) else getattr(task, name, None)
        if value:
            return str(value)
    return ""


def _terminal_run_id(task: Any) -> set[str]:
    run_id = _queue_text(task, "run_id")
    done = _queue_text(task, "run_status").lower() in _TERMINAL_RUN_STATES
    return {run_id} if run_id and done else set()


def _queue_evidence(task: Any) -> list[dict[str, str]]:
    """Raw evidence of a dashboard row as plain label/value pairs (shown by the Hub task modal)."""
    evidence = task.get("evidence") if isinstance(task, dict) else getattr(task, "evidence", None)
    out: list[dict[str, str]] = []
    for ev in evidence or []:
        label = ev.get("label") if isinstance(ev, dict) else getattr(ev, "label", "")
        value = ev.get("value") if isinstance(ev, dict) else getattr(ev, "value", "")
        out.append({"label": str(label or ""), "value": str(value or "")[:500]})
    return out[:12]


def _queue_project(task: Any) -> str:
    """Project of a dashboard row. The row itself has no project_id; the evidence does."""
    explicit = _queue_text(task, "project_id")
    if explicit:
        return explicit
    evidence = task.get("evidence") if isinstance(task, dict) else getattr(task, "evidence", None)
    for entry in evidence or []:
        label = entry.get("label") if isinstance(entry, dict) else getattr(entry, "label", "")
        if str(label) != "projeto":
            continue
        value = entry.get("value") if isinstance(entry, dict) else getattr(entry, "value", "")
        if value:
            return str(value)
    return "darkfac"


class InterventionsMixin:
    def get_priority_interventions(
        self,
        project_id: Optional[str] = None,
    ) -> PriorityInterventionsReport:
        """Aggregate all pending actions requiring human decision (Grills, G8 Deploys, WAITING_HUMAN)."""
        items: List[PriorityInterventionItem] = []
        grill_count = deploy_count = waiting_human_count = 0

        # 1. Grills pendentes
        try:
            tickets = self.demands_service.list_tickets(project_id=project_id)
            for t in tickets:
                if t.status == DeliveryStatus.COMPLETED:
                    continue
                is_grill = False
                reason = ""
                tags_lower = [tag.lower() for tag in t.tags]
                if getattr(t, "lifecycle_stage", None) and str(t.lifecycle_stage).lower() in ("grill", "intake_clarification"):
                    is_grill = True
                    reason = "Demanda em estágio formal de Grill aguardando alinhamento de escopo."
                elif "grill-pending" in tags_lower or "needs-grill" in tags_lower or "grill" in tags_lower:
                    is_grill = True
                    reason = "Demanda marcada com pendência ativa de Grill."
                elif str(t.status).lower() in ("waiting_human", "waiting-human") or "waiting-human" in tags_lower:
                    is_grill = True
                    reason = "Demanda pausada em WAITING_HUMAN para esclarecimento de requisitos com o Owner."

                if is_grill:
                    grill_count += 1
                    items.append(
                        PriorityInterventionItem(
                            id=f"grill:{t.id}",
                            kind=PriorityInterventionKind.GRILL,
                            title=f"Grill Pendente: {t.title} [{t.id}]",
                            description=t.problem_statement[:240] if t.problem_statement else reason,
                            project_id=t.project_id,
                            urgency="high",
                            created_at=t.created_at.isoformat() if hasattr(t.created_at, "isoformat") else str(t.created_at),
                            action_type="modal_grill",
                            action_target_id=t.id,
                            metadata={
                                "ticket_id": t.id,
                                "tags": t.tags,
                                "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                                "suggested_files": t.suggested_files,
                            },
                        )
                    )
        except Exception as exc:
            logger.warning("Error collecting pending grills: %s", exc)

        # 2. Tarefas bloqueadas em WAITING_HUMAN
        planning_only, terminal_runs = self._planning_only_ticket_ids(), set[str]()
        try:
            task_rep = self.get_task_dashboard()
            if hasattr(task_rep, "queue") and task_rep.queue:
                for task in task_rep.queue:
                    terminal_runs.update(_terminal_run_id(task))
                    t_status = getattr(task, "status", None) if not isinstance(task, dict) else task.get("status")
                    if str(t_status or "").upper() == "WAITING_HUMAN":
                        t_id = _queue_text(task, "task_id", "id")
                        t_ticket = _queue_text(task, "ticket_id") or t_id
                        t_run = _queue_text(task, "run_id")
                        t_title = _queue_text(task, "title")
                        t_proj = _queue_project(task)
                        t_cause = _queue_text(task, "cause_code")
                        t_stage = _queue_text(task, "stage")
                        t_diag = _queue_text(task, "diagnostic")
                        label = t_ticket or t_run or t_id
                        if (project_id and t_proj != project_id) or t_ticket in planning_only:
                            continue  # planning-only umbrella tickets have no open question
                        if any(item.action_target_id == label and item.kind == PriorityInterventionKind.GRILL for item in items):
                            continue

                        t_created = _queue_text(task, "created_at", "updated_at")
                        # USR-362: attempts of one ticket collapse to the newest one.
                        prior = next((i for i in items if i.id == f"waiting_human:{label}"), None)
                        if prior is not None:
                            if (t_created or "") < str(prior.metadata.get("queued_at") or ""):
                                continue
                            items.remove(prior)
                            waiting_human_count -= 1
                        waiting_human_count += 1
                        items.append(
                            PriorityInterventionItem(
                                id=f"waiting_human:{label}",
                                kind=PriorityInterventionKind.WAITING_HUMAN,
                                title=f"Bloqueio WAITING_HUMAN: {label} - {t_title or 'Interrupção'}",
                                description=f"Causa: {t_cause or 'Tarefa requer decisão humana ou replan para prosseguir.'}",
                                project_id=t_proj,
                                urgency="high",
                                action_type="modal_task",
                                action_target_id=label,
                                metadata={
                                    "task_id": t_id or t_ticket,
                                    "queued_at": t_created,
                                    "ticket_id": t_ticket,
                                    "run_id": t_run,
                                    "cause_code": t_cause,
                                    "stage": t_stage,
                                    "diagnostic": t_diag,
                                    "evidence": _queue_evidence(task),
                                },
                            )
                        )
        except Exception as exc:
            logger.warning("Error collecting WAITING_HUMAN tasks: %s", exc)

        # 3. Aprovações G8 de deploy Dokploy
        try:
            from core.enterprise.policy import EnterprisePolicyGuard
            guard = EnterprisePolicyGuard()
            for p_cfg in guard._configs.values():
                if project_id and p_cfg.project_id != project_id:
                    continue
                if p_cfg.enabled and p_cfg.require_owner_signoff:
                    deploy_count += 1
                    items.append(
                        PriorityInterventionItem(
                            id=f"deploy_g8:{p_cfg.project_id}",
                            kind=PriorityInterventionKind.DEPLOY_G8,
                            title=f"Aprovação de deploy em produção: {p_cfg.project_id}",
                            description="Ambiente de produção exige aprovação em 2 etapas com signoff explícito do Owner.",
                            project_id=p_cfg.project_id,
                            urgency="critical",
                            action_type="modal_deploy",
                            action_target_id=p_cfg.project_id,
                            metadata={
                                "project_id": p_cfg.project_id,
                                "tier": getattr(p_cfg, "data_residency", "standard"),
                            },
                        )
                    )
        except Exception as exc:
            logger.warning("Error collecting G8 deploy gates: %s", exc)

        # 4. Backlog de acoes humanas (.factory/owner_actions) - USR-190
        owner_action_items, owner_action_warnings = self._collect_owner_action_items(terminal_runs)
        owner_action_items.extend(self._collect_usage_sync_items())
        items.extend(owner_action_items)

        urgency_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        items.sort(
            key=lambda it: (
                urgency_rank.get(it.urgency, 4),
                bool(it.metadata.get("blocked_by")),
                it.created_at,
            )
        )

        return PriorityInterventionsReport(
            total_count=len(items),
            grill_count=grill_count,
            deploy_count=deploy_count,
            waiting_human_count=waiting_human_count,
            owner_action_count=len(owner_action_items),
            owner_action_warnings=owner_action_warnings,
            items=items,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )


    # ------------------------------------------------------------------
    # Owner action backlog (USR-190)
    # ------------------------------------------------------------------

    @staticmethod
    def _owner_action_view(
        action: OwnerAction, by_id: Dict[str, OwnerAction], everything: List[OwnerAction]
    ) -> OwnerActionView:
        return OwnerActionView(
            action=action.model_dump(mode="json"),
            blocked_by=owner_action_blocked_by(action, by_id),
            unblocks=owner_action_unblocks(action, everything),
        )

    def _planning_only_ticket_ids(self) -> set[str]:
        try:
            return {
                t.id for t in self.demands_service.list_tickets()
                if PLANNING_ONLY_TAG in [tag.lower() for tag in t.tags]
            }
        except Exception as exc:
            logger.warning("Error reading planning-only tickets: %s", exc)
            return set()

    def _collect_owner_action_items(self, terminal_runs: frozenset[str] | set[str] = frozenset()) -> Tuple[List[PriorityInterventionItem], List[str]]:
        """Open owner actions as priority-queue items. Never raises: the queue must keep working."""
        try:
            loaded = self.owner_action_store.load()
        except Exception as exc:  # pragma: no cover - the store already swallows read errors
            logger.warning("Error collecting owner actions: %s", exc)
            return [], ["backlog de acoes humanas indisponivel"]
        by_id = {a.id: a for a in loaded.actions}
        items: List[PriorityInterventionItem] = []
        for action in loaded.actions:
            if action.status == ActionStatus.DONE:
                continue
            if action.id.startswith("OA-HR-") and any(
                action.id.startswith(mirrored_id(run, "") + "-") for run in terminal_runs
            ):
                continue  # mirrored HumanRequest of a run that already ended
            view = self._owner_action_view(action, by_id, loaded.actions)
            record = view.action
            items.append(
                PriorityInterventionItem(
                    id=f"owner_action:{action.id}",
                    kind=PriorityInterventionKind.OWNER_ACTION,
                    title=f"{action.id} - {action.title}",
                    description=action.why,
                    project_id="darkfac",
                    urgency=action.priority.value,
                    created_at=record["created_at"],
                    action_type="owner_action",
                    action_target_id=action.id,
                    metadata={
                        "oa_id": action.id,
                        "action_kind": action.kind.value,
                        "status": action.status.value,
                        "why": action.why,
                        "blocks": record["blocks"],
                        "depends_on": record["depends_on"],
                        "blocked_by": view.blocked_by,
                        "unblocks": view.unblocks,
                        "steps": record["steps"],
                        "verify": action.verify,
                        "options": record.get("options", []),
                        "created_by": action.created_by,
                    },
                )
            )
        return items, list(loaded.warnings)

    def _collect_usage_sync_items(self) -> List[PriorityInterventionItem]:
        """Critical item while the usage sync loop is refused or stale (USR-235); closes by itself."""
        try:
            alert = self.usage_sync_watch.evaluate()
        except Exception as exc:
            logger.warning("Error evaluating usage sync health: %s", exc)
            return []
        if alert is None:
            return []
        oa_id = f"usage_sync:{alert.cause}"
        return [
            PriorityInterventionItem(
                id=f"owner_action:{oa_id}",
                kind=PriorityInterventionKind.OWNER_ACTION,
                title=alert.title,
                description=alert.description,
                project_id="darkfac",
                urgency="critical",
                created_at=alert.since,
                action_type="owner_action",
                action_target_id=oa_id,
                metadata={
                    "oa_id": oa_id,
                    "action_kind": "action",
                    "status": "open",
                    "why": alert.description,
                    "blocks": [],
                    "depends_on": [],
                    "blocked_by": [],
                    "unblocks": [],
                    "steps": [
                        {"text": "Confirme que DARKFAC_TELEMETRY_KEY esta configurada no Hub (Dokploy) e igual a do host.", "command": ""},
                        {"text": "No host, verifique a tarefa agendada DarkFac-Usage-Cloud-Sync.", "command": ""},
                    ],
                    "verify": "O item some sozinho quando POST /api/usage/sync voltar a responder com sucesso.",
                    "options": [],
                    "created_by": "usage_sync_watch",
                    "cause": alert.cause,
                    "last_success_at": alert.last_success_at,
                },
            )
        ]

    def get_owner_actions(self, *, include_done: bool = False) -> OwnerActionsReport:
        """The owner action backlog, with dependency fields (GET /owner-actions)."""
        loaded = self.owner_action_store.load()
        by_id = {a.id: a for a in loaded.actions}
        views = [
            self._owner_action_view(a, by_id, loaded.actions)
            for a in loaded.actions
            if include_done or a.status != ActionStatus.DONE
        ]
        views.sort(
            key=lambda v: (
                v.action["status"] == "done",
                PRIORITY_RANK.get(v.action["priority"], 4),
                bool(v.blocked_by),
                v.action["created_at"],
            )
        )
        done = sum(1 for a in loaded.actions if a.status == ActionStatus.DONE)
        return OwnerActionsReport(
            open_count=len(loaded.actions) - done,
            done_count=done,
            items=views,
            warnings=list(loaded.warnings),
        )

    def _owner_action_result(self, action: OwnerAction, annotated: List[str]) -> OwnerActionResolveResult:
        loaded = self.owner_action_store.load()
        by_id = {a.id: a for a in loaded.actions}
        return OwnerActionResolveResult(
            item=self._owner_action_view(by_id.get(action.id, action), by_id, loaded.actions),
            annotated_tickets=annotated,
        )

    def mark_owner_action_done(self, action_id: str, note: str = "") -> OwnerActionResolveResult:
        """Owner closed an `action` item in the DarkHub. Persists to the resolutions overlay."""
        action = self.owner_action_store.resolve(action_id, note=note, to_overlay=True)
        return self._owner_action_result(action, [])

    def answer_owner_decision(self, action_id: str, option_id: str, note: str = "") -> OwnerActionResolveResult:
        """Owner answered a `decision`: persist it and attach it to the tickets it blocks."""
        action = self.owner_action_store.resolve(action_id, option_id=option_id, note=note, to_overlay=True)
        annotated = annotate_blocked_tickets(action, self.demands_service.store)
        return self._owner_action_result(action, annotated)

    def notify_pending_grill(
        self,
        ticket_id: str,
        *,
        hub_base_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Dispatches an active Telegram notification to the Owner with summary and direct DarkHub link."""
        ticket = self.demands_service.get_ticket(ticket_id)
        if not ticket:
            return {"ok": False, "ticket_id": ticket_id, "sent": False, "error": f"Ticket '{ticket_id}' not found"}

        base_url = (hub_base_url or os.getenv("DARKHUB_BASE_URL") or "https://darkhub.ggcampos.com").rstrip("/")
        action_link = f"{base_url}/#grill={ticket.id}"

        summary = ticket.problem_statement[:200] if ticket.problem_statement else "Demanda cadastrada no backlog aguardando desambiguação."
        text = (
            f"🔥 <b>DarkHub: Intervenção Prioritária (Grill Pendente)</b>\n\n"
            f"<b>Demanda:</b> {ticket.title} [<code>{ticket.id}</code>]\n"
            f"<b>Projeto:</b> <code>{ticket.project_id}</code>\n"
            f"<b>Resumo:</b> {summary}\n\n"
            f"👉 <i>Clique no link abaixo para abrir o modal de Grill e submeter o alinhamento diretamente no DarkHub:</i>\n"
            f"{action_link}"
        )
        buttons = [[{"text": "⚡ Responder Grill no DarkHub", "url": action_link}]]

        from core.integrations.telegram import load_telegram_config
        sent = False
        # Broadcast across owner and ops roles to ensure reachability on whichever chat the Owner is monitoring
        for role in ("owner", "ops"):
            try:
                cfg = load_telegram_config(role=role)
                if not cfg.bot_token:
                    continue
                gw = TelegramGateway(config=cfg)
                target_chats = list(cfg.authorized_chat_ids) or list(cfg.authorized_user_ids)
                if not target_chats:
                    if gw.send_message(chat_id=1, text=text, buttons=buttons):
                        sent = True
                else:
                    for cid in target_chats:
                        if gw.send_message(chat_id=cid, text=text, buttons=buttons):
                            sent = True
            except Exception as exc:
                logger.warning("Error dispatching grill notification via role %s: %s", role, exc)

        logger.info("Dispatched Telegram grill notification for ticket %s (sent=%s)", ticket.id, sent)
        return {
            "ok": True,
            "ticket_id": ticket.id,
            "sent": sent,
            "action_link": action_link,
            "message": "Notification dispatched or enqueued to outbox.",
        }
