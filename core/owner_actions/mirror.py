"""Mirror the production line's ``HumanRequest`` into the owner action backlog (USR-190).

``core.line.human`` already tells the owner what to do (``guide_md``, screen by screen) and resumes
the blocked job when the owner answered or a probe came back green. Mirroring makes the same
request visible in the DarkHub queue next to every other human action, with the same steps, and
closes the item when the line resumes.

Idempotent: the item id is derived from the run and the blocking stage
(``OA-HR-<run>-<stage>``), so a retry of ``request_human_help`` never creates a second card.
Everything here is best effort and must never raise into the line.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from core.owner_actions.models import (
    ActionKind,
    ActionPriority,
    ActionStatus,
    ActionStep,
    OwnerAction,
    redact,
)
from core.owner_actions.store import OwnerActionStore

logger = logging.getLogger(__name__)

_LIST_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+(.*\S)\s*$")
_FENCE = re.compile(r"^\s*```")
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

_PRIORITY_BY_KIND: dict[str, ActionPriority] = {
    "grill": ActionPriority.MEDIUM,
    "secret": ActionPriority.HIGH,
    "account": ActionPriority.HIGH,
    "repo_setting": ActionPriority.HIGH,
    "commercial_acceptance": ActionPriority.HIGH,
    "infra": ActionPriority.HIGH,
}


def mirrored_id(run_id: str, blocking_stage: str) -> str:
    slug = _UNSAFE_ID_CHARS.sub("-", f"{run_id}-{blocking_stage}").strip("-.") or "run"
    return f"OA-HR-{slug}"[:80]


def parse_guide(guide_md: str) -> tuple[str, list[ActionStep]]:
    """Split a markdown guide into ``(intro, steps)``; fenced code becomes the step's command."""

    intro: list[str] = []
    steps: list[dict[str, Any]] = []
    in_fence = False
    fence_lines: list[str] = []
    for line in (guide_md or "").splitlines():
        if _FENCE.match(line):
            if in_fence:
                if steps and fence_lines:
                    command = "\n".join(fence_lines).strip()
                    steps[-1]["command"] = f"{steps[-1]['command']}\n{command}" if steps[-1].get("command") else command
                fence_lines = []
            in_fence = not in_fence
            continue
        if in_fence:
            fence_lines.append(line)
            continue
        match = _LIST_ITEM.match(line)
        if match:
            steps.append({"text": match.group(1).replace("**", "").strip(), "command": None})
        elif line.strip():
            if steps:
                steps[-1]["text"] += " " + line.strip().replace("**", "")
            else:
                intro.append(line.strip().replace("**", ""))
    if not steps and (guide_md or "").strip():
        text = "\n".join(intro).strip() or guide_md.strip()
        return "", [ActionStep(text=text)]
    return " ".join(intro).strip(), [ActionStep(text=s["text"], command=s["command"]) for s in steps]


def human_request_to_action(request: Any) -> OwnerAction:
    """Build the (open) owner action for a ``core.line.human.HumanRequest``."""

    intro, steps = parse_guide(request.guide_md)
    stage = request.blocking_stage
    why = intro or (
        f"A linha de producao parou no estagio '{stage}' do run {request.run_id} e so retoma "
        f"depois desta acao humana ({request.kind})."
    )
    verify = (
        f"Probe da linha: {request.probe_cmd}" if request.probe_cmd
        else "A linha retoma sozinha depois da sua resposta; confira o run na Esteira ao vivo."
    )
    return OwnerAction(
        id=mirrored_id(request.run_id, stage),
        kind=ActionKind.ACTION,
        title=f"Linha parada ({request.kind}): run {request.run_id} aguarda voce no estagio '{stage}'",
        priority=_PRIORITY_BY_KIND.get(request.kind, ActionPriority.HIGH),
        status=ActionStatus.OPEN,
        why=redact(why),
        steps=[ActionStep(text=redact(s.text), command=redact(s.command) if s.command else None) for s in steps],
        verify=redact(verify),
        created_at=request.created_at,
        created_by="production-line",
    )


def mirror_human_request(request: Any, *, store: Optional[OwnerActionStore] = None) -> Optional[OwnerAction]:
    """Persist the request as an owner action. Returns the action, or ``None`` on any failure."""

    try:
        action = human_request_to_action(request)
        saved, _created = (store or OwnerActionStore()).upsert(action)
        return saved
    except Exception as exc:  # noqa: BLE001 - the line must not fail because of the mirror
        logger.warning("owner_actions: espelho da HumanRequest %s falhou: %s", getattr(request, "run_id", "?"), exc)
        return None


def close_mirrored_request(run_id: str, blocking_stage: str, *, store: Optional[OwnerActionStore] = None) -> bool:
    """Mark the mirrored item done once the line resumed. Best effort; ``False`` when nothing changed."""

    try:
        active = store or OwnerActionStore()
        action = active.get(mirrored_id(run_id, blocking_stage))
        if action is None or action.status is ActionStatus.DONE:
            return False
        active.resolve(action.id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("owner_actions: nao foi possivel fechar o espelho de %s/%s: %s", run_id, blocking_stage, exc)
        return False


__all__ = [
    "close_mirrored_request",
    "human_request_to_action",
    "mirror_human_request",
    "mirrored_id",
    "parse_guide",
]
