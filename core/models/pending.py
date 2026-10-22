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
# Unversioned (gitignored) Hub storage: the Grill decision never touches .factory/owner_actions/owner_actions.json.
HUB_OWNER_ACTIONS_DIR = REPO_ROOT / "hub" / "data" / "owner_actions"
BLOCKING_STAGE = "model_promotion"

Sender = Callable[[str], bool]
Mirror = Callable[[Any], Any]
# owner-action id -> chosen option id (``approve`` / ``reject``) or ``None`` while unanswered
DecisionReader = Callable[[str], Optional[str]]

APPROVE = "approve"
REJECT = "reject"


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


def decision_action_id(model_id: str) -> str:
    from core.owner_actions.mirror import mirrored_id

    return mirrored_id(f"model-promote-{model_id}", BLOCKING_STAGE)


def _default_mirror(request: Any) -> Any:
    """Mirror the Grill as an owner DECISION (approve/reject): the owner's answer is the approval channel."""
    from core.owner_actions.mirror import human_request_to_action, mirror_human_request
    from core.owner_actions.models import ActionKind, DecisionOption
    from core.owner_actions.store import OwnerActionStore
    from core.owner_actions.sync import publish_request

    if not request.run_id.startswith("model-promote-"):
        return mirror_human_request(request)
    model_id = request.run_id[len("model-promote-"):]
    base = human_request_to_action(request)
    decision = base.model_copy(update={
        "kind": ActionKind.DECISION,
        "title": f"Aprovar a promocao do modelo {model_id}? (a politica pediu sua decisao)",
        "options": [
            DecisionOption(id=APPROVE, label="Aprovar promocao", detail="Libera a promocao no proximo ciclo diario."),
            DecisionOption(id=REJECT, label="Recusar promocao", detail="Encerra a pendencia; o modelo atual segue."),
        ],
    })
    publish_request(request)  # outbox in the state root (unversioned), read by the Hub
    saved, _created = _hub_store().upsert(decision)
    return saved


def _hub_store() -> Any:
    """Store over the Hub's unversioned dynamic file plus its resolutions overlay (where the owner's answer lands)."""
    from core.owner_actions.store import OwnerActionStore

    return OwnerActionStore(
        HUB_OWNER_ACTIONS_DIR / "dynamic.json", overlay_path=HUB_OWNER_ACTIONS_DIR / "resolutions.json",
        dynamic_path=HUB_OWNER_ACTIONS_DIR / "dynamic.json", remote_ref=None,
    )


def default_decision_reader(action_id: str) -> Optional[str]:
    """Answer recorded for the owner decision ``action_id`` (repository ledger plus the Hub overlay)."""
    from core.owner_actions.store import OwnerActionStore

    action = _hub_store().get(action_id)
    if action is None:  # curated ledger (e.g. answered through the repository) plus the same overlay
        action = OwnerActionStore(overlay_path=HUB_OWNER_ACTIONS_DIR / "resolutions.json").get(action_id)
    if action is None or action.answer is None:
        return None
    return action.answer.option_id


def consume_decision(
    model_id: str, *, now: datetime, reader: Optional[DecisionReader] = None, pending_dir: Optional[Path] = None,
) -> Optional[str]:
    """``"approved"`` / ``"rejected"`` once the owner answered the pending Grill of ``model_id``; else ``None``.

    Only a pending that was actually raised can be answered. The verdict is stored in the pending file, so it stays
    terminal: approval lets the next ``promote`` skip the Grill block, rejection closes the pending (no new request
    is ever raised for it).
    """
    path = (pending_dir or PENDING_DIR) / f"{model_id}.json"
    if not path.is_file():
        return None
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if prev.get("decision") in ("approved", "rejected"):
        return str(prev["decision"])
    try:
        answer = (reader or default_decision_reader)(decision_action_id(model_id))
    except Exception as exc:  # noqa: BLE001 - an unreadable backlog just leaves the pending open
        logger.warning("could not read the Grill answer for %s: %s", model_id, exc)
        return None
    verdict = {APPROVE: "approved", REJECT: "rejected"}.get(answer or "")
    if verdict is None:
        return None
    prev.update(decision=verdict, decided_at=now.isoformat())
    path.write_text(json.dumps(prev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return verdict


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
            if prev.get("decision") in ("approved", "rejected") or prev.get("reasons") == list(reasons) and prev.get("role") == role:
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
