"""Singleton owner-action alert for the base branch (main) CI going red or green (USR-247).

Before this module, a red `main` could stay silent for hours: `core.git.autonomy.run_check_main`
only files a defect ticket (idempotent per SHA, but it is a backlog item, not an alert) and/or sends
a stateless `NotificationService` message that is resent on every call with no de-dup at all ("notify
sends an alert on every invocation where the branch is red, so schedule it with a sensible cadence").
Twice (11/10's `test_line_live` time bomb, 12/10's `.pyc`-stale flake after PR #316/USR-245) nobody
noticed for hours, because nothing re-checked `main` on its own -- the only consumer of `check_main`
(`scripts/dokploy_redeploy.py`) only runs after a push whose CI already succeeded (the GitHub Actions
`continuous-deployment` job needs `main-validation` to succeed), and a merge's own PR checks say
nothing about the separate push-triggered run on `main` that follows it.

This module gives red-`main` a single, idempotent, auto-closing alert through the existing owner
action backlog (`core.owner_actions`, shown in the DarkHub and Telegram-notified for CRITICAL items):

* :func:`raise_main_ci_alert` -- the last conclusive push run of `main` is red: upsert the one
  `OA-MAIN-CI` item (a repeat red status never creates a second item) and notify only on the first
  raise.
* :func:`resolve_main_ci_alert` -- the last conclusive push run of `main` is green: close the alert
  if it is open (a no-op otherwise).
* :func:`sync_main_ci_status` -- dispatches on a `ci_checks.MainStatus` (what
  `core.git.autonomy.run_check_main(alert_owner=True)` calls on every probe).

Like `core.git.queue_registration` (USR-238/246), writing the alert from a throwaway checkout (a
run's worktree, a redeploy host) reuses `_commit_owner_action_changes` so the change lands on `main`
through its own small autonomous `queue` PR instead of leaving the caller's checkout dirty. Tests
(and any caller that already holds an `OwnerActionStore`, e.g. a batch sync) can pass `store=`
directly to bypass the git plumbing entirely.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from core.git import ci_checks
from core.git.queue_registration import GhRunner, ManagerFactory, _commit_owner_action_changes
from core.owner_actions.models import ActionKind, ActionPriority, ActionStatus, ActionStep, OwnerAction, redact
from core.owner_actions.notify import notify_owner_action
from core.owner_actions.store import OwnerActionStore

logger = logging.getLogger("darkfac.git.main_ci_alert")

MAIN_CI_ALERT_ID = "OA-MAIN-CI"

Notifier = Callable[[OwnerAction], Any]


def build_main_ci_alert(status: ci_checks.MainStatus) -> OwnerAction:
    """The (open, critical) owner action for a red `status`. Same id every time (singleton)."""
    jobs = ", ".join(name for name, _ in status.red_jobs) or status.workflow
    return OwnerAction(
        id=MAIN_CI_ALERT_ID,
        kind=ActionKind.ACTION,
        title=f"CI vermelho em {status.branch} @{status.short_sha}: {jobs}",
        priority=ActionPriority.CRITICAL,
        status=ActionStatus.OPEN,
        why=redact(
            f"O workflow '{status.workflow}' terminou em '{status.conclusion}' no ultimo push de "
            f"{status.branch} (SHA {status.sha}). Jobs vermelhos: {jobs}. Run: {status.run_url or '(sem link)'}."
        ),
        steps=[
            ActionStep(
                text="Veja o log do job vermelho.",
                command=f"gh run view {status.run_id} --log-failed" if status.run_id else None,
            ),
            ActionStep(
                text=(
                    f"Corrija a causa raiz na branch '{status.branch}' (ou reverta o commit que quebrou) "
                    "e aguarde um push verde."
                ),
            ),
        ],
        verify=f"Um push seguinte de {status.branch} termina o workflow '{status.workflow}' em sucesso.",
        created_by="main-ci-watch",
    )


def raise_main_ci_alert(
    status: ci_checks.MainStatus,
    *,
    store: Optional[OwnerActionStore] = None,
    cwd: Optional[Path] = None,
    gh_runner: Optional[GhRunner] = None,
    manager_factory: Optional[ManagerFactory] = None,
    notifier: Optional[Notifier] = None,
) -> Optional[OwnerAction]:
    """Idempotent upsert of the singleton alert; notifies only when it is newly created.

    With an explicit ``store`` the write goes straight there (tests, or a caller that already opened
    its own isolated worktree). Otherwise ``cwd`` must be given and the upsert is committed through
    ``_commit_owner_action_changes``. ``None`` on any failure; never raises.
    """
    action = build_main_ci_alert(status)

    def _upsert(active: OwnerActionStore) -> OwnerAction:
        saved, created = active.upsert(action)
        if created:
            logger.warning(
                "Main CI red (%s @%s): owner action %s raised", status.branch, status.short_sha, saved.id
            )
            try:
                if notifier is not None:
                    notifier(saved)
                else:
                    notify_owner_action(saved, force=True)
            except Exception as exc:  # noqa: BLE001 - alerting must never break the probe
                logger.warning("Main CI alert notification failed: %s", exc.__class__.__name__)
        return saved

    try:
        if store is not None:
            return _upsert(store)
        if cwd is None:
            logger.warning("Could not raise the main CI alert: no cwd for the isolated worktree")
            return None
        return _commit_owner_action_changes(
            cwd, label=f"oa-{MAIN_CI_ALERT_ID.lower()}", title=action.title, mutate=_upsert,
            gh_runner=gh_runner, manager_factory=manager_factory,
        )
    except Exception as exc:  # noqa: BLE001 - alerting must never break the probe
        logger.warning("Could not raise the main CI alert: %s", exc)
        return None


def resolve_main_ci_alert(
    *,
    store: Optional[OwnerActionStore] = None,
    cwd: Optional[Path] = None,
    gh_runner: Optional[GhRunner] = None,
    manager_factory: Optional[ManagerFactory] = None,
) -> Optional[OwnerAction]:
    """Close the singleton alert if it is open. ``None`` if it does not exist, is already done, or fails."""

    def _resolve(active: OwnerActionStore) -> Optional[OwnerAction]:
        current = active.get(MAIN_CI_ALERT_ID)
        if current is None or current.status is ActionStatus.DONE:
            return None
        resolved = active.resolve(MAIN_CI_ALERT_ID)
        logger.info("Main CI green again: owner action %s resolved", resolved.id)
        return resolved

    try:
        if store is not None:
            return _resolve(store)
        if cwd is None:
            return None
        return _commit_owner_action_changes(
            cwd, label=f"oa-{MAIN_CI_ALERT_ID.lower()}-resolve",
            title=f"Fechar {MAIN_CI_ALERT_ID}: CI voltou a ficar verde em main",
            mutate=_resolve, gh_runner=gh_runner, manager_factory=manager_factory,
        )
    except Exception as exc:  # noqa: BLE001 - alerting must never break the probe
        logger.warning("Could not resolve the main CI alert: %s", exc)
        return None


def sync_main_ci_status(
    status: ci_checks.MainStatus,
    *,
    store: Optional[OwnerActionStore] = None,
    cwd: Optional[Path] = None,
    gh_runner: Optional[GhRunner] = None,
    manager_factory: Optional[ManagerFactory] = None,
    notifier: Optional[Notifier] = None,
) -> Optional[OwnerAction]:
    """Dispatch on ``status.state``: ``red`` raises/keeps the alert, ``green`` resolves it.

    ``unknown`` (gh could not be read) is a no-op: a probe failure must never raise or resolve the
    alert on a guess.
    """
    if status.state == "red":
        return raise_main_ci_alert(
            status, store=store, cwd=cwd, gh_runner=gh_runner, manager_factory=manager_factory,
            notifier=notifier,
        )
    if status.state == "green":
        return resolve_main_ci_alert(store=store, cwd=cwd, gh_runner=gh_runner, manager_factory=manager_factory)
    return None


__all__ = [
    "MAIN_CI_ALERT_ID",
    "build_main_ci_alert",
    "raise_main_ci_alert",
    "resolve_main_ci_alert",
    "sync_main_ci_status",
]
