"""Human-in-the-loop channel for the DarkFac production line (HF-27-08 item 4).

A `HumanRequest` is the line's uniform shape for anything a stage handler
cannot resolve itself: a secret, an account/portal action, a repo setting
change, commercial acceptance, or generic infra work. It captures just
enough to (a) tell the owner what to do, screen by screen, and (b) let the
factory resume automatically once it is done -- either because the owner
answered (grill, commercial_acceptance) or because a deterministic probe
came back green (secret/account/repo_setting/infra).

Persistence: the request is written to
`.darkfac/runs/<run_id>/HUMAN_REQUEST.json` on the run's own branch (the
common line contract in `docs/handoffs/production-line/INDEX.md` -- state
lives on the branch, never in process memory) via `core.line.workspace`,
and also mirrored into a `ManualDependency` (`core.workflow.contracts`) for
compatibility with the pre-existing HF-08-05 `core.workflow.manual_resolution`
probe/resume machinery. `core.workflow.control_store.ControlStore.jobs`
has no dedicated table for `ManualDependency`/`ManualStep` records (that
model predates the line and is keyed by `WorkflowState`, whose values do
not correspond 1:1 to line stage names), so the ManualDependency object is
kept as a courtesy conversion for any caller that wants the HF-08-05 shape;
the line's own resume path (`resume_blocked_job` below) works directly off
the JobKey the blocked job actually has, via `ControlStore.find_job` +
`ControlStore.resume_job` (both added in this ticket).

Only these four kinds of message ever reach the owner bot (per the
HF-27-08 handoff, section 4): (a) grill questions -- already sent by
`stage_grill._notify_owner`, never duplicated here; (b) a `HumanRequest`
(this module); (c) the delivered summary after `build_deploy` success; (d)
a terminal-failure summary with a suggested next step. (c) and (d) are
plain `notify()` calls a caller makes directly from the worker/dispatch
loop; this module only owns (b) plus the resume mechanics shared by all
four kinds of wait.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

from core.line import workspace
from core.projects.models import ProjectDescriptor
from core.workflow.control_contracts import JobKey
from core.workflow.control_store import ControlStore

logger = logging.getLogger(__name__)

HumanRequestKind = Literal["grill", "secret", "account", "repo_setting", "commercial_acceptance", "infra"]

_REQUEST_FILE = "HUMAN_REQUEST.json"


class HumanRequest(BaseModel):
    """A single stage's ask for human help, per HF-27-08 section 4."""

    kind: HumanRequestKind
    run_id: str
    blocking_stage: str
    guide_md: str = Field(min_length=1)
    probe_cmd: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


NotifySender = Callable[[str], bool]


def _win_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


# --------------------------------------------------------------------------
# Persistence on the run's branch
# --------------------------------------------------------------------------


def save_request(project: ProjectDescriptor, request: HumanRequest) -> Path:
    """Write `HUMAN_REQUEST.json` to `.darkfac/runs/<run_id>/` and commit+push it."""
    ws = workspace.checkout(project, request.run_id)
    workspace.write_context(ws, _REQUEST_FILE, request.model_dump_json(indent=2))
    workspace.commit(ws, f"chore(line): human request ({request.kind})", f"{request.run_id}:human:{request.blocking_stage}")
    workspace.push(ws)
    return workspace.context_dir(ws) / _REQUEST_FILE


def load_request(project: ProjectDescriptor, run_id: str) -> Optional[HumanRequest]:
    ws = workspace.checkout(project, run_id)
    path = workspace.context_dir(ws) / _REQUEST_FILE
    if not path.is_file():
        return None
    try:
        return HumanRequest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Failed to load human request for run %s: %s", run_id, exc)
        return None


# --------------------------------------------------------------------------
# Owner notification
# --------------------------------------------------------------------------


def _default_sender() -> Optional[NotifySender]:
    try:
        from core.notifications.models import AlertCategory, AlertSeverity
        from core.notifications.service import NotificationService
    except Exception:  # pragma: no cover - defensive
        return None

    service = NotificationService()

    def _send(text: str) -> bool:
        event = service.notify(
            category=AlertCategory.WORKFLOW_GATE,
            severity=AlertSeverity.WARNING,
            title="Ação humana necessária",
            message=text,
            force=True,
        )
        return event is not None

    return _send


def notify_human_request(
    request: HumanRequest,
    *,
    send: Optional[NotifySender] = None,
) -> bool:
    """Send the HumanRequest's guide to the owner bot. Best-effort: never raises."""
    sender = send if send is not None else _default_sender()
    if sender is None:
        logger.warning("No notification sender available for human request (run %s)", request.run_id)
        return False
    text = (
        f"[{request.kind}] run {request.run_id} bloqueado em '{request.blocking_stage}':\n\n"
        f"{request.guide_md}"
    )
    try:
        return bool(sender(text))
    except Exception as exc:  # pragma: no cover - notification must never break the stage
        logger.warning("Failed to notify human request for run %s: %s", request.run_id, exc)
        return False


def request_human_help(
    project: ProjectDescriptor,
    request: HumanRequest,
    *,
    send: Optional[NotifySender] = None,
) -> None:
    """Persist + notify a HumanRequest. Called by a stage handler once, when it first blocks."""
    save_request(project, request)
    notify_human_request(request, send=send)


# --------------------------------------------------------------------------
# Probe + resume
# --------------------------------------------------------------------------


def run_probe(probe_cmd: str, *, timeout_s: int = 60) -> bool:
    """Run `probe_cmd` (a shell command string) and return True on exit code 0."""
    try:
        proc = subprocess.run(
            probe_cmd,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            **_win_kwargs(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.info("Human-request probe failed to run (%s): %s", probe_cmd, exc)
        return False
    return proc.returncode == 0


def resume_blocked_job(
    store: ControlStore, run_id: str, blocking_stage: str, *, now: Optional[datetime] = None
) -> bool:
    """Resolve the exact waiting job for `(run_id, blocking_stage)` and resume only it.

    Never touches sibling jobs: `ControlStore.find_job` looks up the job's
    real `iteration`, and `ControlStore.resume_job` matches on the full
    `JobKey` plus a `status IN ('waiting_human', 'waiting_dependency')`
    guard, so a stale or already-resumed request is a safe no-op.
    """
    job_key = store.find_job(run_id, blocking_stage, status="waiting_human")
    if job_key is None:
        job_key = store.find_job(run_id, blocking_stage, status="waiting_dependency")
    if job_key is None:
        logger.info("No waiting job found for run %s stage %s; nothing to resume", run_id, blocking_stage)
        return False
    return store.resume_job(job_key, now or datetime.now(UTC))


def probe_and_resume(
    store: ControlStore,
    project: ProjectDescriptor,
    run_id: str,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Re-run a saved HumanRequest's `probe_cmd` (if any) and resume on success.

    Used for `secret`/`account`/`repo_setting`/`infra` kinds, which resolve
    by a deterministic check rather than an owner answer. A request with no
    `probe_cmd` resumes unconditionally (the owner's own confirmation, via
    whatever channel delivered it, is the "probe").
    """
    request = load_request(project, run_id)
    if request is None:
        return False
    if request.probe_cmd and not run_probe(request.probe_cmd):
        return False
    return resume_blocked_job(store, run_id, request.blocking_stage, now=now)


# --------------------------------------------------------------------------
# Telegram callback wiring (HF-27-08 item F)
# --------------------------------------------------------------------------


def build_telegram_line_grill_handler(
    store: ControlStore, *, project_resolver: Optional[Callable[[str], Optional[ProjectDescriptor]]] = None
) -> Callable[[str, str, str, int], dict[str, Any]]:
    """`TelegramGateway.line_grill_handler` for `cb:grill:<run_id>#<question_id>:<index>`.

    Resolves the project from the run's grill job (`ControlStore.find_job`,
    `ticket_id` is the project id for line runs), calls
    `stage_grill.submit_grill_answers`, then resumes only the grill job --
    per stage_grill's own docstring, the next `run_grill()` reconciles
    partial answers (not every question needs to be answered before the
    job resumes; a still-incomplete set returns `waiting_human` again).
    """
    from core.line.bindings import default_project_resolver

    resolver = project_resolver or default_project_resolver()

    def _handler(run_id: str, question_id: str, index: str, user_id: int) -> dict[str, Any]:
        del user_id
        from core.line import stage_grill

        job_key = store.find_job(run_id, "grill")
        if job_key is None:
            return {"resumed": False}
        project = resolver(job_key.ticket_id)
        if project is None:
            return {"resumed": False}
        submitted = stage_grill.submit_grill_answers(project, run_id, {question_id: index})
        if not submitted:
            return {"resumed": False}
        resumed = resume_blocked_job(store, run_id, "grill")
        return {"resumed": resumed}

    return _handler


def build_telegram_commercial_acceptance_handler(
    store: ControlStore, *, project_resolver: Optional[Callable[[str], Optional[ProjectDescriptor]]] = None
) -> Callable[[str, int], dict[str, Any]]:
    """`TelegramGateway.commercial_acceptance_handler` for `cb:accept:<run_id>` (D-g).

    Resolves the project and `merge_sha` from the waiting `build_deploy`
    job's `input_refs` (`[pr_url, merge_sha]`, per `stage_release`'s own
    docstring), records the acceptance, then resumes only that job.
    """
    from core.line.bindings import default_project_resolver

    resolver = project_resolver or default_project_resolver()

    def _handler(run_id: str, user_id: int) -> dict[str, Any]:
        del user_id
        from core.line.stage_release import default_state_store, record_commercial_acceptance

        job_key = store.find_job(run_id, "build_deploy", status="waiting_human")
        if job_key is None:
            return {"resumed": False}
        project = resolver(job_key.ticket_id)
        if project is None:
            return {"resumed": False}
        input_refs = store.get_latest_success_output_refs(run_id, exclude_stage="build_deploy")
        sha = input_refs[-1] if input_refs else ""
        if not sha:
            return {"resumed": False}
        record_commercial_acceptance(default_state_store(project), project.id, run_id, sha)
        resumed = resume_blocked_job(store, run_id, "build_deploy")
        return {"resumed": resumed, "sha": sha}

    return _handler


__all__ = [
    "HumanRequest",
    "HumanRequestKind",
    "save_request",
    "load_request",
    "notify_human_request",
    "request_human_help",
    "run_probe",
    "resume_blocked_job",
    "probe_and_resume",
    "build_telegram_line_grill_handler",
    "build_telegram_commercial_acceptance_handler",
]
