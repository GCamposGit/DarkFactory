"""Reconcile the owner-action backlog with reality (USR-307).

An open owner action (OA) can outlive the thing it was raised for: the tickets it ``blocks`` get
completed through another path, or the queue-registration PR it tracks is closed without merging.
:func:`reconcile` classifies every open OA and *proposes* an outcome with an explanation:

* ``still_needed``      - something real is still waiting (keep open; stale ``blocks`` may be trimmed);
* ``obsolete_blocker``  - every ticket it blocks is completed/cancelled (only ``blocks`` is trimmed:
  whether the owner steps are still needed is a judgement call, so closing is never automatic here);
* ``orphan_pr``         - the ``OA-QR-<n>`` alert tracks a PR that was closed unmerged,
  so the alert has nothing left to say: proposed to close, with the evidence in the note.

Applying a proposal never deletes anything: the OA keeps every field and gets an appended
``history`` entry ({at, outcome, note}). Only ``safe`` proposals are applied.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from core.owner_actions.models import ActionStatus, OwnerAction
from core.owner_actions.store import OwnerActionStore

TERMINAL_TICKET_STATUSES = frozenset({"completed", "cancelled", "canceled"})
_PR_IN_ID = re.compile(r"^OA-QR-(\d+)$")
_PR_IN_TEXT = re.compile(r"/pull/(\d+)")

# number -> (state, merged) or None when unknown
PrStateLookup = Callable[[int], Optional[tuple[str, bool]]]


class Classification(str, Enum):
    STILL_NEEDED = "still_needed"
    OBSOLETE_BLOCKER = "obsolete_blocker"
    ORPHAN_PR = "orphan_pr"


@dataclass(frozen=True)
class Proposal:
    action_id: str
    classification: Classification
    outcome: str  # "keep" | "update_blocks" | "close"
    explanation: str
    new_blocks: tuple[str, ...] = ()
    dropped_blocks: tuple[str, ...] = ()
    safe: bool = False
    evidence: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.action_id,
            "classification": self.classification.value,
            "outcome": self.outcome,
            "safe": self.safe,
            "dropped_blocks": list(self.dropped_blocks),
            "new_blocks": list(self.new_blocks),
            "explanation": self.explanation,
        }


def linked_pr(action: OwnerAction) -> Optional[int]:
    match = _PR_IN_ID.match(action.id)
    if match:
        return int(match.group(1))
    if action.id.startswith("OA-QR-"):
        found = _PR_IN_TEXT.search(action.why)
        return int(found.group(1)) if found else None
    return None


def load_ticket_statuses(demands_path: Path) -> dict[str, str]:
    payload = json.loads(demands_path.read_text(encoding="utf-8-sig"))
    rows = payload if isinstance(payload, list) else payload.get("demands", [])
    return {str(r["id"]): str(r.get("status", "")).lower() for r in rows if isinstance(r, dict) and "id" in r}


def gh_pr_state(number: int, cwd: Optional[Path] = None) -> Optional[tuple[str, bool]]:
    """``(state, merged)`` of PR ``number`` through read-only ``gh pr view``; ``None`` when unknown."""
    try:
        res = subprocess.run(
            ["gh", "pr", "view", str(number), "--json", "state,mergedAt"],
            capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(cwd) if cwd else None,
        )
        if res.returncode != 0:
            return None
        data = json.loads(res.stdout or "{}")
        return str(data.get("state", "")).upper(), bool(data.get("mergedAt"))
    except Exception:  # noqa: BLE001 - evidence gathering is best effort
        return None


def _propose(action: OwnerAction, tickets: Mapping[str, str], pr_state: Optional[PrStateLookup]) -> Proposal:
    number = linked_pr(action)
    if number is not None and pr_state is not None:
        found = pr_state(number)
        if found is not None:
            state, merged = found
            if state == "CLOSED" and not merged:
                return Proposal(
                    action.id, Classification.ORPHAN_PR, "close", safe=True,
                    explanation=(f"O PR #{number} foi fechado sem merge, entao este alerta nao rastreia mais nada. "
                                 "O ticket, se ainda for necessario, e acompanhado pelo seu proprio registro."),
                    evidence={"pr": str(number), "pr_state": "CLOSED"},
                )
            if state == "MERGED" or merged:
                return Proposal(
                    action.id, Classification.OBSOLETE_BLOCKER, "close", safe=True,
                    explanation=f"O PR #{number} foi mergeado: o registro chegou a main e o alerta cumpriu o papel.",
                    evidence={"pr": str(number), "pr_state": "MERGED"},
                )
            return Proposal(
                action.id, Classification.STILL_NEEDED, "keep",
                explanation=(f"O PR #{number} segue aberto; fechar o PR duplicado/superado (ou mergear) resolve o alerta "
                             "na proxima varredura do watchdog."),
                evidence={"pr": str(number), "pr_state": state},
            )
    done = [t for t in action.blocks if tickets.get(t, "") in TERMINAL_TICKET_STATUSES]
    if not done:
        return Proposal(action.id, Classification.STILL_NEEDED, "keep",
                        explanation="Ha ticket bloqueado ainda aberto (ou sem evidencia de conclusao) ou nao ha bloqueios.",
                        new_blocks=tuple(action.blocks))
    remaining = tuple(t for t in action.blocks if t not in done)
    statuses = ", ".join(f"{t}={tickets[t]}" for t in done)
    if not remaining:
        return Proposal(
            action.id, Classification.OBSOLETE_BLOCKER, "update_blocks", safe=True,
            dropped_blocks=tuple(done), new_blocks=(),
            explanation=(f"Todos os tickets bloqueados ja terminaram ({statuses}); a lista de bloqueios sera esvaziada. "
                         "Se os passos ainda forem uma acao real do owner, a OA segue aberta; fechar exige julgamento."),
        )
    return Proposal(
        action.id, Classification.STILL_NEEDED, "update_blocks", safe=True,
        dropped_blocks=tuple(done), new_blocks=remaining,
        explanation=f"Tickets ja terminados saem dos bloqueios ({statuses}); {', '.join(remaining)} continuam bloqueados.",
    )


def reconcile(
    actions: Iterable[OwnerAction],
    tickets: Mapping[str, str],
    pr_state: Optional[PrStateLookup] = None,
) -> list[Proposal]:
    """One proposal per open (non-done) OA, in input order. Pure: nothing is written."""
    return [_propose(a, tickets, pr_state) for a in actions if a.status is not ActionStatus.DONE]


def apply_proposals(
    store: OwnerActionStore, proposals: Iterable[Proposal], *, only: Optional[Iterable[str]] = None
) -> list[Proposal]:
    """Apply the ``safe`` proposals that change something (optionally only ``only`` ids); returns those applied."""
    wanted = set(only) if only is not None else None
    applied: list[Proposal] = []
    for p in proposals:
        if not p.safe or p.outcome == "keep" or (wanted is not None and p.action_id not in wanted):
            continue
        store.reconcile_update(
            p.action_id,
            blocks=list(p.new_blocks) if p.outcome == "update_blocks" else None,
            close=p.outcome == "close",
            outcome=f"{p.classification.value}:{p.outcome}",
            note=p.explanation,
        )
        applied.append(p)
    return applied


__all__ = ["Classification", "Proposal", "apply_proposals", "gh_pr_state", "linked_pr",
           "load_ticket_statuses", "reconcile"]
