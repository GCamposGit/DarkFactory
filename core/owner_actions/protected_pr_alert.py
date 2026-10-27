"""USR-395: PR protegido sem aviso por toque vira owner action (OA) com a assinatura manual.

O caminho principal e o Telegram com botoes (``core.governance.publication``). Quando o envio nao e
possivel (token/chat ausente, GitHub inacessivel, webhook que nao chega), este modulo garante que o
owner veja o pedido no DarkHub com o passo a passo da assinatura manual. Nunca levanta excecao e e
idempotente por numero de PR.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional, Sequence

from core.owner_actions.models import ActionPriority, ActionStep, OwnerActionDraft

logger = logging.getLogger(__name__)

TITLE_PREFIX = "Assinar autorizacao do PR protegido #"


def _title(pr_number: int) -> str:
    return f"{TITLE_PREFIX}{pr_number}"


def build_draft(pr_number: int, files: Sequence[str], reason: str, ticket_id: str = "") -> OwnerActionDraft:
    shown = ", ".join(files) if files else "arquivos protegidos (ver o diff do PR)"
    return OwnerActionDraft(
        title=_title(pr_number),
        priority=ActionPriority.HIGH,
        why=(
            f"O PR #{pr_number} toca {shown} e o aviso com botoes Aprovar/Rejeitar no Telegram nao foi entregue "
            f"({reason}). Sem autorizacao assinada o PR nao entra."
        ),
        blocks=[ticket_id] if ticket_id else [],
        steps=[
            ActionStep(text=f"Abra o PR #{pr_number} no GitHub e revise o diff dos arquivos protegidos."),
            ActionStep(
                text="Crie o registro de autorizacao (ticket, paths exatos, sha da cabeca do PR) conforme "
                "docs/manual/governanca.md e assine com a chave do owner.",
                command="cd C:\\dev\\DarkFac; python -m core.governance.keys sign C:\\caminho\\registro.json",
            ),
            ActionStep(text="Publique o JSON assinado em .factory/governance/authorizations/ na main."),
        ],
        verify=f"O PR #{pr_number} fica com o CI de governanca verde e e mergeado.",
        created_by="protected-pr-alert",
    )


def ensure_signing_oa(
    store: Any,
    pr_number: int,
    files: Sequence[str] = (),
    reason: str = "",
    ticket_id: str = "",
) -> Optional[str]:
    """Cria (uma vez) a OA de assinatura manual. Devolve o id da OA ou ``None`` se falhou."""
    try:
        title = _title(pr_number)
        for action in store.list_actions(include_done=False):
            if action.title == title:
                return str(action.id)
        created = store.add(build_draft(pr_number, files, reason, ticket_id))
        try:
            from core.owner_actions.notify import notify_owner_action

            notify_owner_action(created)
        except Exception:  # noqa: BLE001 - aviso best effort
            logger.warning("protected pr alert: notificacao da OA falhou")
        return str(created.id)
    except Exception as exc:  # noqa: BLE001 - side channel nunca derruba o chamador
        logger.error("protected pr alert: nao criou OA do PR #%s: %s", pr_number, type(exc).__name__)
        return None


def fetch_pr_files(pr_number: int, repo: str = "") -> Optional[list[str]]:
    """Arquivos do PR pela API publica sem token; ``None`` se nao deu para saber."""
    try:
        import httpx

        repo = repo or os.environ.get("DARKFAC_GH_REPO", "GCamposGit/DarkFactory")
        res = httpx.get(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files?per_page=100",
            headers={"User-Agent": "darkfac-factory", "Accept": "application/vnd.github+json"},
            timeout=15.0,
        )
        if res.status_code != 200:
            return None
        return [str(item["filename"]) for item in res.json()]
    except Exception:  # noqa: BLE001
        return None


def alert_if_protected(
    store: Any, pr_number: int, files: Sequence[str], reason: str, ticket_id: str = ""
) -> Optional[str]:
    """Fallback do motor: PR com arquivo protegido e sem autorizacao vira OA."""
    from core.orchestrator.guard import audit_paths

    protected = sorted(audit_paths(list(files)))
    if not protected:
        return None
    return ensure_signing_oa(store, pr_number, protected, reason, ticket_id)
