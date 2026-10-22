"""PR delivery for model lifecycle changes (USR-185, slice 9b).

Flow: a dedicated worktree (``core.git.ticket_workspace.create_workspace``) -> ``apply(worktree)`` performs the
registry transition there (registry, regenerated docs and ledger all land in the worktree) -> the executor delivers the
branch (default ``GitAutonomyManager.deliver_branch(kind="registry")``) -> the worktree is cleaned up.

Why ``registry``: ``queue`` only accepts ledger files and ``implementation`` completes a ticket (and titles the PR
``feat(core): ... [USR-185]``). ``registry`` accepts only the model registry, the generated docs and the model ledger,
runs the same CI gate and squash-merge, completes no ticket and titles the PR ``chore(models): ...``.
No duplicate PR: before opening, open PRs from a ``models/*`` branch or titled ``chore(models)`` are looked up
(``gh pr list``, injectable); if one exists nothing new is opened and the reason is recorded.
Everything is injectable for tests.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from core.models.registry import REPO_ROOT

logger = logging.getLogger(__name__)

MODEL_TICKET_ID = "USR-185"
TITLE_PREFIX = "chore(models): "

# (ticket_id, title, cwd, kind) -> object with ``ok``, ``action``, ``pr_url`` (DeliveryReport-like)
Executor = Callable[[str, str, Path, str], Any]
Apply = Callable[[Path], Any]
# (gh args, cwd) -> CompletedProcess-like with ``returncode`` and ``stdout``
GhLister = Callable[[Sequence[str], Path], Any]
BRANCH_PREFIX = "models/"


@dataclass(frozen=True)
class ModelPrResult:
    opened: bool
    title: str
    ticket_id: str
    kind: str
    pr_url: Optional[str] = None
    action: str = ""
    message: str = ""
    applied: Any = None


def pr_title(summary: str) -> str:
    text = summary.strip().replace("\n", " ")
    return text if text.startswith(TITLE_PREFIX) else TITLE_PREFIX + text


def default_executor(ticket_id: str, title: str, cwd: Path, kind: str) -> Any:
    from core.git.autonomy import GitAutonomyManager

    return GitAutonomyManager(root=cwd).deliver_branch(ticket_id, title, cwd=cwd, kind=kind)  # type: ignore[arg-type]


def find_open_model_pr(lister: GhLister, cwd: Path) -> Optional[dict[str, Any]]:
    """First open PR from a ``models/*`` branch or titled ``chore(models)``; ``None`` when none (or gh unreadable)."""
    res = lister(["pr", "list", "--state", "open", "--limit", "100", "--json", "number,title,headRefName,url"], cwd)
    if getattr(res, "returncode", 1) != 0:
        logger.warning("could not list open PRs for the duplicate check: %s", getattr(res, "stderr", ""))
        return None
    try:
        rows = json.loads(getattr(res, "stdout", "") or "[]")
    except ValueError:
        return None
    for row in rows if isinstance(rows, list) else []:
        head, title = str(row.get("headRefName", "")), str(row.get("title", ""))
        if head.startswith(BRANCH_PREFIX) or title.startswith("chore(models)"):
            return row
    return None


def _default_lister(args: Sequence[str], cwd: Path) -> Any:
    from core.git.autonomy import _run_gh

    return _run_gh(args, cwd)


def deliver_model_change(
    summary: "str | Callable[[], str]",
    apply: Apply,
    *,
    repo_root: Optional[Path] = None,
    ticket_id: str = MODEL_TICKET_ID,
    kind: str = "registry",
    executor: Optional[Executor] = None,
    workspace_factory: Optional[Callable[..., Any]] = None,
    cleanup_fn: Optional[Callable[..., Any]] = None,
    has_changes: Optional[Callable[[Path], bool]] = None,
    pr_lister: Optional[GhLister] = None,
) -> ModelPrResult:
    """Apply a model change in its own worktree and open/merge the ``chore(models)`` PR. Never raises on delivery."""
    from core.git import ticket_workspace as tw

    title = pr_title(summary if isinstance(summary, str) else "")
    make_ws = workspace_factory or tw.create_workspace
    clean = cleanup_fn or tw.cleanup
    run = executor or default_executor
    root = repo_root or REPO_ROOT
    lister = pr_lister or (_default_lister if executor is None else None)  # injected executors never reach real gh
    if lister is not None:
        try:
            dup = find_open_model_pr(lister, root)
        except Exception as exc:  # noqa: BLE001 - an unreadable PR list must not block the daily run
            logger.warning("duplicate PR check failed: %s", exc)
            dup = None
        if dup is not None:
            reason = f"open model PR #{dup.get('number')} already exists ({dup.get('url') or dup.get('headRefName')})"
            logger.info("model PR not opened: %s", reason)
            return ModelPrResult(False, title, ticket_id, kind, pr_url=dup.get("url"), action="duplicate_pr", message=reason)
    ws = make_ws(ticket_id, cwd=root, unique_branch=True)
    try:
        applied = apply(ws.path)
        if callable(summary):
            title = pr_title(summary())
        if has_changes is not None and not has_changes(ws.path):
            return ModelPrResult(False, title, ticket_id, kind, action="no_changes", message="nothing to deliver", applied=applied)
        report = run(ticket_id, title, ws.path, kind)
        ok = bool(getattr(report, "ok", False))
        return ModelPrResult(
            ok, title, ticket_id, kind, pr_url=getattr(report, "pr_url", None),
            action=str(getattr(report, "action", "")), message=str(getattr(report, "message", "")), applied=applied,
        )
    except Exception as exc:  # noqa: BLE001 - fail-closed result instead of crashing the daily run
        logger.exception("model PR delivery failed")
        return ModelPrResult(False, title, ticket_id, kind, action="error", message=str(exc))
    finally:
        try:
            clean(ws.path, ws.main_root)
        except Exception as exc:  # noqa: BLE001
            logger.warning("worktree cleanup failed for %s: %s", ws.path, exc)
