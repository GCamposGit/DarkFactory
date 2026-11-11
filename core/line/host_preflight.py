"""Host preflight before a manual run reserves the line (USR-486 T2).

Checks ``gh auth status`` with the active-PR classifier, then the auth of the
claude, codex and grok CLIs. A missing or unusable ``gh``, a requested harness
that cannot authenticate, or no usable harness at all refuses before
``line_lock.guard``. Claude and Grok auth gaps stay on the report when another
CLI can still run, because those sessions are what the quota reader uses.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

from core.line import active_pull_request as gh_lookup

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 20
GH_AUTH_STATUS_ARGV: tuple[str, ...] = ("gh", "auth", "status")
GH_AUTH_STATUS_TIMEOUT_MESSAGE = "gh auth status timed out; host preflight cannot verify GitHub"
MANUAL_HARNESSES: tuple[str, ...] = ("claude", "codex", "grok")
NO_USABLE_HARNESS = "no_usable_harness"

_LOGGED_OUT = ("not logged in", "not signed in", "logged out")


@dataclass(frozen=True)
class _HarnessProbe:
    name: str
    argv: tuple[str, ...]
    missing_command: str
    unauthenticated_command: str
    timeout_command: str
    quota_gap: bool


_HARNESSES: tuple[_HarnessProbe, ...] = (
    _HarnessProbe(
        "claude",
        ("claude", "auth", "status", "--json"),
        "npm install -g @anthropic-ai/claude-code",
        "claude auth login",
        "claude auth status --json",
        True,
    ),
    _HarnessProbe(
        "codex",
        ("codex", "login", "status"),
        "npm install -g @openai/codex",
        "codex login",
        "codex login status",
        False,
    ),
    _HarnessProbe(
        "grok",
        ("grok", "models"),
        "npm install -g @xai-official/grok",
        "grok login",
        "grok models",
        True,
    ),
)


@dataclass(frozen=True)
class HostGap:
    """One CLI that cannot authenticate while another harness can still run."""

    harness: str
    cause: str
    fix_command: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "harness": self.harness,
            "cause": self.cause,
            "fix_command": self.fix_command,
            "message": self.message,
        }


@dataclass(frozen=True)
class HostPreflightReport:
    """Refusal (``ok`` false) or a pass that may still list quota gaps."""

    ok: bool
    cause: str | None
    fix_command: str | None
    message: str
    gaps: tuple[HostGap, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "cause": self.cause,
            "fix_command": self.fix_command,
            "message": self.message,
            "gaps": [gap.to_dict() for gap in self.gaps],
        }


@dataclass(frozen=True)
class _CliFailure:
    cause: str
    fix_command: str
    message: str


def default_runner(argv: list[str], **kwargs: Any) -> Any:
    """Run one CLI probe. Tests replace this or pass ``run`` and never call it."""
    return subprocess.run(argv, **kwargs)


def _subprocess_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": PROBE_TIMEOUT_S,
        "check": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def _invoke(runner: Callable[..., Any], argv: tuple[str, ...]) -> Any:
    try:
        return runner(list(argv), **_subprocess_kwargs())
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return exc


def _logged_out(result: Any) -> bool:
    combined = f"{getattr(result, 'stderr', '') or ''}\n{getattr(result, 'stdout', '') or ''}".casefold()
    return any(marker in combined for marker in _LOGGED_OUT)


def _claude_authenticated(result: Any) -> bool:
    raw = str(getattr(result, "stdout", "") or "").strip()
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("loggedIn") or payload.get("email"))


def _authenticated(spec: _HarnessProbe, result: Any) -> bool:
    if getattr(result, "returncode", 1) != 0 or _logged_out(result):
        return False
    if spec.name == "claude":
        return _claude_authenticated(result)
    return True


def _probe_harness(spec: _HarnessProbe, outcome: Any) -> _CliFailure | None:
    if isinstance(outcome, FileNotFoundError):
        return _CliFailure(
            f"{spec.name}_missing",
            spec.missing_command,
            f"{spec.name} nao esta instalado ou nao esta no PATH",
        )
    if isinstance(outcome, subprocess.TimeoutExpired):
        return _CliFailure(
            f"{spec.name}_timeout",
            spec.timeout_command,
            f"{spec.name} nao respondeu a tempo",
        )
    if isinstance(outcome, OSError):
        return _CliFailure(
            f"{spec.name}_unavailable",
            spec.timeout_command,
            f"{spec.name} nao pode ser executado ({type(outcome).__name__})",
        )
    if _authenticated(spec, outcome):
        return None
    if spec.quota_gap:
        message = f"{spec.name} sem autenticacao; a leitura de cota nao acontece"
    else:
        message = f"{spec.name} sem autenticacao"
    return _CliFailure(f"{spec.name}_unauthenticated", spec.unauthenticated_command, message)


def _gap_for(spec: _HarnessProbe, failure: _CliFailure) -> HostGap:
    cause = failure.cause
    if spec.quota_gap and failure.cause.endswith("_unauthenticated"):
        cause = "quota_unread"
    return HostGap(harness=spec.name, cause=cause, fix_command=failure.fix_command, message=failure.message)


def _refuse(
    cause: str,
    fix_command: str,
    message: str,
    gaps: tuple[HostGap, ...] = (),
) -> HostPreflightReport:
    logger.warning("host preflight refused cause=%s fix_command=%s", cause, fix_command)
    return HostPreflightReport(ok=False, cause=cause, fix_command=fix_command, message=message, gaps=gaps)


def _gh_report(outcome: Any) -> HostPreflightReport | None:
    classified = gh_lookup.classify_gh_call(
        outcome,
        repo="",
        timeout_message=GH_AUTH_STATUS_TIMEOUT_MESSAGE,
        github_fix_command=gh_lookup.GH_AUTH_STATUS_COMMAND,
    )
    if classified is None:
        return None
    logger.warning(
        "host preflight refused cause=%s fix_command=%s",
        classified.cause,
        classified.fix_command,
    )
    return HostPreflightReport(
        ok=False,
        cause=classified.cause,
        fix_command=classified.fix_command,
        message=str(classified),
    )


def _other_gaps(
    failures: dict[str, _CliFailure],
    specs: dict[str, _HarnessProbe],
    *,
    skip: str | None = None,
) -> tuple[HostGap, ...]:
    gaps: list[HostGap] = []
    for name in MANUAL_HARNESSES:
        if name == skip or name not in failures:
            continue
        gaps.append(_gap_for(specs[name], failures[name]))
    return tuple(gaps)


def _decide(
    requested: str | None,
    failures: dict[str, _CliFailure],
    specs: dict[str, _HarnessProbe],
) -> HostPreflightReport:
    usable = [name for name in MANUAL_HARNESSES if name not in failures]
    if requested in MANUAL_HARNESSES and requested in failures:
        failure = failures[requested]
        return _refuse(failure.cause, failure.fix_command, failure.message, _other_gaps(failures, specs, skip=requested))
    if not usable:
        gaps = _other_gaps(failures, specs)
        command = " | ".join(gap.fix_command for gap in gaps)
        return _refuse(
            NO_USABLE_HARNESS,
            command,
            "nenhum de claude, codex e grok esta utilizavel",
            gaps,
        )
    gaps = _other_gaps(failures, specs)
    for gap in gaps:
        if gap.cause == "quota_unread":
            logger.warning(
                "host preflight quota gap harness=%s cause=%s fix_command=%s",
                gap.harness,
                gap.cause,
                gap.fix_command,
            )
    return HostPreflightReport(ok=True, cause=None, fix_command=None, message="", gaps=gaps)


def run_host_preflight(
    *,
    requested_harness: str | None = None,
    run: Callable[..., Any] | None = None,
) -> HostPreflightReport:
    """Probe gh and the three manual harness CLIs. ``run`` replaces the process runner."""
    runner = run if run is not None else default_runner
    requested = requested_harness.lower().strip() if isinstance(requested_harness, str) and requested_harness.strip() else None
    gh_failure = _gh_report(_invoke(runner, GH_AUTH_STATUS_ARGV))
    if gh_failure is not None:
        return gh_failure
    specs = {spec.name: spec for spec in _HARNESSES}
    failures: dict[str, _CliFailure] = {}
    for spec in _HARNESSES:
        failure = _probe_harness(spec, _invoke(runner, spec.argv))
        if failure is not None:
            failures[spec.name] = failure
    return _decide(requested, failures, specs)


__all__ = [
    "GH_AUTH_STATUS_ARGV",
    "HostGap",
    "HostPreflightReport",
    "MANUAL_HARNESSES",
    "NO_USABLE_HARNESS",
    "default_runner",
    "run_host_preflight",
]
