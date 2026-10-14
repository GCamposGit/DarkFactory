"""Supervision of queue-registration PRs (``chore(backlog)``, label ``queue``) (USR-238).

A queue PR only adds one ticket row to ``.factory/demands/demands.json``. The delivery code
(:mod:`core.git.autonomy`) keeps it mergeable by re-applying the ticket onto the newest
``origin/main``; when it still cannot merge, the PR used to stay open in silence and the ticket
never reached the backlog on main (PRs 225, 245 and 276; see ``docs/runbooks/queue_registration.md``).

This module makes that failure loud, through the existing owner-action backlog
(``core.owner_actions``, shown in the DarkHub queue):

* :func:`alert_queue_pr_stuck` -- the delivery gave up on an open PR: raise ``OA-QR-<pr>`` now.
* :func:`check_stalled_queue_prs` -- watchdog: any open ``queue`` PR older than
  :data:`STALL_THRESHOLD_S` (30 min) gets the same alert, whatever killed the process that opened
  it; alerts of PRs that are no longer open are resolved.

Everything is best effort and idempotent (one item per PR) and never raises into the delivery.

Writing the alert (USR-246): both functions run from whatever checkout the caller's own delivery
is using (the shared checkout, or a ticket's throwaway worktree) -- never a location it would be
safe to write into directly, because nobody commits there on this module's behalf. Without an
explicit ``store`` (the test-only direct-write path) the mutation instead happens inside its own
disposable worktree off ``origin/main`` and is landed on main as a small autonomous ``queue`` PR
(:func:`_commit_owner_action_changes`), exactly like a ticket's own registration PR. The caller's
working tree is never touched, so raising or resolving an alert can never be the reason the next
``run_ticket`` refuses with ``SharedCheckoutDirty``, and the change reaches main so the Hub (which
reads the committed file) shows it too.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import subprocess
from typing import TYPE_CHECKING, Any, Callable, Optional, Sequence, TypeVar

from core.owner_actions.models import ActionKind, ActionPriority, ActionStatus, ActionStep, OwnerAction, redact
from core.owner_actions.store import OwnerActionStore

if TYPE_CHECKING:
    from core.git.autonomy import GitAutonomyManager

logger = logging.getLogger("darkfac.git.queue_registration")

STALL_THRESHOLD_S = 1800.0
ALERT_PREFIX = "OA-QR-"
RUNBOOK = "docs/runbooks/queue_registration.md"

GhRunner = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[str]"]
ManagerFactory = Callable[[Path], "GitAutonomyManager"]

T = TypeVar("T")

# Repository-relative path of the owner-action backlog (kept in sync with
# ``core.owner_actions.store.DEFAULT_PATH`` by ``tests/test_queue_registration.py``).
_OWNER_ACTIONS_RELATIVE = ".factory/owner_actions/owner_actions.json"

_PR_NUMBER = re.compile(r"/pull/(\d+)")
_TICKET_IN_TITLE = re.compile(r"registrar\s+(\S+)\s+-", re.IGNORECASE)
_PR_FIELDS = "number,url,title,createdAt,headRefName,mergeable,mergeStateStatus"


def alert_id(number: int) -> str:
    return f"{ALERT_PREFIX}{number}"


def stall_reason(pr: dict[str, Any]) -> str:
    """Human-readable reason derived from the PR's GitHub merge state."""
    mergeable = str(pr.get("mergeable") or "UNKNOWN").upper()
    state = str(pr.get("mergeStateStatus") or "UNKNOWN").upper()
    if mergeable == "CONFLICTING" or state == "DIRTY":
        return (
            "o PR conflita com a main (outro registro alterou .factory/demands/demands.json "
            f"depois do ultimo push); mergeable={mergeable}, mergeStateStatus={state}"
        )
    if state == "BLOCKED":
        return f"a politica da main bloqueia o merge (checks obrigatorios pendentes ou vermelhos); mergeable={mergeable}"
    return f"o PR nao foi mergeado; mergeable={mergeable}, mergeStateStatus={state}"


def build_alert(
    number: int, pr_url: str, ticket_id: str, reason: str, *, age_s: Optional[float] = None
) -> OwnerAction:
    """The (open) owner action for a queue PR that did not merge."""
    age = f" ha {age_s / 60:.0f} min" if age_s is not None else ""
    ticket = ticket_id or "ticket"
    return OwnerAction(
        id=alert_id(number),
        kind=ActionKind.ACTION,
        title=f"PR de registro #{number} ({ticket}) aberto{age} sem mergear: o ticket nao chegou ao backlog da main",
        priority=ActionPriority.HIGH,
        status=ActionStatus.OPEN,
        why=redact(f"Motivo: {reason.strip() or 'nao informado'}. PR: {pr_url}"),
        blocks=[ticket_id] if re.match(r"^[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9._-]+$", ticket_id or "") else [],
        steps=[
            ActionStep(text=f"Veja o estado e os checks do PR #{number}.",
                       command=f"gh pr view {number} --json mergeable,mergeStateStatus,statusCheckRollup"),
            ActionStep(
                text=(
                    f"Se o PR esta CONFLICTING ou atrasado, peca a um agente (Claude Code) o resgate descrito em {RUNBOOK}: "
                    "re-aplicar a linha do ticket sobre a origin/main atual, esperar o CI e mergear (squash). "
                    "Nao e preciso editar JSON a mao."
                ),
            ),
            ActionStep(text="Se o ticket ja entrou na main por outro caminho, feche o PR duplicado.",
                       command=f"gh pr close {number} --delete-branch"),
        ],
        verify=f"O PR #{number} aparece como MERGED e o ticket {ticket} esta em .factory/demands/demands.json na main.",
        created_by="queue-registration",
    )


def _commit_owner_action_changes(
    cwd: Path,
    *,
    label: str,
    title: str,
    mutate: Callable[[OwnerActionStore], T],
    gh_runner: Optional[GhRunner] = None,
    manager_factory: Optional[ManagerFactory] = None,
) -> Optional[T]:
    """Apply ``mutate`` to the owner-action ledger inside its own throwaway worktree (USR-246).

    ``cwd`` is only used to locate the repository (any worktree of it will do, via
    :func:`core.git.ticket_workspace.create_workspace`'s own ``git worktree list`` lookup); it is
    never written to. When ``mutate`` actually changed the ledger, the commit is landed on main as
    its own small autonomous ``queue`` PR (reusing :meth:`core.git.autonomy.GitAutonomyManager.deliver_branch`,
    the same path a ticket's own registration PR takes) and the disposable worktree is cleaned up by
    that delivery; a no-op mutation (idempotent re-raise/re-resolve) just discards the worktree.
    Returns ``mutate``'s result, or ``None`` if the worktree could not even be created. Never raises.

    ``manager_factory`` builds the manager used for that landing delivery (default: a plain
    ``GitAutonomyManager(root)``). The caller's own ``deliver_branch`` passes its ``_child_manager``
    bound method so the nested delivery shares its CI timeout/poll and, critically, its injected
    clock/sleep functions -- without that, a test driving the outer delivery with a fake clock would
    have this inner one fall back to real ``time.sleep`` (USR-246).
    """
    from core.git.autonomy import GitAutonomyManager
    from core.git.ticket_workspace import WorkspaceError, cleanup, create_workspace

    try:
        workspace = create_workspace(label, cwd=cwd, unique_branch=True)
    except WorkspaceError as exc:
        logger.warning("owner_actions: could not open an isolated worktree for %s: %s", label, exc)
        return None
    store = OwnerActionStore(workspace.path / _OWNER_ACTIONS_RELATIVE)
    try:
        result = mutate(store)
    except Exception as exc:  # noqa: BLE001 - supervision must never break the delivery
        logger.warning("owner_actions: mutation failed for %s: %s", label, exc)
        cleanup(workspace.path, workspace.main_root)
        return None
    mgr = (manager_factory or GitAutonomyManager)(workspace.main_root)
    if not mgr.is_dirty(cwd=workspace.path):
        cleanup(workspace.path, workspace.main_root)
        return result
    # supervise_queue=False: this delivery IS the stuck-PR alert/resolution commit itself, so it
    # must never re-trigger the watchdog that would recurse right back into this function.
    report = mgr.deliver_branch(
        label, title, cwd=workspace.path, gh_runner=gh_runner, kind="queue", supervise_queue=False
    )
    if not report.ok:
        logger.warning("owner_actions: could not land %s on main (%s): %s", label, report.action, report.message)
    return result


def raise_alert(
    number: int, pr_url: str, ticket_id: str, reason: str, *,
    age_s: Optional[float] = None,
    store: Optional[OwnerActionStore] = None,
    cwd: Optional[Path] = None,
    gh_runner: Optional[GhRunner] = None,
    manager_factory: Optional[ManagerFactory] = None,
) -> Optional[OwnerAction]:
    """Idempotent upsert of the alert. ``None`` on any failure (never raises).

    With an explicit ``store`` (tests, or a batch caller like :func:`check_stalled_queue_prs` that
    already opened its own isolated worktree) the write goes straight there. Otherwise (production)
    ``cwd`` must be given and the upsert is committed through :func:`_commit_owner_action_changes`,
    so the caller's own checkout is never the one left dirty (USR-246).
    """
    action = build_alert(number, pr_url, ticket_id, reason, age_s=age_s)

    def _upsert(active: OwnerActionStore) -> Optional[OwnerAction]:
        saved, created = active.upsert(action)
        if created:
            logger.warning("Queue PR #%s (%s) stuck: owner action %s raised (%s)", number, ticket_id, saved.id, reason)
        return saved

    try:
        if store is not None:
            return _upsert(store)
        if cwd is None:
            logger.warning("Could not raise the owner action for queue PR #%s: no cwd for the isolated worktree", number)
            return None
        return _commit_owner_action_changes(
            cwd, label=f"oa-{alert_id(number)}", title=action.title, mutate=_upsert, gh_runner=gh_runner,
            manager_factory=manager_factory,
        )
    except Exception as exc:  # noqa: BLE001 - supervision must never break the delivery
        logger.warning("Could not raise the owner action for queue PR #%s: %s", number, exc)
        return None


def alert_queue_pr_stuck(
    ticket_id: str, pr_url: str, reason: str, *,
    store: Optional[OwnerActionStore] = None,
    cwd: Optional[Path] = None,
    gh_runner: Optional[GhRunner] = None,
    manager_factory: Optional[ManagerFactory] = None,
) -> Optional[OwnerAction]:
    """The delivery gave up on an open queue PR: raise the alert straight away."""
    match = _PR_NUMBER.search(pr_url or "")
    if not match:
        return None
    return raise_alert(
        int(match.group(1)), pr_url, ticket_id, reason, store=store, cwd=cwd, gh_runner=gh_runner,
        manager_factory=manager_factory,
    )


def _age_s(created_at: str, now: datetime) -> Optional[float]:
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (now - created).total_seconds()


def check_stalled_queue_prs(
    cwd: Path,
    runner: GhRunner,
    *,
    now: Optional[datetime] = None,
    threshold_s: float = STALL_THRESHOLD_S,
    store: Optional[OwnerActionStore] = None,
    manager_factory: Optional[ManagerFactory] = None,
) -> list[OwnerAction]:
    """Alert on open ``queue`` PRs older than ``threshold_s`` and close alerts of PRs no longer open.

    Returns the alerts raised or refreshed. Does nothing (and resolves nothing) when ``gh`` fails.
    Without an explicit ``store`` every raise/resolve this call makes is batched into a single
    isolated-worktree commit (USR-246), landed on main as one small autonomous ``queue`` PR -- never
    writing into ``cwd`` itself, whatever checkout that happens to be.
    """
    try:
        res = runner(["pr", "list", "--label", "queue", "--state", "open", "--limit", "100", "--json", _PR_FIELDS], cwd)
        if res.returncode != 0:
            return []
        rows = json.loads(res.stdout or "[]")
        if not isinstance(rows, list):
            return []
    except Exception as exc:  # noqa: BLE001 - supervision must never break the delivery
        logger.warning("Stalled queue PR check failed: %s", exc)
        return []

    moment = now or datetime.now(timezone.utc)

    def _scan(active: OwnerActionStore) -> list[OwnerAction]:
        raised: list[OwnerAction] = []
        open_numbers: set[int] = set()
        for row in rows:
            if not isinstance(row, dict) or "number" not in row:
                continue
            number = int(row["number"])
            open_numbers.add(number)
            age = _age_s(str(row.get("createdAt") or ""), moment)
            if age is None or age < threshold_s:
                continue
            title_match = _TICKET_IN_TITLE.search(str(row.get("title") or ""))
            alert = raise_alert(
                number, str(row.get("url") or ""), title_match.group(1) if title_match else "",
                stall_reason(row), age_s=age, store=active,
            )
            if alert is not None:
                raised.append(alert)
        for action in active.list_actions(include_done=False):
            if not action.id.startswith(ALERT_PREFIX):
                continue
            tail = action.id[len(ALERT_PREFIX):]
            if tail.isdigit() and int(tail) not in open_numbers:
                active.resolve(action.id)
        return raised

    try:
        if store is not None:
            return _scan(store)
        return _commit_owner_action_changes(
            cwd, label="oa-queue-watchdog", title="Varredura de PRs de registro presos (USR-238)",
            mutate=_scan, gh_runner=runner, manager_factory=manager_factory,
        ) or []
    except Exception as exc:  # noqa: BLE001 - supervision must never break the delivery
        logger.warning("Stalled queue PR check failed: %s", exc)
        return []


__all__ = [
    "ALERT_PREFIX",
    "STALL_THRESHOLD_S",
    "alert_id",
    "alert_queue_pr_stuck",
    "build_alert",
    "check_stalled_queue_prs",
    "raise_alert",
    "stall_reason",
]
