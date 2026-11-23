"""Sweep ownerless Git worktrees through the shared safe cleanup routine (USR-500 T3)."""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from core.common.time import utc_now
from core.git import ticket_workspace

REPO_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger("darkfac.git.orphan_worktree_cleanup")

SWEEP_INTERVAL = timedelta(days=1)
INITIAL_FAILURE_BACKOFF = timedelta(hours=1)
MAX_FAILURE_BACKOFF = timedelta(hours=24)
SCHEDULE_STATE = Path(".factory") / "runs" / "orphan_worktree_cleanup.json"

_BLOCKED_REASONS = frozenset({"dirty", "holder", "locked", "index_lock", "detached_outside_base"})
_ERROR_REASONS = frozenset(
    {
        "psutil_unavailable",
        "git_unreadable",
        "status_unreadable",
        "ticket_status_unknown",
        "age_unknown",
        "unregistered",
        "cleanup_pending_inventory_error",
    }
)
_CLEANUP_RETRY_REASONS = frozenset(
    {"dirty", "holder", "locked", "index_lock", "psutil_unavailable", "git_unreadable", "status_unreadable"}
)


@dataclass
class CleanupSummary:
    """Counts and per-worktree diagnostics from one idempotent sweep."""

    removed: int = 0
    blocked: int = 0
    ignored: int = 0
    errors: int = 0
    details: list[dict[str, str]] = field(default_factory=list)

    def record(self, category: str, path: Path | str, reason: str, detail: str = "") -> None:
        setattr(self, category, getattr(self, category) + 1)
        item = {"path": str(path), "reason": reason}
        if detail:
            item["detail"] = detail
        self.details.append(item)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["preserved"] = self.blocked + self.ignored
        payload["failures"] = self.errors
        return payload


def sweep_orphan_worktrees(
    root: Path = REPO_ROOT,
    *,
    now: Optional[datetime] = None,
    run_git: Optional[ticket_workspace.GitRunner] = None,
    holder_probe: Optional[ticket_workspace.HolderProbe] = None,
    queue_lookup: Optional[Callable[[str], Optional[str]]] = None,
    statuses: Optional[Mapping[str, str]] = None,
) -> CleanupSummary:
    """Inspect each registered worktree and send every eligible candidate to ``cleanup``.

    Ticket ownership is resolved by ``ticket_workspace`` against its canonical demands
    ledger and control-store queue unless a test supplies explicit sources. Errors for an
    individual worktree are recorded and do not stop later entries from being inspected.
    """
    summary = CleanupSummary()
    moment = now or utc_now()
    runner = run_git or ticket_workspace._git
    try:
        main_root = ticket_workspace.main_checkout_root(root, runner)
        entries = ticket_workspace.list_worktrees(main_root, runner)
    except Exception as exc:  # noqa: BLE001 - a broken repository probe is a reportable sweep error
        summary.record("errors", root, "worktree_inventory_error", f"{type(exc).__name__}: {exc}")
        return summary
    if not entries:
        summary.record("errors", main_root, "worktree_inventory_empty", "Git returned no registered worktrees")
        return summary

    try:
        pending_paths = ticket_workspace.cleanup_pending_worktree_paths(main_root)
    except Exception as exc:  # noqa: BLE001 - missing pending state must fail closed
        summary.record("errors", main_root, "cleanup_pending_inventory_error", f"{type(exc).__name__}: {exc}")
        return summary

    for index, entry in enumerate(entries):
        _process_sweep_entry(
            index,
            entry,
            summary=summary,
            main_root=main_root,
            moment=moment,
            runner=runner,
            holder_probe=holder_probe,
            queue_lookup=queue_lookup,
            statuses=statuses,
            pending_paths=pending_paths,
        )

    logger.info(
        "orphan worktree sweep: removed=%d preserved=%d failures=%d (blocked=%d ignored=%d)",
        summary.removed,
        summary.blocked + summary.ignored,
        summary.errors,
        summary.blocked,
        summary.ignored,
    )
    return summary


def _process_sweep_entry(
    index: int,
    entry: Mapping[str, str],
    *,
    summary: CleanupSummary,
    main_root: Path,
    moment: datetime,
    runner: ticket_workspace.GitRunner,
    holder_probe: Optional[ticket_workspace.HolderProbe],
    queue_lookup: Optional[Callable[[str], Optional[str]]],
    statuses: Optional[Mapping[str, str]],
    pending_paths: set[str],
) -> None:
    raw_path = entry.get("worktree", "")
    path = Path(raw_path) if raw_path else main_root
    try:
        verdict = ticket_workspace.classify_ownerless_worktree(
            path,
            main_root=main_root,
            entry=entry,
            is_main=index == 0,
            now=moment,
            run_git=runner,
            holder_probe=holder_probe,
            queue_lookup=queue_lookup,
            statuses=statuses,
            pending_paths=pending_paths,
        )
    except Exception as exc:  # noqa: BLE001 - isolate malformed worktree state
        summary.record("errors", path, "classification_error", f"{type(exc).__name__}: {exc}")
        logger.exception("could not classify worktree %s; continuing sweep", path)
        return

    should_cleanup = verdict.eligible or verdict.reason in _CLEANUP_RETRY_REASONS
    if not should_cleanup:
        category = (
            "blocked" if verdict.reason in _BLOCKED_REASONS
            else ("errors" if verdict.reason in _ERROR_REASONS else "ignored")
        )
        summary.record(category, path, verdict.reason, verdict.detail)
        if category in {"blocked", "errors"}:
            logger.warning(
                "ownerless worktree preserved path=%s reason=%s detail=%s",
                path,
                verdict.reason,
                verdict.detail,
            )
        return

    try:
        result = ticket_workspace.cleanup(
            path,
            main_root,
            run_git=runner,
            now_fn=lambda: moment,
            holder_probe=holder_probe,
        )
    except Exception as exc:  # noqa: BLE001 - cleanup normally returns a result; isolate broken adapters too
        summary.record("errors", path, "cleanup_exception", f"{type(exc).__name__}: {exc}")
        logger.exception("cleanup raised for worktree %s; continuing sweep", path)
        return

    if result.removed:
        summary.record("removed", path, result.outcome, result.detail)
    elif result.outcome in {"blocked_changes", "blocked_process"}:
        summary.record("blocked", path, result.reason or result.outcome, result.detail)
        logger.warning(
            "ownerless worktree preserved path=%s reason=%s detail=%s",
            path,
            result.reason or result.outcome,
            result.detail,
        )
    else:
        summary.record("errors", path, result.reason or result.outcome, result.detail or "; ".join(result.errors))
        logger.error(
            "ownerless worktree cleanup failed path=%s reason=%s detail=%s",
            path,
            result.reason or result.outcome,
            result.detail or "; ".join(result.errors),
        )


def _parse_time(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_schedule_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_schedule_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(
        json.dumps(dict(state), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


def _failure_backoff(failure_count: int) -> timedelta:
    # Five doublings already exceed the 24-hour cap; bound before exponentiation.
    exponent = min(max(0, failure_count - 1), 5)
    seconds = INITIAL_FAILURE_BACKOFF.total_seconds() * (2**exponent)
    return timedelta(seconds=min(seconds, MAX_FAILURE_BACKOFF.total_seconds()))


def _next_failure_count(state: Mapping[str, Any]) -> int:
    try:
        return max(0, int(state.get("failure_count", 0))) + 1
    except (TypeError, ValueError):
        return 1


def _failure_cause(summary: CleanupSummary) -> str:
    details = [
        ": ".join(part for part in (item.get("reason"), item.get("detail")) if part)
        for item in summary.details
        if item.get("reason") or item.get("detail")
    ]
    return f"sweep reported {summary.errors} error(s)" + (f": {'; '.join(details)}" if details else "")


def schedule_due(root: Path, now: datetime) -> bool:
    """Read the daily schedule without waiting for active maintenance."""
    path = root / SCHEDULE_STATE
    state = _read_schedule_state(path)
    if not state and not path.exists():
        return True
    if state.get("status") == "failed":
        retry_at = _parse_time(state.get("next_attempt_at"))
        return retry_at is None or now.astimezone(timezone.utc) >= retry_at
    previous = _parse_time(state.get("last_attempt_at") if isinstance(state, dict) else None)
    return previous is None or not timedelta(0) <= now - previous < SWEEP_INTERVAL


def _try_lock(handle: Any) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def maybe_run_scheduled(
    root: Path = REPO_ROOT,
    *,
    now_fn: Callable[[], datetime] = utc_now,
    sweep_fn: Callable[..., CleanupSummary] = sweep_orphan_worktrees,
    force: bool = False,
) -> Optional[CleanupSummary]:
    """Run at most once per day; wired into the boot-scheduled fleet dispatcher."""
    state_path = root / SCHEDULE_STATE
    lock_path = state_path.with_suffix(".sweep.lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            if not _try_lock(handle):
                return None
            started_at = now_fn()
            if not force and not schedule_due(root, started_at):
                return None
            try:
                summary = sweep_fn(root)
            except Exception as exc:  # noqa: BLE001 - persist failures so dispatchers honor backoff
                cause = f"{type(exc).__name__}: {exc}"
                logger.exception("scheduled orphan worktree sweep failed: %s", cause)
                if not force:
                    previous = _read_schedule_state(state_path)
                    failure_count = _next_failure_count(previous)
                    retry_at = started_at + _failure_backoff(failure_count)
                    _write_schedule_state(
                        state_path,
                        {
                            "status": "failed",
                            "last_attempt_at": started_at.astimezone(timezone.utc).isoformat(),
                            "failure_count": failure_count,
                            "next_attempt_at": retry_at.astimezone(timezone.utc).isoformat(),
                            "last_failure": cause,
                        },
                    )
                return CleanupSummary(errors=1, details=[{"path": str(root), "reason": "sweep_exception", "detail": cause}])
            if not force:
                if summary.errors:
                    previous = _read_schedule_state(state_path)
                    failure_count = _next_failure_count(previous)
                    cause = _failure_cause(summary)
                    retry_at = started_at + _failure_backoff(failure_count)
                    _write_schedule_state(
                        state_path,
                        {
                            "status": "failed",
                            "last_attempt_at": started_at.astimezone(timezone.utc).isoformat(),
                            "failure_count": failure_count,
                            "next_attempt_at": retry_at.astimezone(timezone.utc).isoformat(),
                            "last_failure": cause,
                        },
                    )
                    logger.error("scheduled orphan worktree sweep failed; retry_at=%s cause=%s", retry_at, cause)
                else:
                    _write_schedule_state(
                        state_path,
                        {
                            "status": "succeeded",
                            "last_attempt_at": started_at.astimezone(timezone.utc).isoformat(),
                            "failure_count": 0,
                        },
                    )
            return summary
    except Exception:  # noqa: BLE001 - a failed sweep must be retried and visible
        logger.exception("scheduled orphan worktree sweep failed")
        return CleanupSummary(errors=1, details=[{"path": str(root), "reason": "sweep_exception"}])


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheduled", action="store_true", help="honor the persistent daily schedule guard")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    summary = maybe_run_scheduled(args.root, force=not args.scheduled)
    if summary is None:
        print(json.dumps({"status": "skipped", "reason": "not_due_or_already_running"}))
        return 0
    print(json.dumps(summary.to_dict(), ensure_ascii=False))
    return 1 if summary.errors else 0


__all__ = [
    "CleanupSummary",
    "REPO_ROOT",
    "SCHEDULE_STATE",
    "SWEEP_INTERVAL",
    "main",
    "maybe_run_scheduled",
    "sweep_orphan_worktrees",
]
