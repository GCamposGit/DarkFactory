"""Real WAITING_HUMAN for model-change Grills (USR-185, slice 9b).

When ``lifecycle.promote`` is blocked by the Grill policy, the blockage becomes a pending item: a ``HumanRequest``
(kind ``grill``) sent through ``core.line.human.notify_human_request`` (Telegram owner bot) and mirrored into the
DarkHub owner-action queue, carrying a ready recommendation and a step by step guide for a non-technical owner.
The sender and the mirror are injectable (tests pass fakes); the pending file makes the request idempotent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from core.models.registry import REPO_ROOT

logger = logging.getLogger(__name__)

PENDING_DIR = REPO_ROOT / ".factory" / "models" / "pending_grill"
BLOCKING_STAGE = "model_promotion"

Sender = Callable[[str], bool]
Mirror = Callable[[Any], Any]


@dataclass(frozen=True)
class GrillPending:
    model_id: str
    role: Optional[str]
    reasons: tuple[str, ...]
    recommendation: str
    guide_md: str
    run_id: str
    notified: bool
    created: bool  # False when the same pending already existed (idempotent re-run)


def recommend(reasons: Sequence[str]) -> str:
    """One ready recommendation derived from the Grill reasons (conservative: keep the current model)."""
    low = " ".join(reasons).lower()
    if "proibido" in low or "frontier" in low or "forbidden" in low:
        return "NAO PROMOVER: modelo de fronteira/proibido para uso autonomo. Manter o modelo atual."
    if "preco" in low or "price" in low:
        return "NAO PROMOVER por enquanto: o preco passa do teto. Aprove so se aceitar o custo maior."
    if "familia" in low or "family" in low:
        return "NAO PROMOVER: trocaria a familia do revisor e quebraria a revisao cruzada."
    return "REPETIR A QUALIFICACAO: a evidencia esta incompleta ou ambigua. Manter o modelo atual ate la."


def build_guide(model_id: str, role: Optional[str], reasons: Sequence[str], recommendation: str) -> str:
    bullets = "\n".join(f"- {r}" for r in reasons) or "- (sem motivo informado)"
    target = f"o papel '{role}'" if role else "nenhum papel (apenas ativar)"
    return (
        f"A fabrica quer promover o modelo '{model_id}' ({target}), mas a politica pede sua decisao.\n\n"
        f"Por que parou:\n{bullets}\n\n"
        f"Recomendacao pronta: {recommendation}\n\n"
        "Passo a passo (sem precisar saber programar):\n"
        "1. Abra o DarkHub e va em 'Acoes do owner'; procure o item deste modelo.\n"
        "2. Leia os motivos acima. Se concordar com a recomendacao, nao faca nada: a promocao "
        "continua bloqueada e o modelo atual segue em uso.\n"
        "3. Se quiser aprovar mesmo assim, avise o agente da fabrica (chat do Claude Code) com: "
        f"'aprovar promocao {model_id}'. Ele registra a decisao e libera a promocao.\n"
        "4. Para recusar de vez, diga 'recusar promocao " + model_id + "'.\n"
        "Como verificar: o item some da fila do DarkHub e o ledger de modelos registra a decisao."
    )


def _default_mirror(request: Any) -> Any:
    from core.owner_actions.mirror import mirror_human_request

    return mirror_human_request(request)


def request_grill(
    model_id: str,
    role: Optional[str],
    reasons: Sequence[str],
    *,
    now: datetime,
    send: Optional[Sender] = None,
    mirror: Optional[Mirror] = None,
    pending_dir: Optional[Path] = None,
) -> GrillPending:
    """Register the pending Grill (file + owner action + Telegram). Best effort on the channels, idempotent."""
    from core.line.human import HumanRequest, notify_human_request

    directory = pending_dir or PENDING_DIR
    run_id = f"model-promote-{model_id}"
    recommendation = recommend(reasons)
    guide = build_guide(model_id, role, reasons, recommendation)
    path = directory / f"{model_id}.json"
    if path.is_file():
        try:
            prev = json.loads(path.read_text(encoding="utf-8"))
            if prev.get("reasons") == list(reasons) and prev.get("role") == role:
                return GrillPending(model_id, role, tuple(reasons), recommendation, guide, run_id,
                                    bool(prev.get("notified")), False)
        except (OSError, ValueError):
            logger.warning("pending grill file for %s unreadable; rewriting", model_id)
    request = HumanRequest(kind="grill", run_id=run_id, blocking_stage=BLOCKING_STAGE, guide_md=guide, created_at=now)
    try:
        (mirror or _default_mirror)(request)
    except Exception as exc:  # noqa: BLE001 - never break the lifecycle because of the mirror
        logger.warning("owner action mirror failed for %s: %s", run_id, exc)
    notified = notify_human_request(request, send=send)
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"model_id": model_id, "role": role, "reasons": list(reasons), "recommendation": recommendation,
                    "run_id": run_id, "notified": notified, "created_at": now.isoformat()},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    return GrillPending(model_id, role, tuple(reasons), recommendation, guide, run_id, notified, True)
