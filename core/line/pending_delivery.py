"""Durable route waits consumed by the existing dispatcher without a chat supervisor."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import psutil
from pydantic import BaseModel, Field

from core.paths import state_root
from core.line import run_outcome
from core.line.admission_store import AdmissionStore

logger = logging.getLogger(__name__)
RECHECK_SECONDS = 900
STALE_DISPATCHER_IMPORT = "stale_dispatcher_import"
# Red CI repair is still a delivery, even after the ledger was marked completed (USR-509).
CI_RECOVERY_CAUSES = frozenset({"ci_fix_requires_revalidation", "ci_fix_route_wait", "ci_red"})
# These stay blocked even when a review route appears (USR-512 T3).
_PERMANENT_BLOCK_CAUSES = frozenset({
    "deploy_evidence_missing",
    "deploy_evidence_mismatch",
    "implementing_family_unknown",
})
# Preserved exit of the USR-467 review crash. Not a general launcher failure.
_LEGACY_REVIEW_EXIT = "launcher_exit:1"
_RESUMABLE_REVIEW_MARKERS = ("no_route_available", "review_agent_exhausted", "route_wait")
# A blocked evidence wait of a ticket closed this long ago is obsolete (USR-490).
_OBSOLETE_EVIDENCE_CAUSES = frozenset({"deploy_evidence_missing", "deploy_evidence_mismatch"})
OBSOLETE_WAIT_SECONDS = 6 * 3600
_children: dict[int, subprocess.Popen] = {}


class PendingChildFailure(BaseModel):
    """Redacted structured result of a failed pending-delivery child."""

    run_id: str
    attempt: int = 1
    exit_code: int
    cause: str
    cause_class: str
    stage: str
    detail: str
    log_path: str = ""
    outcome_path: str = ""
    recorded_at: str


class PendingReconciliation(BaseModel):
    """One durable, idempotent transition found while reconciling a legacy wait."""

    fingerprint: str
    from_status: str
    from_cause: str
    to_status: str
    to_cause: str
    reason: str
    evidence: str
    recorded_at: str


class PendingDelivery(BaseModel):
    ticket_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    worktree: str
    stage: Literal["grill", "planning", "development", "review"]
    implementing_harness: str | None = None
    resume_delivery: bool = False
    next_check: float = 0
    pid: int | None = None
    started_at: float = 0
    status: Literal["waiting", "running", "blocked"] = "waiting"
    cause: str = "route_wait"
    child_failures: list[PendingChildFailure] = Field(default_factory=list)
    project_id: str = "darkfac"
    run_id: str | None = None


class TicketStatusConflict(RuntimeError):
    """The durable queue has contradictory status rows for one ticket."""

    def __init__(self, evidence: str) -> None:
        super().__init__("contradictory ticket status evidence")
        self.evidence = evidence


def queue_dir() -> Path:
    """Share waits across linked worktrees, including standalone operator resumes."""
    if os.environ.get("DARKFAC_STATE_ROOT"):
        return state_root() / "pending_delivery"
    from core.git.ticket_workspace import main_checkout_root
    return main_checkout_root(Path(__file__).resolve().parents[2]) / ".factory" / "pending_delivery"


def _save(path: Path, row: PendingDelivery) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(row.model_dump_json(indent=2))
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def schedule(ticket_id: str, worktree: Path, stage: str, *, resume_delivery: bool = False,
             cause: str = "route_wait", directory: Path | None = None) -> PendingDelivery:
    """Persist a wait before the launcher exits; writing is required, never best-effort."""
    from core.line.ticket_engine import artifacts_root
    harness = None
    progress = artifacts_root() / ticket_id / ".darkfac" / "runs" / ticket_id / "progress.json"
    if progress.is_file():
        harness = json.loads(progress.read_text(encoding="utf-8")).get("harness")
    row = PendingDelivery(ticket_id=ticket_id, worktree=str(worktree.resolve()), stage=stage,
                          project_id=run_outcome.current_project(), run_id=run_outcome.current_run_id(),
                          implementing_harness=harness, resume_delivery=resume_delivery,
                          next_check=time.time() + RECHECK_SECONDS, cause=cause[:500])
    if stage == "review" and not harness:
        row.status, row.cause = "blocked", "implementing_family_unknown"
    path = (directory or queue_dir()) / f"{row.ticket_id}.json"
    if path.is_file():
        try:
            previous = PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"pending delivery state is unreadable; preserved at {path}") from exc
        if previous.ticket_id == row.ticket_id and Path(previous.worktree).resolve() == Path(row.worktree).resolve():
            row.child_failures = previous.child_failures
    _save(path, row)
    return row


def _cached_quota(provider: str) -> float | None:
    """Read snapshots only: an idle scheduler must not infer to measure its quota.

    A snapshot older than the freshness window is not discarded: routing's hold keeps the last valid
    reading (pessimistic margin) until its window resets, at most 6 h.
    """
    from datetime import datetime, timezone
    from core.line.routing import _stale_until_reset_headroom
    from core.router.token_budget import _quota_headroom
    from core.usage.models import ProviderAccountUsage
    snapshot = state_root() / "usage" / "providers" / f"{provider}.json"
    try:
        usage = ProviderAccountUsage.model_validate_json(snapshot.read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(str(usage.checked_at).replace("Z", "+00:00"))
        if checked.tzinfo is None or (datetime.now(timezone.utc) - checked).total_seconds() > 3600:
            return _stale_until_reset_headroom(snapshot, datetime.now(timezone.utc))
        if usage.plausibility_flags:
            return None
        return _quota_headroom(usage)
    except (OSError, ValueError, TypeError):
        return None


def _route(row: PendingDelivery):
    from core.line.routing import pick
    if row.stage == "review" and not row.implementing_harness:
        logger.warning("Implementing family unavailable for %s; review cannot resume", row.ticket_id)
        return None
    return pick(row.stage, ["harness:claude", "harness:codex", "harness:grok"],
                implementing_harness=row.implementing_harness, quota_lookup=_cached_quota,
                execution_context="headless")


def _ci_recovery_without_merge(row: PendingDelivery, release: Path, merge_path: Path) -> bool:
    """True when a red-CI wait has no SHA-bearing merge or deploy receipt."""
    if row.cause not in CI_RECOVERY_CAUSES:
        return False
    for path, sha_field in ((release, "sha"), (merge_path, "merge_sha")):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(receipt, dict) and receipt.get(sha_field):
            return False
    return True


def _review_cause_resumable(cause: str) -> bool:
    """True when a REVIEW wait may be tried again after a compatible route appears."""
    if cause in _PERMANENT_BLOCK_CAUSES or cause == _LEGACY_REVIEW_EXIT:
        return False
    if cause.startswith("launcher_exit:"):
        return False
    return any(marker in cause for marker in _RESUMABLE_REVIEW_MARKERS)


def _blocked_review_reopen(row: PendingDelivery) -> str:
    """``resumable`` or ``legacy`` when a blocked REVIEW row may leave ``blocked``.

    Empty means the block stays. ``legacy`` is only the preserved USR-467 exit and
    still has to pass the candidate, gate and absent-review checks before launch.
    """
    if row.status != "blocked" or row.stage != "review":
        return ""
    if row.cause in _PERMANENT_BLOCK_CAUSES:
        return ""
    if row.cause == _LEGACY_REVIEW_EXIT:
        return "legacy"
    if _review_cause_resumable(row.cause):
        return "resumable"
    return ""


def _ticket_run_dir(ticket_id: str) -> Path:
    from core.line.ticket_engine import artifacts_root
    return artifacts_root() / ticket_id / ".darkfac" / "runs" / ticket_id


def _live_candidate_digest(worktree: str) -> str | None:
    """Digest of the preserved worktree, or None when git cannot show it."""
    from core.line.stage_review import _candidate_digest
    from core.line.workspace import RunWorkspace, WorkspaceError
    ws = RunWorkspace(
        project_id="darkfac",
        run_id="pending-review",
        branch="ticket/pending-review",
        path=Path(worktree),
        base_sha="0" * 40,
    )
    try:
        return _candidate_digest(ws, "main")
    except (OSError, WorkspaceError):
        logger.warning("candidate digest unavailable for preserved review wait at %s", worktree)
        return None


def _review_still_absent(context: Path, digest: str) -> bool:
    """True when this candidate has no verdict, execution receipt or bound approval."""
    if (context / "review_verdict.json").is_file() or (context / "review_execution.json").is_file():
        return False
    state_path = context / "review_state.json"
    if not state_path.is_file():
        return True
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    rounds = payload.get("rounds") if isinstance(payload, dict) else None
    if not isinstance(rounds, list):
        return False
    for item in rounds:
        if not isinstance(item, dict):
            continue
        if item.get("verdict") == "approve" and item.get("candidate_digest") == digest and not item.get("forced"):
            return False
    return True


def _budget_store(now: float) -> AdmissionStore:
    return AdmissionStore(state_root() / "admission.sqlite3",
                          clock=lambda: datetime.fromtimestamp(now, timezone.utc))


def _child_finished(path: Path, row: PendingDelivery, rc: int, repo_root: Path, now: float) -> dict:
    """A completed child either waits for recheck or consumes the shared retry budget."""
    outcome = run_outcome.read_outcome(row.ticket_id, row.run_id)
    row.pid = None
    row.status = "waiting"
    if outcome is not None:
        rc = outcome.exit_code
    row.next_check = now + RECHECK_SECONDS
    if rc:
        log = path.with_suffix(".log")
        text = log.read_text(encoding="utf-8", errors="replace")[-131072:] if log.is_file() else ""
        cause = outcome.cause if outcome else run_outcome.diagnostic_cause(text, rc)
        detail = outcome.detail if outcome else run_outcome.error_detail(text, cause)
        failure_run_id = row.run_id or uuid.uuid4().hex
        row.run_id = failure_run_id
        failure_stage = str(getattr(outcome, "stage", None) or row.stage).lower()
        failure = PendingChildFailure(
            run_id=failure_run_id,
            attempt=max((item.attempt for item in row.child_failures), default=0) + 1,
            exit_code=int(rc),
            cause=str(cause),
            cause_class=str(
                getattr(getattr(outcome, "cause_class", None), "value", None)
                or getattr(run_outcome.classify(cause), "value", "unknown")
            ),
            stage=failure_stage,
            detail=run_outcome.error_detail(str(detail), str(cause))[:1000],
            log_path=str(log.resolve()),
            outcome_path=str(run_outcome.outcome_path(row.ticket_id).resolve()),
            recorded_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
        )
        if not any(item.run_id == failure.run_id for item in row.child_failures):
            row.child_failures = [*row.child_failures, failure][-50:]
        row.cause = str(cause)
        if row.stage == "review" or failure_stage in _DELIVERY_FAILURE_STAGES:
            # A child can start from a BUILD wait and fail only after it reaches delivery.
            # Persist the actual continuation before the dispatcher can launch another child.
            row.stage = "review"
            row.resume_delivery = True
        # Persist the structured failure and retry mode before touching admission accounting or
        # allowing a later dispatcher tick to start another child.
        _save(path, row)
        store = _budget_store(now)
        budget = store.record_attempt(row.project_id, row.ticket_id, cause,
                                      run_id=failure_run_id,
                                      main_sha=run_outcome.main_sha(repo_root), detail=detail, log_path=str(log),
                                      phase=failure.stage,
                                      next_step="Resume the saved delivery at its recorded stage after the persisted backoff.")
        row.next_check = float(budget["next_attempt_at"]) or now + RECHECK_SECONDS
        if store.ticket_hold(project_id=row.project_id, ticket_id=row.ticket_id):
            row.status = "blocked"
    else:
        store = _budget_store(now)
    _save(path, row)
    return {"ticket": row.ticket_id, "status": row.status, "cause": row.cause, "next_check": row.next_check}


def _legacy_review_exit_ready(row: PendingDelivery) -> bool:
    """USR-467: reopen ``launcher_exit:1`` only with a candidate, a current gate and no review."""
    context = _ticket_run_dir(row.ticket_id)
    if not (context / "progress.json").is_file():
        return False
    try:
        validation = json.loads((context / "validation.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(validation, dict) or validation.get("passed") is not True:
        return False
    digest = validation.get("candidate_digest")
    if not isinstance(digest, str) or not digest.strip():
        return False
    digest = digest.strip()
    if _live_candidate_digest(row.worktree) != digest:
        return False
    return _review_still_absent(context, digest)


def _closed_since(ticket: dict, path: Path) -> float | None:
    """Epoch of the ticket's last update, or the wait file mtime when it is unknown."""
    try:
        stamp = datetime.fromisoformat(str(ticket.get("updated_at")).replace("Z", "+00:00"))
        return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        pass
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _archive_obsolete_wait(path: Path, row: PendingDelivery, now: float) -> dict | None:
    """Move a blocked evidence wait of a long-closed ticket to ``archive/`` (never deleted)."""
    from core.demands.queue_store import control_store
    store = control_store()
    if getattr(store, "mock_mode", False):
        return None
    ticket = next((t for t in store.list_tickets() if t.get("id") == row.ticket_id), None)
    if ticket is None or ticket.get("status") not in {"completed", "cancelled", "canceled"}:
        return None
    closed = _closed_since(ticket, path)
    if closed is None or now - closed <= OBSOLETE_WAIT_SECONDS:
        return None
    target = path.parent / "archive"
    target.mkdir(parents=True, exist_ok=True)
    os.replace(path, target / path.name)
    logger.info("archived obsolete pending delivery %s cause=%s ticket_status=%s",
                row.ticket_id, row.cause, ticket.get("status"))
    return {"ticket": row.ticket_id, "status": "archived", "cause": row.cause}


def _pid_running(row: PendingDelivery) -> bool:
    try:
        process = psutil.Process(row.pid)
        return process.is_running() and abs(process.create_time() - row.started_at) < 5
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True  # A possibly live owner must not be duplicated.


_TERMINAL_TICKET_STATUSES = frozenset({"completed", "cancelled", "canceled"})
_DELIVERY_FAILURE_STAGES = frozenset({"review", "gate", "ci", "merge", "deploy", "delivery"})


def _ticket_status(ticket_id: str) -> str | None:
    """Durable status, or None when there is no coherent terminal proof yet."""
    from core.demands.queue_store import control_store

    try:
        store = control_store()
        if getattr(store, "mock_mode", False):
            raise RuntimeError("pending delivery requires the durable control store")
        matches = [item for item in store.list_tickets() if item.get("id") == ticket_id]
        if not matches:
            raise RuntimeError(f"Ticket {ticket_id} absent from durable queue")
    except Exception:
        logger.debug("terminal reconciliation has no ticket proof for %s", ticket_id, exc_info=True)
        return None
    statuses = sorted({str(item.get("status") or "") for item in matches})
    if len(statuses) > 1:
        raise TicketStatusConflict("ticket_statuses:" + ",".join(statuses))
    return statuses[0]


def _record_reconciliation(
    path: Path,
    row: PendingDelivery,
    *,
    to_status: str,
    to_cause: str,
    reason: str,
    evidence: str,
    now: float,
) -> None:
    """Persist a stable transition before deleting or blocking the legacy wait."""
    details = {
        "ticket_id": row.ticket_id,
        "from_status": row.status,
        "from_cause": row.cause,
        "to_status": to_status,
        "to_cause": to_cause,
        "reason": reason,
        "evidence": evidence,
    }
    identity = json.dumps(
        {key: value for key, value in details.items() if key not in {"from_status", "from_cause"}},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    ledger = path.parent / "reconciliations" / f"{row.ticket_id}.json"
    events: list[dict] = []
    if ledger.is_file():
        try:
            stored = json.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(f"reconciliation ledger is unreadable; preserved at {ledger}") from exc
        if (not isinstance(stored, dict) or stored.get("ticket_id") != row.ticket_id
                or not isinstance(stored.get("events"), list)):
            raise RuntimeError(f"reconciliation ledger is contradictory; preserved at {ledger}")
        try:
            events = [
                PendingReconciliation.model_validate(event).model_dump(mode="json")
                for event in stored["events"]
            ]
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"reconciliation ledger is contradictory; preserved at {ledger}") from exc
    if events and events[-1].get("fingerprint") == fingerprint:
        return
    event = PendingReconciliation(
        fingerprint=fingerprint,
        from_status=row.status,
        from_cause=row.cause,
        to_status=to_status,
        to_cause=to_cause,
        reason=reason,
        evidence=evidence,
        recorded_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
    )
    events = [*events, event.model_dump(mode="json")][-100:]
    ledger.parent.mkdir(parents=True, exist_ok=True)
    temporary = ledger.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps({"ticket_id": row.ticket_id, "events": events}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, ledger)


def _release_paths(row: PendingDelivery) -> tuple[Path, Path, dict]:
    from core.line.ticket_engine import artifacts_root

    release = artifacts_root() / row.ticket_id / ".darkfac" / "runs" / row.ticket_id / "deploy_report.json"
    report: dict = {}
    if release.is_file():
        try:
            loaded = json.loads(release.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            loaded = {}
        if isinstance(loaded, dict):
            report = loaded
    return release, release.with_name("merge.json"), report


def _evidence_incoherent(release: Path, report: dict) -> str:
    """Empty when the deploy bundle matches. Otherwise the blocked cause."""
    try:
        merge = json.loads(release.with_name("merge.json").read_text(encoding="utf-8"))
        validation = json.loads(release.with_name("validation.json").read_text(encoding="utf-8"))
        review = json.loads(release.with_name("review_state.json").read_text(encoding="utf-8"))["rounds"][-1]
        execution = json.loads(release.with_name("review_execution.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return "deploy_evidence_missing"
    families = execution.get("implementation_families", [execution.get("implementer_family")])
    if (merge.get("merge_sha") != report.get("sha") or not validation.get("passed")
            or not validation.get("candidate_digest")
            or review.get("candidate_digest") != validation["candidate_digest"]
            or merge.get("candidate_digest") != validation["candidate_digest"]
            or execution.get("candidate_digest") != validation["candidate_digest"]
            or not execution.get("reviewer_family") or not execution.get("implementer_family")
            or execution["reviewer_family"] in families
            or review.get("verdict") != "approve" or review.get("forced")):
        return "deploy_evidence_mismatch"
    return ""


def _evidence_token(release: Path, report: dict, problem: str) -> str:
    """Stable evidence identity. A new token is a new alert; the same token is silent."""
    if problem == "deploy_evidence_missing":
        if not release.is_file():
            return "release_missing"
        if not report.get("sha"):
            return "sha_missing"
        if not report.get("deployed"):
            return "not_deployed"
        return "files_missing"
    if problem == "deploy_evidence_mismatch":
        return f"mismatch:{report.get('sha') or ''}"
    return f"deployed:{report.get('sha') or ''}"


def _block_wait(
    path: Path,
    row: PendingDelivery,
    cause: str,
    evidence: str,
    *,
    now: float,
    reason: str = "terminal evidence is missing or contradictory",
    next_step: str = "Reconcile the recorded terminal evidence, then release or resume this delivery wait.",
) -> dict:
    _record_reconciliation(
        path,
        row,
        to_status="blocked",
        to_cause=cause,
        reason=reason,
        evidence=evidence,
        now=now,
    )
    row.status, row.cause = "blocked", cause
    _save(path, row)
    try:
        from core.line.failure_alerts import notify_deterministic_failure

        notify_deterministic_failure(
            row.ticket_id, cause, evidence, str(path), phase=row.stage,
            attempts=max((item.attempt for item in row.child_failures), default=0),
            next_step=next_step,
        )
    except Exception:
        logger.exception("deterministic failure alert failed for %s", row.ticket_id)
    return {"ticket": row.ticket_id, "status": row.status, "cause": row.cause, "stage": row.stage}


def _completed_report(row: PendingDelivery, release: Path, merge_path: Path, report: dict) -> dict:
    if report or not merge_path.is_file():
        return report
    try:
        merge = json.loads(merge_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return report
    if not isinstance(merge, dict):
        return report
    return {"sha": merge.get("merge_sha"), "deployed": False}


def _reconcile_terminal_wait(path: Path, row: PendingDelivery, now: float) -> dict | None:
    """Terminal tickets are decided before blocked/backoff and removed only with proof.

    ``cancelled`` and ``canceled`` use the durable control-store status as proof.
    ``completed`` is removed only when the deploy bundle is coherent and deployed.
    CI recovery without a SHA-bearing receipt stays a delivery. No proof means
    the wait is left for the ordinary due path.
    """
    try:
        status = _ticket_status(row.ticket_id)
    except TicketStatusConflict as exc:
        return _block_wait(
            path, row, "ticket_status_conflict", exc.evidence, now=now,
        )
    if status not in _TERMINAL_TICKET_STATUSES:
        return None
    release, merge_path, report = _release_paths(row)
    if status == "completed":
        report = _completed_report(row, release, merge_path, report)
        if _ci_recovery_without_merge(row, release, merge_path):
            resume_delivery = True
            next_status = "waiting" if row.status == "blocked" else row.status
            changed = resume_delivery != row.resume_delivery or next_status != row.status
            if changed:
                _record_reconciliation(
                    path, row, to_status=next_status, to_cause=row.cause,
                    reason="delivery recovery remains actionable without a merge receipt",
                    evidence=f"ticket_status:{status};cause:{row.cause}", now=now,
                )
                row.resume_delivery = resume_delivery
                row.status = next_status
                _save(path, row)
            return None
        if row.resume_delivery and row.child_failures and not report.get("sha"):
            # The durable ticket status may have been closed when a PR opened. A failed delivery
            # child with no merge/deploy receipt is still actionable and must resume delivery.
            return None
    if status in {"cancelled", "canceled"}:
        _record_reconciliation(
            path, row, to_status="terminal", to_cause="", reason="ticket cancellation is durable",
            evidence=f"ticket_status:{status}", now=now,
        )
        path.unlink(missing_ok=True)
        return {"ticket": row.ticket_id, "status": "terminal", "proof": status, "stage": row.stage}
    if report.get("sha") and not report.get("deployed"):
        problem = _evidence_incoherent(release, report)
        if problem:
            return _block_wait(path, row, problem, _evidence_token(release, report, problem), now=now)
        _record_reconciliation(
            path, row, to_status="merged_awaiting_deploy", to_cause="",
            reason="coherent merge receipt awaits deployment proof", evidence=f"merge_sha:{report['sha']}",
            now=now,
        )
        return {"ticket": row.ticket_id, "status": "merged_awaiting_deploy", "sha": report["sha"], "stage": row.stage}
    if not release.is_file() or not report.get("deployed") or not report.get("sha"):
        cause = "deploy_evidence_missing"
        return _block_wait(path, row, cause, _evidence_token(release, report, cause), now=now)
    problem = _evidence_incoherent(release, report)
    if problem:
        return _block_wait(path, row, problem, _evidence_token(release, report, problem), now=now)
    _record_reconciliation(
        path, row, to_status="terminal", to_cause="", reason="coherent deployed release is durable",
        evidence=f"deployed_sha:{report['sha']}", now=now,
    )
    path.unlink(missing_ok=True)
    return {"ticket": row.ticket_id, "status": "terminal", "proof": "deployed", "stage": row.stage}


def _check_completed_deploy_evidence(
    path: Path, row: PendingDelivery, release: Path, ticket: dict,
) -> tuple[dict, dict | None]:
    """Validate coherent deploy artifacts for a pending row once a release SHA exists."""
    report = json.loads(release.read_text(encoding="utf-8")) if release.is_file() else {}
    merge_path = release.with_name("merge.json")
    if not report and ticket.get("status") == "completed" and merge_path.is_file():
        report = {"sha": json.loads(merge_path.read_text(encoding="utf-8")).get("merge_sha"), "deployed": False}
    if report.get("sha") and not report.get("deployed"):
        try:
            merge = json.loads(merge_path.read_text(encoding="utf-8"))
            validation = json.loads(release.with_name("validation.json").read_text(encoding="utf-8"))
            review = json.loads(release.with_name("review_state.json").read_text(encoding="utf-8"))["rounds"][-1]
            execution = json.loads(release.with_name("review_execution.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            row.status, row.cause = "blocked", "deploy_evidence_missing"
            _save(path, row)
            return report, {"ticket": row.ticket_id, "status": row.status, "cause": row.cause}
        if (merge.get("merge_sha") != report["sha"] or not validation.get("passed")
                or not validation.get("candidate_digest")
                or review.get("candidate_digest") != validation["candidate_digest"]
                or merge.get("candidate_digest") != validation["candidate_digest"]
                or execution.get("candidate_digest") != validation["candidate_digest"]
                or not execution.get("reviewer_family") or not execution.get("implementer_family")
                or execution["reviewer_family"] in execution.get("implementation_families", [execution["implementer_family"]])
                or review.get("verdict") != "approve" or review.get("forced")):
            row.status, row.cause = "blocked", "deploy_evidence_mismatch"
            _save(path, row)
            return report, {"ticket": row.ticket_id, "status": row.status, "cause": row.cause}
        return report, {"ticket": row.ticket_id, "status": "merged_awaiting_deploy", "sha": report["sha"]}
    return report, None


def _validate_pending_worktree(
    path: Path, row: PendingDelivery, repo_root: Path, now: float,
) -> Path | dict:
    """Confirm the pending row's worktree is registered under the ticket's branch."""
    from core.git.ticket_workspace import list_worktrees

    worktree = Path(row.worktree).resolve()
    entries = list_worktrees(repo_root)
    registered = [entry for entry in entries if entry.get("worktree")
                  and Path(entry["worktree"]).resolve() == worktree]
    if not registered:
        return _block_wait(
            path, row, "pending_worktree_unregistered",
            f"ticket:{row.ticket_id};worktree:{worktree};registered:false", now=now,
            reason="pending worktree is absent from the Git worktree registry",
            next_step="Restore or re-register the recorded ticket worktree, then resume this pending delivery.",
        )
    slug = row.ticket_id.lower()
    branch_ok = any(
        (branch := str(entry.get("branch", "")).removeprefix("refs/heads/").lower())
        == f"ticket/{slug}" or branch.startswith(f"ticket/{slug}-")
        for entry in registered
    )
    if not worktree.is_dir() or not branch_ok:
        branches = ",".join(sorted(
            str(entry.get("branch", "")).removeprefix("refs/heads/") or "detached"
            for entry in registered
        ))
        return _block_wait(
            path, row, "pending_worktree_mismatch",
            f"ticket:{row.ticket_id};worktree:{worktree};branches:{branches};directory_exists:{worktree.is_dir()}",
            now=now,
            reason="pending worktree is registered under a different ticket branch or is missing",
            next_step="Restore the same ticket branch and worktree recorded by the BUILD checkpoint, then resume delivery.",
        )
    return worktree


def _reconcile_terminal_ticket(
    path: Path, row: PendingDelivery, release: Path, merge_path: Path, report: dict, ticket: dict, now: float,
) -> dict | None:
    """Resolve terminal ticket rows inside `_tick_row` without discarding active CI/delivery recovery."""
    if ticket.get("status") == "completed" and _ci_recovery_without_merge(row, release, merge_path):
        # The ledger was closed when the PR opened. CI is still red and nothing merged.
        row.resume_delivery = True
        return None
    if (ticket.get("status") == "completed" and row.resume_delivery and row.child_failures
            and not report.get("sha")):
        # Preserve the failed-delivery retry path even when the ledger's status is terminal.
        return None
    if ticket.get("status") in {"cancelled", "canceled"}:
        path.unlink(missing_ok=True)
        return {"ticket": row.ticket_id, "status": "terminal", "proof": ticket["status"], "stage": row.stage}
    if (ticket.get("status") == "completed" and report.get("deployed") and report.get("sha")
            and not _evidence_incoherent(release, report)):
        path.unlink(missing_ok=True)
        return {"ticket": row.ticket_id, "status": "terminal", "proof": "deployed", "stage": row.stage}
    if ticket.get("status") == "completed":
        cause = _evidence_incoherent(release, report) or "deploy_evidence_missing"
        return _block_wait(path, row, cause, _evidence_token(release, report, cause), now=now)
    return None


def _tick_row(path: Path, repo_root: Path, now: float) -> dict:
    from core.demands.queue_store import control_store
    from core.git.ticket_workspace import list_worktrees
    from core.line.line_lock import is_locked

    row = PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
    if row.resume_delivery and row.stage != "review":
        # Legacy rows must route to REVIEW whenever their persisted launch mode resumes delivery.
        row.stage = "review"
        _save(path, row)
    if row.pid is not None:
        child = _children.get(row.pid)
        if child is not None:
            rc = child.poll()
            if rc is None:
                log_path = path.with_suffix(".log")
                try:
                    heartbeat = log_path.stat().st_mtime
                except OSError:
                    heartbeat = 0.0
                return {"ticket": row.ticket_id, "status": "running", "pid": row.pid,
                        "stage": row.stage, "started_at": row.started_at,
                        "heartbeat_epoch": heartbeat}
            _children.pop(row.pid)
            # A child returning route_wait rewrites this record with its next wait.
            current = PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
            if current.pid != row.pid:
                return {"ticket": row.ticket_id, "status": current.status}
            # The child may have died before the dispatcher restart. Resolve durable terminal
            # proof before turning that stale PID into a technical failure or honoring an old
            # blocked state.
            reconciled = _reconcile_terminal_wait(path, row, now)
            if reconciled is not None:
                return reconciled
            return _child_finished(path, row, rc, repo_root, now)
        elif _pid_running(row):
            log_path = path.with_suffix(".log")
            try:
                heartbeat = log_path.stat().st_mtime
            except OSError:
                heartbeat = 0.0
            return {"ticket": row.ticket_id, "status": "running", "pid": row.pid,
                    "stage": row.stage, "started_at": row.started_at,
                    "heartbeat_epoch": heartbeat}
        else:
            # A recorded PID is not proof that the ticket is still actionable. A terminal
            # ledger/deploy receipt takes precedence over failure accounting after restart.
            reconciled = _reconcile_terminal_wait(path, row, now)
            if reconciled is not None:
                return reconciled
            return _child_finished(path, row, 1, repo_root, now)
    # Archive long-closed evidence waits before terminal unlinking/reconciliation (USR-467 / USR-475).
    if row.status == "blocked" and row.cause in _OBSOLETE_EVIDENCE_CAUSES:
        archived = _archive_obsolete_wait(path, row, now)
        if archived:
            return archived
    # Terminal proof wins over a stale block or a future backoff.
    reconciled = _reconcile_terminal_wait(path, row, now)
    if reconciled is not None:
        return reconciled
    admission = _budget_store(now)
    observed_sha = run_outcome.main_sha(repo_root)
    before = admission.budget(row.project_id, row.ticket_id)
    reason = admission.retry_reason(row.project_id, row.ticket_id,
                                    main_sha=observed_sha, now=now)
    if observed_sha and before.get("main_sha") != observed_sha and before["infra_streak"]:
        row.next_check = 0
        _save(path, row)
    if reason:
        row.status = "blocked" if reason.startswith("ticket_hold:") else "waiting"
        row.next_check = float(admission.budget(row.project_id, row.ticket_id)["next_attempt_at"])
        _save(path, row)
        return {"ticket": row.ticket_id, "status": row.status, "cause": reason, "next_check": row.next_check}
    reopen = _blocked_review_reopen(row)
    if (row.status == "blocked" and not reopen) or row.next_check > now or is_locked():
        result = {"ticket": row.ticket_id, "status": row.status, "next_check": row.next_check}
        if row.status == "blocked":
            result["cause"] = row.cause
        return result
    if reopen == "legacy" and not _legacy_review_exit_ready(row):
        row.next_check = now + RECHECK_SECONDS
        _save(path, row)
        return {"ticket": row.ticket_id, "status": "blocked", "cause": row.cause, "next_check": row.next_check}
    store = control_store()
    if getattr(store, "mock_mode", False):
        raise RuntimeError("pending delivery requires the durable control store")
    ticket = next((t for t in store.list_tickets() if t.get("id") == row.ticket_id), None)
    if ticket is None:
        raise RuntimeError(f"Ticket {row.ticket_id} absent from durable queue")
    from core.line.ticket_engine import artifacts_root
    release = artifacts_root() / row.ticket_id / ".darkfac" / "runs" / row.ticket_id / "deploy_report.json"
    merge_path = release.with_name("merge.json")
    report, deploy_wait = _check_completed_deploy_evidence(path, row, release, ticket)
    if deploy_wait is not None:
        return deploy_wait
    if ticket.get("status") in _TERMINAL_TICKET_STATUSES:
        terminal_outcome = _reconcile_terminal_ticket(path, row, release, merge_path, report, ticket, now)
        if terminal_outcome is not None:
            return terminal_outcome
    if ticket.get("status") == "waiting_human":
        return {"ticket": row.ticket_id, "status": "waiting_human"}
    checked_worktree = _validate_pending_worktree(path, row, repo_root, now)
    if isinstance(checked_worktree, dict):
        return checked_worktree
    worktree = checked_worktree
    row.next_check = now + RECHECK_SECONDS
    _save(path, row)  # Backoff survives network/probe errors and service restart.
    if _route(row) is None:
        if row.stage == "review" and not row.implementing_harness:
            row.status, row.cause = "blocked", "implementing_family_unknown"
            _save(path, row)
            return {"ticket": row.ticket_id, "status": row.status, "cause": row.cause}
        if row.status == "blocked":
            return {
                "ticket": row.ticket_id, "status": "blocked", "cause": row.cause,
                "next_check": row.next_check,
            }
        return {"ticket": row.ticket_id, "status": "waiting_route", "next_check": row.next_check}
    if row.stage == "review":
        # REVIEW never restarts PLAN or BUILD. The same worktree is the candidate.
        row.resume_delivery = True
    if reopen:
        logger.info("reopening preserved review wait for %s cause=%s", row.ticket_id, row.cause)
    row.run_id = f"pending-{row.ticket_id}-{uuid.uuid4().hex}"
    env = dict(os.environ, DARKFAC_STATE_ROOT=str(state_root().resolve()), DARKFAC_RUN_ID=row.run_id)
    env.pop("DARKFAC_OPERATING_HARNESS", None)
    cmd = [sys.executable, str(repo_root / "run_ticket.py")]
    if row.resume_delivery:
        cmd += ["--resume-delivery", row.ticket_id, "--worktree", str(worktree)]
    else:
        cmd += [row.ticket_id]
    log_path = path.with_suffix(".log")
    env["DARKFAC_RUN_LOG"] = str(log_path)
    with log_path.open("ab") as log:
        child = subprocess.Popen(cmd, cwd=repo_root, env=env, stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    row.pid, row.status = child.pid, "running"
    row.started_at = time.time()
    _children[child.pid] = child
    _save(path, row)
    return {"ticket": row.ticket_id, "status": "running", "pid": child.pid,
            "stage": row.stage, "started_at": row.started_at, "heartbeat_epoch": row.started_at}


def acknowledge_deploy(repo_root: Path, rows: list[dict]) -> None:
    """Close only releases proven included in the healthy deployed main SHA."""
    from core.infra.node_sync import VPS_URL, request_json
    from core.line import ticket_engine
    from types import SimpleNamespace
    health = request_json("GET", VPS_URL, None, None)
    sha = health.get("git_sha", "")
    if health.get("status") != "ok" or not sha:
        raise RuntimeError("Deployment health identity missing")
    for row in rows:
        if row.get("status") != "merged_awaiting_deploy":
            continue
        proof = subprocess.run(["git", "merge-base", "--is-ancestor", row["sha"], sha],
                               cwd=repo_root, capture_output=True, check=False)
        if proof.returncode:
            raise RuntimeError("Deployed SHA does not include the pending candidate")
        ws = ticket_engine.run_workspace(SimpleNamespace(id=row["ticket"], project_id="darkfac"))
        ticket_engine.write_deploy(ws, row["sha"], True, True, f"Canonical deploy succeeded; production health SHA {sha} verified")
        (queue_dir() / f"{row['ticket']}.json").unlink(missing_ok=True)


def _restore_wait_snapshot(path: Path, snapshot: bytes | None) -> None:
    """Put the wait file back to the bytes captured before a stale import."""
    if snapshot is None:
        return
    try:
        if path.is_file() and path.read_bytes() == snapshot:
            return
        temporary = path.with_suffix(f".{os.getpid()}.restore.tmp")
        temporary.write_bytes(snapshot)
        os.replace(temporary, path)
    except OSError:
        logger.exception("failed to restore wait %s after stale import", path.name)


def _import_failure_result(path: Path, exc: ImportError) -> dict:
    """``check_failed`` with the stable stale-import cause. ``missing`` follows ``exc.name``."""
    result = {"ticket": path.stem, "status": "check_failed", "cause": STALE_DISPATCHER_IMPORT}
    if isinstance(exc.name, str) and exc.name:
        result["missing"] = exc.name
    return result


def tick(repo_root: Path, *, directory: Path | None = None, now: float | None = None) -> list[dict]:
    """Probe due waits and launch at most one resume, serialized across dispatchers."""
    from core.demands.store import DemandsLockTimeout, _PathMutex
    directory = directory or queue_dir()
    if not directory.is_dir():
        return []
    results = []
    try:
        with _PathMutex(directory / "dispatch", timeout=0.1):
            for path in sorted(directory.glob("*.json")):
                try:
                    snapshot = path.read_bytes()
                except OSError:
                    snapshot = None
                try:
                    result = _tick_row(path, repo_root, time.time() if now is None else now)
                    # check_failed keeps the exact dict the caller already asserts.
                    if (isinstance(result, dict) and result.get("status") != "check_failed"
                            and "stage" not in result and path.is_file()):
                        try:
                            result["stage"] = PendingDelivery.model_validate_json(
                                path.read_text(encoding="utf-8")).stage
                        except (OSError, ValueError):
                            pass
                except ImportError as exc:
                    # ModuleNotFoundError subclasses ImportError and shares this cause.
                    logger.warning(
                        "Resume check failed for %s: %s missing=%s",
                        path.name, STALE_DISPATCHER_IMPORT, exc.name,
                    )
                    _restore_wait_snapshot(path, snapshot)
                    result = _import_failure_result(path, exc)
                except Exception as exc:
                    logger.warning("Resume check failed for %s: %s", path.name, type(exc).__name__)
                    result = {"ticket": path.stem, "status": "check_failed", "cause": type(exc).__name__}
                results.append(result)
                if result["status"] in {"running", "merged_awaiting_deploy"}:
                    break
    except DemandsLockTimeout:
        return [{"status": "dispatcher_busy"}]
    return results
