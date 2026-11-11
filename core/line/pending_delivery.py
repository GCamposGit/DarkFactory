"""Durable route waits consumed by the existing dispatcher without a chat supervisor."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

import psutil
from pydantic import BaseModel, Field

from core.paths import state_root

logger = logging.getLogger(__name__)
RECHECK_SECONDS = 900
_children: dict[int, subprocess.Popen] = {}


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
    # One automatic release of a technical waiting_human. A later pause stays paused (USR-447).
    technical_release_done: bool = False


def queue_dir() -> Path:
    """Share waits across linked worktrees, including standalone operator resumes."""
    if os.environ.get("DARKFAC_STATE_ROOT"):
        return state_root() / "pending_delivery"
    from core.git.ticket_workspace import main_checkout_root
    return main_checkout_root(Path(__file__).resolve().parents[2]) / ".factory" / "pending_delivery"


def _save(path: Path, row: PendingDelivery) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    temp.write_text(row.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temp, path)


def schedule(ticket_id: str, worktree: Path, stage: str, *, resume_delivery: bool = False,
             cause: str = "route_wait", directory: Path | None = None) -> PendingDelivery:
    """Persist a wait before the launcher exits; writing is required, never best-effort."""
    from core.line.ticket_engine import artifacts_root
    harness = None
    progress = artifacts_root() / ticket_id / ".darkfac" / "runs" / ticket_id / "progress.json"
    if progress.is_file():
        harness = json.loads(progress.read_text(encoding="utf-8")).get("harness")
    row = PendingDelivery(ticket_id=ticket_id, worktree=str(worktree.resolve()), stage=stage,
                          implementing_harness=harness, resume_delivery=resume_delivery,
                          next_check=time.time() + RECHECK_SECONDS, cause=cause[:500])
    if stage == "review" and not harness:
        row.status, row.cause = "blocked", "implementing_family_unknown"
    _save((directory or queue_dir()) / f"{row.ticket_id}.json", row)
    return row


def _cached_quota(provider: str) -> float | None:
    """Read snapshots only: an idle scheduler must not infer to measure its quota."""
    from datetime import datetime, timezone
    from core.router.token_budget import _quota_headroom
    from core.usage.models import ProviderAccountUsage
    try:
        usage = ProviderAccountUsage.model_validate_json(
            (state_root() / "usage" / "providers" / f"{provider}.json").read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(str(usage.checked_at).replace("Z", "+00:00"))
        if checked.tzinfo is None or (datetime.now(timezone.utc) - checked).total_seconds() > 3600:
            return None
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


def _pid_running(row: PendingDelivery) -> bool:
    try:
        process = psutil.Process(row.pid)
        return process.is_running() and abs(process.create_time() - row.started_at) < 5
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True  # A possibly live owner must not be duplicated.


_TECHNICAL_WAIT_PREFIXES = ("rebase_conflict:", "technical_failed:")


def _evidence_is_technical(ticket: dict) -> bool:
    evidence = str(ticket.get("delivery_evidence") or "").strip()
    return evidence.startswith(_TECHNICAL_WAIT_PREFIXES)


def _open_human_request(ticket_id: str, worktree: Path) -> bool:
    """True when a HumanRequest file is still on disk for this ticket."""
    from core.line.human import _REQUEST_FILE
    from core.line.ticket_engine import artifacts_root

    candidates = (
        artifacts_root() / ticket_id / ".darkfac" / "runs" / ticket_id / _REQUEST_FILE,
        worktree / ".darkfac" / "runs" / ticket_id / _REQUEST_FILE,
    )
    for candidate in candidates:
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict) and str(raw.get("kind") or "") and str(raw.get("run_id") or ""):
            return True
    return False


def _mark_ticket_planned(store: object, ticket: dict) -> None:
    """Best-effort durable release. The in-memory row is what this tick continues with."""
    ticket["status"] = "planned"
    ticket_id = str(ticket.get("id") or "")
    upsert = getattr(store, "upsert_ticket", None)
    if upsert is None or not ticket_id:
        return
    try:
        from datetime import datetime, timezone

        upsert(
            str(ticket.get("project_id") or "darkfac"),
            ticket_id,
            "planned",
            dict(ticket),
            datetime.now(timezone.utc),
        )
    except Exception as exc:  # noqa: BLE001 - the in-memory release still lets this tick resume
        logger.warning("Could not persist technical release of %s: %s", ticket_id, type(exc).__name__)


def _release_technical_human_wait(store: object, row: PendingDelivery, ticket: dict) -> bool:
    """Release one technical ``waiting_human``. A decision or an open human request stays paused.

    The flag on the pending row makes the release happen once: putting the ticket back into
    ``waiting_human`` afterwards does not launch another automatic resume.
    """
    if row.technical_release_done:
        return False
    if not _evidence_is_technical(ticket):
        return False
    if _open_human_request(row.ticket_id, Path(row.worktree)):
        return False
    row.technical_release_done = True
    _mark_ticket_planned(store, ticket)
    return True


def _tick_row(path: Path, repo_root: Path, now: float) -> dict:
    from core.demands.queue_store import control_store
    from core.git.ticket_workspace import list_worktrees
    from core.line.line_lock import is_locked

    row = PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
    if row.pid is not None:
        child = _children.get(row.pid)
        if child is not None:
            rc = child.poll()
            if rc is None:
                return {"ticket": row.ticket_id, "status": "running", "pid": row.pid}
            _children.pop(row.pid)
            # A child returning route_wait rewrites this record with its next wait.
            current = PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
            if current.pid != row.pid:
                return {"ticket": row.ticket_id, "status": current.status}
            if rc == 0:
                row.pid, row.status, row.next_check = None, "waiting", 0
                _save(path, row)
            else:
                row.pid = None
                row.status = "waiting" if rc == 75 else "blocked"
                row.cause = f"launcher_exit:{rc}"
                row.next_check = now + RECHECK_SECONDS
                _save(path, row)
        elif _pid_running(row):
            return {"ticket": row.ticket_id, "status": "running", "pid": row.pid}
        else:
            row.pid, row.status = None, "waiting"
    if row.status == "blocked" or row.next_check > now or is_locked():
        return {"ticket": row.ticket_id, "status": row.status, "next_check": row.next_check}
    store = control_store()
    if getattr(store, "mock_mode", False):
        raise RuntimeError("pending delivery requires the durable control store")
    ticket = next((t for t in store.list_tickets() if t.get("id") == row.ticket_id), None)
    if ticket is None:
        raise RuntimeError(f"Ticket {row.ticket_id} absent from durable queue")
    from core.line.ticket_engine import artifacts_root
    release = artifacts_root() / row.ticket_id / ".darkfac" / "runs" / row.ticket_id / "deploy_report.json"
    report = json.loads(release.read_text(encoding="utf-8")) if release.is_file() else {}
    merge_path = release.with_name("merge.json")
    if not report and ticket.get("status") == "completed" and merge_path.is_file():
        report = {"sha": json.loads(merge_path.read_text(encoding="utf-8")).get("merge_sha"), "deployed": False}
    if report.get("sha") and not report.get("deployed"):
        try:
            merge = json.loads(release.with_name("merge.json").read_text(encoding="utf-8"))
            validation = json.loads(release.with_name("validation.json").read_text(encoding="utf-8"))
            review = json.loads(release.with_name("review_state.json").read_text(encoding="utf-8"))["rounds"][-1]
            execution = json.loads(release.with_name("review_execution.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            row.status, row.cause = "blocked", "deploy_evidence_missing"
            _save(path, row)
            return {"ticket": row.ticket_id, "status": row.status, "cause": row.cause}
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
            return {"ticket": row.ticket_id, "status": row.status, "cause": row.cause}
        return {"ticket": row.ticket_id, "status": "merged_awaiting_deploy", "sha": report["sha"]}
    if ticket.get("status") in {"completed", "cancelled", "canceled"}:
        if ticket.get("status") == "completed" and (
                not release.is_file() or not report.get("deployed") or not report.get("sha")):
            row.status, row.cause = "blocked", "deploy_evidence_missing"
            _save(path, row)
            return {"ticket": row.ticket_id, "status": row.status, "cause": row.cause}
        path.unlink()
        return {"ticket": row.ticket_id, "status": "terminal"}
    if ticket.get("status") == "waiting_human":
        if not _release_technical_human_wait(store, row, ticket):
            return {"ticket": row.ticket_id, "status": "waiting_human"}
        _save(path, row)
    allowed = {Path(w["worktree"]).resolve() for w in list_worktrees(repo_root)}
    worktree = Path(row.worktree).resolve()
    if worktree not in allowed:
        raise RuntimeError(f"Worktree not registered for {row.ticket_id}")
    row.next_check = now + RECHECK_SECONDS
    _save(path, row)  # Backoff survives network/probe errors and service restart.
    if _route(row) is None:
        if row.stage == "review" and not row.implementing_harness:
            row.status, row.cause = "blocked", "implementing_family_unknown"
            _save(path, row)
            return {"ticket": row.ticket_id, "status": row.status, "cause": row.cause}
        return {"ticket": row.ticket_id, "status": "waiting_route", "next_check": row.next_check}
    env = dict(os.environ, DARKFAC_STATE_ROOT=str(state_root().resolve()))
    env.pop("DARKFAC_OPERATING_HARNESS", None)
    cmd = [sys.executable, str(repo_root / "run_ticket.py")]
    cmd += (["--resume-delivery", row.ticket_id, "--worktree", str(worktree)] if row.resume_delivery
            else [row.ticket_id])
    log_path = path.with_suffix(".log")
    with log_path.open("ab") as log:
        child = subprocess.Popen(cmd, cwd=repo_root, env=env, stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    row.pid, row.status = child.pid, "running"
    row.started_at = time.time()
    _children[child.pid] = child
    _save(path, row)
    return {"ticket": row.ticket_id, "status": "running", "pid": child.pid}


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
                    result = _tick_row(path, repo_root, time.time() if now is None else now)
                except Exception as exc:
                    logger.warning("Resume check failed for %s: %s", path.name, type(exc).__name__)
                    result = {"ticket": path.stem, "status": "check_failed", "cause": type(exc).__name__}
                results.append(result)
                if result["status"] in {"running", "merged_awaiting_deploy"}:
                    break
    except DemandsLockTimeout:
        return [{"status": "dispatcher_busy"}]
    return results
