"""Persist decisions answered in the DarkHub into the repository ledger (USR-195).

The Hub keeps an owner's answer in a durable overlay, but the ``owner-decision:OA-NNN=X`` tag and the
paragraph on the blocked ticket land in the container's ephemeral ``demands.json``. This module runs on
the Notebook: it reads the answered decisions from the Hub (``GET /api/owner-actions/answers``, guarded
by the telemetry key) and applies ``resolve`` + ``annotate_blocked_tickets`` straight to the control
store (USR-343 S4b): no worktree, no ``queue`` PR, no push. ``owner_actions.json`` / ``demands.json`` catch
up through the daily snapshot (``scripts/snapshot_queue.py``).

Idempotent: a decision already recorded whose open blocked tickets already carry the tag changes nothing.
Never raises (the caller is a long-running sync loop).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.owner_actions.models import ActionKind, ActionStatus, OwnerAction, redact
from core.owner_actions.grill_reply import GRILL_ID_PREFIX
from core.owner_actions.propagate import decision_tag
from core.owner_actions.store import OwnerActionError, OwnerActionStore

logger = logging.getLogger(__name__)

OWNER_ACTIONS_RELATIVE = Path(".factory") / "owner_actions" / "owner_actions.json"
DEMANDS_RELATIVE = Path(".factory") / "demands" / "demands.json"
FETCH_TIMEOUT_S = 15.0

Fetcher = Callable[[], list[dict[str, Any]]]


@dataclass
class AnswerSyncResult:
    fetched: int = 0
    pending: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    annotated: dict[str, list[str]] = field(default_factory=dict)
    delivered: bool = False
    error: str = ""


def make_http_fetcher(base_url: str, telemetry_key: str, *, timeout: float = FETCH_TIMEOUT_S) -> Fetcher:
    """Fetcher for the Hub endpoint. The key only travels in a header and never in an error message."""

    def fetch() -> list[dict[str, Any]]:
        import httpx

        response = httpx.get(
            f"{base_url.rstrip('/')}/api/owner-actions/answers",
            headers={"X-DarkFac-Telemetry-Key": telemetry_key},
            timeout=timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Hub answered HTTP {response.status_code}")
        rows = response.json().get("answers", [])
        return [row for row in rows if isinstance(row, dict)]

    return fetch


def _parse(rows: list[dict[str, Any]]) -> list[OwnerAction]:
    answers: list[OwnerAction] = []
    for row in rows:
        try:
            action = OwnerAction.model_validate(row)
        except Exception:  # noqa: BLE001 - a malformed row must not block the others
            continue
        if action.kind is ActionKind.DECISION and action.answer is not None:
            answers.append(action)
    return answers


def _missing_tag(action: OwnerAction, demands_store: Any) -> list[str]:
    """Open blocked tickets that do not carry the decision tag yet."""
    from core.roadmap.models import DeliveryStatus

    tag = decision_tag(action)
    missing: list[str] = []
    for ticket_id in action.blocks:
        try:
            ticket = demands_store.get_ticket(ticket_id)
        except Exception:  # noqa: BLE001
            continue
        if ticket is None or ticket.status == DeliveryStatus.COMPLETED:
            continue
        if tag not in ticket.tags:
            missing.append(ticket_id)
    return missing


def pending_answers(root: Path, answers: list[OwnerAction], *, control: bool | None = None) -> list[OwnerAction]:
    """Answers whose decision is not done in ``root``'s ledger or whose tickets lack the tag."""
    from core.demands.store import DemandsStore

    store = OwnerActionStore(root / OWNER_ACTIONS_RELATIVE, remote_ref=None, control=control)
    demands = DemandsStore(root / DEMANDS_RELATIVE)
    pending: list[OwnerAction] = []
    for answer in answers:
        current = store.get(answer.id)
        if current is None:
            if answer.id.startswith(GRILL_ID_PREFIX):
                pending.append(answer)  # free-text Grill reply from Telegram (USR-353): persist it
            continue  # unknown in the repo (e.g. dynamic/machine item): nothing to persist
        if current.status is not ActionStatus.DONE or _missing_tag(answer, demands):
            pending.append(answer)
    return pending


def apply_answers(root: Path, answers: list[OwnerAction], *, control: bool | None = None) -> AnswerSyncResult:
    """Write the answers into ``root``'s ledger (control store for the default ledger; no VCS). Idempotent."""
    from core.demands.store import DemandsStore
    from core.owner_actions.propagate import annotate_blocked_tickets

    result = AnswerSyncResult(fetched=len(answers))
    store = OwnerActionStore(root / OWNER_ACTIONS_RELATIVE, remote_ref=None, control=control)
    demands = DemandsStore(root / DEMANDS_RELATIVE)
    for answer in answers:
        if answer.answer is None:
            continue
        current = store.get(answer.id)
        if current is None and answer.id.startswith(GRILL_ID_PREFIX):
            # Grill replies are born in the Hub's dynamic store; import them as already-answered items.
            try:
                store.upsert(answer)
                result.applied.append(answer.id)
                current = store.get(answer.id)
            except OwnerActionError as exc:
                logger.warning("owner_actions: %s nao importada: %s", answer.id, redact(str(exc)))
        if current is None:
            continue
        try:
            if current.status is not ActionStatus.DONE:
                store.resolve(
                    answer.id,
                    option_id=answer.answer.option_id,
                    note=answer.answer.note,
                    answered_by=answer.answer.answered_by,
                )
                result.applied.append(answer.id)
            elif current.answer is not None and current.answer.option_id != answer.answer.option_id:
                logger.warning("owner_actions: %s ja respondida no repositorio com outra opcao; ignorada", answer.id)
                continue
            if _missing_tag(answer, demands):
                updated = annotate_blocked_tickets(answer, demands)
                if updated:
                    result.annotated[answer.id] = updated
        except OwnerActionError as exc:
            logger.warning("owner_actions: %s nao aplicada: %s", answer.id, redact(str(exc)))
    return result


def sync_answers_to_repo(
    repo_root: Path, fetch: Fetcher, *, control: bool | None = None
) -> AnswerSyncResult:
    """Fetch answered decisions from the Hub and persist them in the ledger. Never raises."""
    result = AnswerSyncResult()
    try:
        answers = _parse(fetch())
        result.fetched = len(answers)
        pending = pending_answers(repo_root, answers, control=control)
        result.pending = [a.id for a in pending]
        if pending:
            applied = apply_answers(repo_root, pending, control=control)
            result.applied = applied.applied
            result.annotated = applied.annotated
            result.delivered = bool(applied.applied or applied.annotated)
        return result
    except Exception as exc:  # noqa: BLE001 - the sync loop must keep running
        result.error = redact(f"{exc.__class__.__name__}: {exc}")
        logger.warning("owner_actions: sincronizacao de respostas adiada (%s)", result.error)
        return result


__all__ = ["AnswerSyncResult", "apply_answers", "make_http_fetcher", "pending_answers", "sync_answers_to_repo"]
