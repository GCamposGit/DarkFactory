"""Sweep ownerless Git worktrees through the shared safe cleanup routine (USR-500 T3)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.common.time import utc_now  # noqa: E402
from core.git import ticket_workspace  # noqa: E402

logger = logging.getLogger("darkfac.git.orphan_worktree_cleanup")

SWEEP_INTERVAL = timedelta(hours=6)
SCHEDULE_STATE = Path(".factory") / "runs" / "orphan_worktree_cleanup.json"
_LOCK_STALE_S = 300.0
_LOCK_WAIT_S = 2.0

_BLOCKED_REASONS = frozenset({"dirty", "holder", "locked", "index_lock", "detached_outside_base"})
_ERROR_REASONS = frozenset(
    {"psutil_unavailable", "git_unreadable", "status_unreadable", "ticket_status_unknown", "age_unknown", "unregistered"}
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
        return asdict(self)


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

    for index, entry in enumerate(entries):
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
            )
        except Exception as exc:  # noqa: BLE001 - isolate malformed worktree state
            summary.record("errors", path, "classification_error", f"{type(exc).__name__}: {exc}")
            logger.exception("could not classify worktree %s; continuing sweep", path)
            continue

        if not verdict.eligible:
            category = "blocked" if verdict.reason in _BLOCKED_REASONS else (
                "errors" if verdict.reason in _ERROR_REASONS else "ignored"
            )
            summary.record(category, path, verdict.reason, verdict.detail)
            continue

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
            continue

        if result.removed:
            summary.record("removed", path, result.outcome, result.detail)
        elif result.outcome in {"blocked_changes", "blocked_process"}:
            summary.record("blocked", path, result.reason or result.outcome, result.detail)
        else:
            summary.record("errors", path, result.reason or result.outcome, result.detail or "; ".join(result.errors))

    logger.info(
        "orphan worktree sweep: removed=%d blocked=%d ignored=%d errors=%d",
        summary.removed,
        summary.blocked,
        summary.ignored,
        summary.errors,
    )
    return summary


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


def _write_schedule_state(path: Path, attempted_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(
        json.dumps({"last_attempt_at": attempted_at.astimezone(timezone.utc).isoformat()}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


def _claim_due_run(state_path: Path, now: datetime) -> bool:
    """Atomically claim one scheduled attempt; the lock protects only the state update."""
    lock_path = state_path.with_name(state_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _LOCK_WAIT_S
    descriptor: Optional[int] = None
    while descriptor is None:
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > _LOCK_STALE_S:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    try:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            logger.warning("schedule state unreadable; running a recovery sweep: %s", exc)
            state = {}
        previous = _parse_time(state.get("last_attempt_at") if isinstance(state, dict) else None)
        current = now.astimezone(timezone.utc)
        elapsed = current - previous if previous is not None else SWEEP_INTERVAL
        if previous is not None and timedelta(0) <= elapsed < SWEEP_INTERVAL:
            return False
        try:
            _write_schedule_state(state_path, current)
        except OSError:
            logger.exception("could not persist orphan worktree sweep schedule state")
            return False
        return True
    finally:
        os.close(descriptor)
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove orphan worktree schedule lock %s", lock_path)


def maybe_run_scheduled(
    root: Path = REPO_ROOT,
    *,
    now_fn: Callable[[], datetime] = utc_now,
    sweep_fn: Callable[..., CleanupSummary] = sweep_orphan_worktrees,
) -> Optional[CleanupSummary]:
    """Run at most once per six hours; wired into the boot-scheduled fleet dispatcher."""
    state_path = root / SCHEDULE_STATE
    try:
        due = _claim_due_run(state_path, now_fn())
    except OSError:
        logger.exception("could not claim scheduled orphan worktree sweep")
        return None
    if not due:
        return None
    try:
        summary = sweep_fn(root)
    except Exception:  # noqa: BLE001 - the fleet loop must continue after maintenance failure
        logger.exception("scheduled orphan worktree sweep failed")
        return CleanupSummary(errors=1, details=[{"path": str(root), "reason": "sweep_exception"}])
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheduled", action="store_true", help="honor the persistent six-hour schedule guard")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.scheduled:
        summary = maybe_run_scheduled(args.root)
        if summary is None:
            print(json.dumps({"status": "not_due", "interval_hours": SWEEP_INTERVAL.total_seconds() / 3600}))
            return 0
    else:
        summary = sweep_orphan_worktrees(args.root)
    print(json.dumps(summary.to_dict(), ensure_ascii=False))
    return 1 if summary.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
