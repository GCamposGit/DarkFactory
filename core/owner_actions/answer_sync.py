"""Persist decisions answered in the DarkHub into the repository ledger (USR-195).

The Hub keeps an owner's answer in a durable overlay, but the ``owner-decision:OA-NNN=X`` tag and the
paragraph on the blocked ticket land in the container's ephemeral ``demands.json``. This module runs on
the Notebook: it reads the answered decisions from the Hub (``GET /api/owner-actions/answers``, guarded
by the telemetry key), applies ``resolve`` + ``annotate_blocked_tickets`` inside a throwaway worktree of
``origin/main`` and lands the ledger-only change through an autonomous ``queue`` delivery.

Idempotent: a decision already recorded in the repository whose open blocked tickets already carry the
tag generates no worktree and no PR. Never raises (the caller is a long-running sync loop).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from core.owner_actions.models import ActionKind, ActionStatus, OwnerAction, redact
from core.owner_actions.propagate import decision_tag
from core.owner_actions.store import OwnerActionError, OwnerActionStore

logger = logging.getLogger(__name__)

OWNER_ACTIONS_RELATIVE = Path(".factory") / "owner_actions" / "owner_actions.json"
DEMANDS_RELATIVE = Path(".factory") / "demands" / "demands.json"
FETCH_TIMEOUT_S = 15.0

Fetcher = Callable[[], list[dict[str, Any]]]
Deliver = Callable[[str, str, Path], Any]


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


def pending_answers(root: Path, answers: list[OwnerAction]) -> list[OwnerAction]:
    """Answers whose decision is not done in ``root``'s ledger or whose tickets lack the tag."""
    from core.demands.store import DemandsStore

    store = OwnerActionStore(root / OWNER_ACTIONS_RELATIVE, remote_ref=None)
    demands = DemandsStore(root / DEMANDS_RELATIVE)
    pending: list[OwnerAction] = []
    for answer in answers:
        current = store.get(answer.id)
        if current is None:
            continue  # unknown in the repo (e.g. dynamic/machine item): nothing to persist
        if current.status is not ActionStatus.DONE or _missing_tag(answer, demands):
            pending.append(answer)
    return pending


def apply_answers(root: Path, answers: list[OwnerAction]) -> AnswerSyncResult:
    """Write the answers into ``root``'s ledger files (no git). Idempotent."""
    from core.demands.store import DemandsStore
    from core.owner_actions.propagate import annotate_blocked_tickets

    result = AnswerSyncResult(fetched=len(answers))
    store = OwnerActionStore(root / OWNER_ACTIONS_RELATIVE, remote_ref=None)
    demands = DemandsStore(root / DEMANDS_RELATIVE)
    for answer in answers:
        if answer.answer is None:
            continue
        current = store.get(answer.id)
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


def _default_deliver(label: str, title: str, workspace_path: Path, main_root: Path) -> Any:
    from core.git.autonomy import GitAutonomyManager

    return GitAutonomyManager(main_root).deliver_branch(label, title, cwd=workspace_path, kind="queue")


def branch_name(action_id: str) -> str:
    """Deterministic queue branch of one decision (matches what ``create_workspace`` derives)."""
    from core.git.ticket_workspace import _slug

    return f"ticket/{_slug(f'oa-answer-{action_id}')}"


def _default_in_flight(repo_root: Path, branch: str) -> bool:
    """True when ``origin`` already has the branch or a PR is open for it. Fail-closed on doubt."""
    import subprocess

    try:
        ls = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            cwd=repo_root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if ls.returncode != 0 or ls.stdout.strip():
        return True
    try:
        pr = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number"],
            cwd=repo_root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        return pr.returncode == 0 and pr.stdout.strip() not in ("", "[]")
    except (OSError, subprocess.TimeoutExpired):
        return False  # gh unavailable: the branch check above already covers an open PR


def sync_answers_to_repo(
    repo_root: Path,
    fetch: Fetcher,
    *,
    deliver: Optional[Deliver] = None,
    in_flight: Optional[Callable[[str], bool]] = None,
) -> AnswerSyncResult:
    """Fetch answered decisions from the Hub and land them in the repository ledger. Never raises.

    One deterministic branch (``ticket/oa-answer-<oa-id>``) and one delivery per decision. While that
    branch exists on ``origin`` (CI of the queue PR still running) the decision is skipped, so a
    sync cycle shorter than the CI never opens a second PR for it.
    """
    result = AnswerSyncResult()
    try:
        answers = _parse(fetch())
        result.fetched = len(answers)
        pending = pending_answers(repo_root, answers)
        result.pending = [a.id for a in pending]
        check = in_flight or (lambda branch: _default_in_flight(repo_root, branch))
        errors: list[str] = []
        for answer in pending:
            try:
                if check(branch_name(answer.id)):
                    continue
                _deliver_one(repo_root, answer, answers, result, errors, deliver)
            except Exception as exc:  # noqa: BLE001 - one decision must not block the others
                errors.append(f"{answer.id}: {exc.__class__.__name__}: {exc}")
        if errors:
            result.error = redact("; ".join(errors))
            logger.warning("owner_actions: sincronizacao de respostas adiada (%s)", result.error)
        return result
    except Exception as exc:  # noqa: BLE001 - the sync loop must keep running
        result.error = redact(f"{exc.__class__.__name__}: {exc}")
        logger.warning("owner_actions: sincronizacao de respostas adiada (%s)", result.error)
        return result


def _deliver_one(
    repo_root: Path,
    answer: OwnerAction,
    answers: list[OwnerAction],
    result: AnswerSyncResult,
    errors: list[str],
    deliver: Optional[Deliver],
) -> None:
    from core.git.ticket_workspace import cleanup, create_workspace

    label = f"oa-answer-{answer.id}"
    workspace = create_workspace(label, cwd=repo_root)
    if workspace.branch != branch_name(answer.id):
        cleanup(workspace.path, workspace.main_root)
        raise RuntimeError(f"branch {branch_name(answer.id)} ja existe localmente")
    landed = False
    try:
        # Re-evaluate against a fresh origin/main: the shared checkout may be stale.
        if not pending_answers(workspace.path, [answer]):
            return
        applied = apply_answers(workspace.path, [answer])
        result.applied.extend(applied.applied)
        result.annotated.update(applied.annotated)
        title = f"chore(owner-actions): registrar decisao do owner {answer.id} [USR-195]"
        send = deliver or (lambda l, t, p: _default_deliver(l, t, p, workspace.main_root))
        report = send(label, title, workspace.path)
        landed = True
        result.delivered = bool(getattr(report, "ok", False))
        if not result.delivered:
            errors.append(f"{answer.id}: entrega falhou: {getattr(report, 'message', '')}")
    finally:
        if not landed:
            cleanup(workspace.path, workspace.main_root)


__all__ = ["AnswerSyncResult", "apply_answers", "branch_name", "make_http_fetcher", "pending_answers", "sync_answers_to_repo"]
