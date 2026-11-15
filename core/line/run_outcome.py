"""Stable per-attempt outcomes, independent of disposable stage context directories."""
from __future__ import annotations

import contextlib
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable
import uuid

from pydantic import BaseModel

from core.line.failure_taxonomy import canonical_cause, classify, FailureClass
from core.paths import state_root

logger = logging.getLogger(__name__)
_BANNERS = ("quota_usage", "quota summary", "quota report", "resumo de cota", "telemetria de cotas",
            "cotas dos", "saldo das", "headroom")


class RunOutcome(BaseModel):
    ticket: str
    run_id: str
    exit_code: int
    cause: str
    cause_class: FailureClass
    stage: str
    detail: str
    ts: str


def outcome_path(ticket: str) -> Path:
    """One stable outcome per ticket, replaced atomically after each attempt."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", ticket):
        raise ValueError("Invalid outcome ticket")
    return state_root() / "runs" / "outcomes" / f"{ticket}.json"


def read_outcome(ticket: str, run_id: str | None) -> RunOutcome | None:
    """Never attribute an older run's diagnosis to the current child."""
    try:
        result = RunOutcome.model_validate_json(outcome_path(ticket).read_text(encoding="utf-8"))
        return result if run_id and result.run_id == run_id else None
    except (OSError, ValueError):
        return None


def ci_recovery_path(ticket: str) -> Path:
    """CI repair allowance survives context reset and launcher restarts."""
    return outcome_path(ticket).parent.parent / "ci_recovery" / f"{ticket}.json"


def main_sha(repo: Path) -> str:
    """Read origin/main locally; dispatch must not fetch to inspect a retry budget."""
    try:
        result = subprocess.run(["git", "rev-parse", "origin/main"], cwd=repo,
                                capture_output=True, text=True, encoding="utf-8", timeout=5, check=False)
        value = result.stdout.strip()
        return value if result.returncode == 0 and re.fullmatch(r"[a-fA-F0-9]{40,64}", value) else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def error_detail(text: str, fallback: str = "unknown") -> str:
    """Keep the actual error line, redacted, excluding quota/accounting banners."""
    from core.line.agent_cli import redact_secrets

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    errors = [line for line in lines if any(marker in line.casefold() for marker in
              ("error", "erro", "failed", "failure", "timeout", "denied", "exception", "recusa", "aguardando", "exceeded"))]
    clean = [line for line in (errors or lines) if not any(marker in line.casefold() for marker in _BANNERS)]
    return redact_secrets((clean[0] if clean else fallback))[:1000]


def merged(ticket: str, project: str) -> None:
    """Clear both repair allowances only after a validated merge receipt."""
    from core.line.admission_store import AdmissionStore
    AdmissionStore(state_root() / "admission.sqlite3").reset_budget(project, ticket)
    ci_recovery_path(ticket).unlink(missing_ok=True)


def record_failure(ticket: str, project: str, cause: str, detail: str, *, run_id: str | None = None,
                   log_path: str = "", repo: Path | None = None) -> dict[str, Any]:
    """Shared durable accounting, also used when a launcher dies without an outcome."""
    from core.line.admission_store import AdmissionStore

    attempt = _current.get()
    identity = run_id or (attempt.run_id if attempt else uuid.uuid4().hex)
    root = repo or Path(__file__).resolve().parents[2]
    return AdmissionStore(state_root() / "admission.sqlite3").record_attempt(
        project, ticket, cause, run_id=identity, main_sha=main_sha(root),
        detail=error_detail(detail, cause),
        log_path=log_path or os.environ.get("DARKFAC_RUN_LOG") or str(outcome_path(ticket).with_suffix(".log")),
    )


class _Tee:
    def __init__(self, stream: Any, attempt: "_Attempt") -> None:
        self.stream, self.attempt = stream, attempt

    def write(self, value: str) -> int:
        self.attempt.output = (self.attempt.output + value)[-131072:]
        return self.stream.write(value)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


class _Attempt:
    def __init__(self) -> None:
        self.ticket = ""
        self.project = "darkfac"
        self.run_id = os.environ.get("DARKFAC_RUN_ID") or uuid.uuid4().hex
        self.output = ""
        self.progress: Any = None
        self.enabled = True

    def finish(self, code: int, result: Any) -> int:
        if not self.ticket:
            return code
        payload = result if isinstance(result, dict) else {}
        progress = self.progress
        events = getattr(progress, "events", [])
        failed = [event for event in events if event.cause_code and event.status in {"failed", "skipped"}
                  and event.phase != "run"]
        event = failed[-1] if failed else None
        cause = payload.get("cause") or getattr(progress, "_outcome_cause", None)
        message = payload.get("error") or getattr(progress, "_outcome_message", "")
        if event is not None and (not cause or str(cause).startswith("exit_")):
            cause, message = event.cause_code, event.message
        if not cause:
            cause = diagnostic_cause(self.output, code)
        if canonical_cause(cause) == "unknown":
            inferred = canonical_cause(message)
            if classify(inferred) == "infra":
                cause = inferred
        cause = canonical_cause(cause) if code else "success"
        category = classify(cause)
        code = 75 if code and category == "infra" else code
        if not self.enabled:
            return code
        detail = error_detail(message or self.output, cause)
        previous = read_outcome(self.ticket, self.run_id)
        if code and previous is not None and previous.exit_code:
            cause, category, detail, code = previous.cause, previous.cause_class, previous.detail, previous.exit_code
        stage_match = re.search(r"estagio\s+([A-Z_]+)", str(message))
        stage = stage_match.group(1).lower() if stage_match else event.phase if event else "preflight"
        outcome = RunOutcome(ticket=self.ticket, run_id=self.run_id, exit_code=code, cause=cause,
                             cause_class=category, stage=previous.stage if previous else stage,
                             detail=detail, ts=datetime.now(timezone.utc).isoformat())
        path = outcome_path(self.ticket)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(outcome.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, path)
        if not os.environ.get("DARKFAC_RUN_LOG"):
            from core.line.agent_cli import redact_secrets
            path.with_suffix(".log").write_text(f"{cause}: {detail}\n" + redact_secrets(self.output), encoding="utf-8")
        if code:
            record_failure(self.ticket, self.project, cause, detail, run_id=self.run_id)
        elif "[DELIVERY_MERGED]" in self.output or payload.get("status") == "merged_awaiting_deploy":
            from core.line.admission_store import AdmissionStore
            AdmissionStore(state_root() / "admission.sqlite3").reset_budget(self.project, self.ticket)
        return code


_current: ContextVar[_Attempt | None] = ContextVar("failure_attempt", default=None)


def diagnostic_cause(output: str, code: int) -> str:
    """Fallback for early refusals, before progress exists."""
    official = re.findall(r"\[ERRO NA ENTREGA\][^\n]*?causa:\s*([a-z0-9_]+)", output)
    if official:
        return official[-1]
    for line in output.splitlines():
        if any(marker in line.casefold() for marker in _BANNERS):
            continue
        cause = canonical_cause(line)
        if cause not in {"unknown", "self_modification"}:
            return cause
    return "infra" if code == 75 else "unknown"


def bind_attempt(ticket: str, project: str = "darkfac", *, run_id: str | None = None,
                 progress: Any = None, enabled: bool | None = None) -> None:
    """Bind early CLI refusals and subsequent stage progress to the same attempt."""
    attempt = _current.get()
    if attempt is not None:
        attempt.ticket, attempt.project = ticket, project
        if enabled is not None:
            attempt.enabled = enabled
        if run_id:
            attempt.run_id = run_id
        if progress is not None:
            attempt.progress = progress


def current_run_id() -> str | None:
    """Identity inherited by delivery children, including manual launcher calls."""
    attempt = _current.get()
    return attempt.run_id if attempt else os.environ.get("DARKFAC_RUN_ID")


def current_project() -> str:
    """Keep pending resumes in the same project budget as their launcher."""
    attempt = _current.get()
    return attempt.project if attempt else "darkfac"


def tracked(function: Callable[..., Any]) -> Callable[..., Any]:
    """Cover every return and exception of both public launcher entry points."""
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if _current.get() is not None:
            return function(*args, **kwargs)
        attempt = _Attempt()
        if function.__name__ == "resume_delivery":
            attempt.ticket = str(kwargs.get("ticket_id") or args[0])
        token = _current.set(attempt)
        try:
            with contextlib.redirect_stdout(_Tee(sys.stdout, attempt)), contextlib.redirect_stderr(_Tee(sys.stderr, attempt)):
                try:
                    result = function(*args, **kwargs)
                except Exception as exc:
                    logger.exception("Launcher attempt failed")
                    attempt.output += f"\n{type(exc).__name__}: {exc}"
                    code = attempt.finish(1, {"cause": canonical_cause(f"{type(exc).__name__}: {exc}"),
                                              "error": str(exc)})
                    return code
                code = int(result.get("exit_code", 1)) if isinstance(result, dict) else int(result)
                code = attempt.finish(code, result)
                return {**result, "exit_code": code} if isinstance(result, dict) else code
        finally:
            _current.reset(token)
    return wrapped
