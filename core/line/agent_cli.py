"""Real AgentCLI runner for the DarkFac production line (HF-27-03).

`run_agent()` invokes an actual coding-agent CLI (Claude Code, Codex,
Grok Build, Antigravity) or the OpenRouter gateway, in write mode (the
agent may edit files under `req.cwd`) or read mode (review/analysis
only), and returns a normalized `AgentResult` with measured cost/usage
when the underlying CLI reports it.

Design notes:
- Subprocesses are always invoked with argument lists (never
  `shell=True`); the prompt is sent via stdin for `claude` and `codex`,
  which both support it, mirroring what a human would pipe in.
- Binary discovery is reused from `core.harness.remote_worker` so both
  the line and the ad hoc HTTP worker agree on where each CLI lives.
- Error classification is intentionally simple regex matching over
  combined stdout/stderr/JSON-error text, per the HF-27-03 spec:
  rate limit/usage limit/429 -> rate_limited; login/unauthorized/401/
  expired -> auth_expired; missing binary -> not_installed;
  subprocess.TimeoutExpired -> timeout; anything else -> crash.
- Capabilities are declared, never assumed: `HARNESS_CAPABILITIES` is the
  single table of which harness can run which mode (`read`/`write`), so the
  router (`core.line.routing.pick`) can refuse to elect a harness that would
  only fail the stage with `unsupported_mode`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from core.paths import project_root, state_root

from pydantic import BaseModel, Field, field_validator

from core.harness.remote_worker import (
    find_antigravity_binary,
    find_claude_binary,
    find_codex_binary,
    find_grok_binary,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

ErrorKind = Optional[
    Literal[
        "rate_limited",
        "auth_expired",
        "not_installed",
        "timeout",
        "crash",
        # A harness call that exited "ok" but produced no text (e.g. an
        # OpenRouter reasoning model returning `content: null`): a classified,
        # retryable failure instead of a pydantic crash downstream.
        "empty_output",
        # No route at all: no subscription harness is authenticated on this
        # worker and OpenRouter is unavailable for the stage.
        "no_authenticated_harness",
        # The harness exists and is installed but does not implement the
        # requested mode (e.g. `write` on a read-only harness). Distinct from
        # `not_installed` so a capability gap is never mistaken for a missing
        # binary.
        "unsupported_mode",
    ]
]

AgentMode = Literal["write", "read"]

# Declared capabilities per harness: `write` only where the runner below really
# implements a mode that lets the agent edit files under `req.cwd`.
# - claude: `--permission-mode bypassPermissions` (see `build_claude_argv`).
# - codex: `--sandbox workspace-write` or the documented bypass (`build_codex_argv`).
# - grok: `--permission-mode auto` plus `--prompt-file`/`--cwd` (see `build_grok_write_argv`).
# - antigravity: only one-shot conversation calls that return text; it has no reliable headless
#   write mode on this host.
# - openrouter: a plain chat-completion gateway, it cannot touch the filesystem.
HARNESS_CAPABILITIES: dict[str, frozenset[str]] = {
    "claude": frozenset({"read", "write"}),
    "codex": frozenset({"read", "write"}),
    "grok": frozenset({"read", "write"}),
    "antigravity": frozenset({"read"}),
    "openrouter": frozenset({"read"}),
}


HEADLESS_DEVELOPMENT_PREAMBLE = (
    "CONTEXTO DE EXECUCAO HEADLESS: O orquestrador/launcher ja fez o preflight de cota "
    "e roteou esta tarefa para o harness {harness}{headroom}. A regra de bloqueio de cota "
    "do chat interativo nao se aplica a este agente headless roteado. Nao repita o preflight "
    "nem peca override. Se nao conseguir implementar por qualquer motivo, diga isso "
    "claramente: a fabrica trata uma execucao sem alteracoes de implementacao como falha.\n\n"
)


def headless_development_preamble(harness: str, headroom: Optional[float] = None) -> str:
    """Explain the already completed routing decision to a headless development agent."""
    quota = f" (headroom {headroom:.1f}%)" if headroom is not None else ""
    return HEADLESS_DEVELOPMENT_PREAMBLE.format(harness=harness, headroom=quota)


def supports(harness: str, mode: str) -> bool:
    """True when `harness` declares `mode`. An unknown harness supports nothing (fail-closed)."""
    return mode in HARNESS_CAPABILITIES.get(harness.lower().strip(), frozenset())


class AgentRequest(BaseModel):
    """A single agent-CLI invocation request."""

    prompt: str
    cwd: Path
    mode: Literal["write", "read"]
    harness: str
    model: Optional[str] = None
    effort: Optional[str] = None
    timeout_s: int = 1800
    max_turns: Optional[int] = None


class AgentResult(BaseModel):
    """Normalized outcome of an agent-CLI invocation."""

    ok: bool
    text: str
    harness: str
    model: Optional[str] = None
    duration_s: float
    usage: Optional[dict[str, Any]] = None
    cost_usd: Optional[float] = None
    error_kind: ErrorKind = None
    reset_at: Optional[datetime] = None
    # Subprocess diagnostics (claude/codex/grok/antigravity). `stderr_tail` is the redacted last
    # ~2000 characters of stderr; both stay at their defaults for non-subprocess results.
    exit_code: Optional[int] = None
    stderr_tail: str = ""

    @field_validator("text", mode="before")
    @classmethod
    def _none_text_is_empty(cls, value: Any) -> Any:
        """`text=None` (e.g. a provider `content: null`) must never crash the
        contract; `run_agent` classifies an empty successful result as
        `error_kind="empty_output"`."""
        return "" if value is None else value


# --------------------------------------------------------------------------
# Secret redaction (diagnostics must never leak a token into logs / detail)
# --------------------------------------------------------------------------

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]+"),
    re.compile(r"(?i)\b(CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_API_KEY|OPENAI_API_KEY|OPENROUTER_API_KEY)\s*[=:]\s*\S+"),
)
_REDACTED = "[REDACTED]"
_ENV_SECRET_NAMES = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")


def redact_secrets(text: Optional[str]) -> str:
    """Strip token-like strings (sk-ant-..., sk-..., `Bearer ...`, `NAME=value`
    for known secret env names) and the literal value of any configured secret
    env var. Safe to call on any diagnostic text; never raises."""
    if not text:
        return ""
    out = str(text)
    for name in _ENV_SECRET_NAMES:
        value = os.environ.get(name, "")
        if len(value) >= 8:
            out = out.replace(value, _REDACTED)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    return out


def _partial_output(value: Any) -> str:
    """`TimeoutExpired.stdout/stderr` may be None, bytes or str."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


STDERR_TAIL_CHARS = 2000


def _stderr_tail(value: Any) -> str:
    """Redacted last `STDERR_TAIL_CHARS` characters of a stderr blob (str, bytes or None).

    Redaction runs before truncation so a token straddling the cut is never left half-visible.
    """
    return redact_secrets(_partial_output(value)).strip()[-STDERR_TAIL_CHARS:]


# --------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------

_RATE_LIMIT_PATTERN = re.compile(
    r"rate[\s_-]?limit|usage[\s_-]?limit|\b429\b"
    # Claude Code subscription messages: "You've hit your session limit", "weekly limit reached",
    # "Opus limit reached", "5-hour limit reached".
    r"|\b(?:session|weekly|monthly|daily|hourly|\d+[\s-]?hour|opus|sonnet)\s+limit"
    r"|hit\s+your\s+(?:\w+\s+){0,2}limit"
    r"|\blimit\s+(?:reached|exceeded|hit)\b",
    re.IGNORECASE,
)
_AUTH_PATTERN = re.compile(r"\blogin\b|unauthorized|\b401\b|\bexpired\b", re.IGNORECASE)
_RESET_ISO_PATTERN = re.compile(
    r'reset\w*["\']?\s*[:=]\s*["\']?(\d{4}-\d{2}-\d{2}T[0-9:.,+Zz-]+)',
    re.IGNORECASE,
)
_RESET_RELATIVE_PATTERN = re.compile(
    r"(?:reset[s]?|retry|try again|available again)(?:[^.\n\d]{0,40}?)(\d+)\s*"
    r"(hour|hr|h|minute|min|m|second|sec|s)s?\b",
    re.IGNORECASE,
)


# "resets 1:50am (UTC)", "resets 13:50 (UTC)", "resets 9am (America/Sao_Paulo)", "resets Oct 3, 9am (UTC)",
# "resets Oct 3 at 9:30am (UTC)". Date optional; time is 12h (am/pm) or 24h (HH:MM).
_RESET_CLOCK_PATTERN = re.compile(
    r"\bresets?\s+(?:at\s+)?"
    r"(?:(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s*(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]\.?m\.?)?"
    r"(?:\s*\((?P<tz>[^)]{1,64})\))?",
    re.IGNORECASE,
)
_MONTHS = {
    name: index
    for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1
    )
}


def _resolve_tz(name: Optional[str]) -> Optional[Any]:
    """tzinfo for a `(UTC)` / `(America/Sao_Paulo)` suffix; None when it cannot be resolved."""
    if not name or not name.strip():
        return timezone.utc
    clean = name.strip()
    if clean.upper() in {"UTC", "GMT", "Z", "UTC+0", "UTC+00:00"}:
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(clean)
    except Exception:  # unknown zone or no tzdata on this host
        return None


def _extract_clock_reset_at(text: str, now: datetime) -> Optional[datetime]:
    """Next UTC instant matching a wall-clock reset such as `resets 1:50am (UTC)`; None if unparseable."""
    match = _RESET_CLOCK_PATTERN.search(text)
    if not match:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    ampm = (match.group("ampm") or "").lower().replace(".", "")
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    elif match.group("minute") is None:
        return None  # a bare number ("resets 3") is not a clock time
    if hour > 23 or minute > 59:
        return None
    tz = _resolve_tz(match.group("tz"))
    if tz is None:
        return None
    local_now = now.astimezone(tz)
    mon_raw = match.group("mon")
    try:
        if mon_raw:
            month = _MONTHS.get(mon_raw[:3].lower())
            if month is None:
                return None
            candidate = datetime(local_now.year, month, int(match.group("day")), hour, minute, tzinfo=tz)
            if candidate < local_now - timedelta(days=1):
                candidate = candidate.replace(year=candidate.year + 1)
        else:
            candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= local_now:
                candidate += timedelta(days=1)
    except ValueError:
        return None
    return candidate.astimezone(timezone.utc)


def _classify_error(text: str) -> ErrorKind:
    """Classify a combined stdout/stderr/error-message blob into an ErrorKind."""
    if not text:
        return "crash"
    if _RATE_LIMIT_PATTERN.search(text):
        return "rate_limited"
    if _AUTH_PATTERN.search(text):
        return "auth_expired"
    return "crash"


def _extract_reset_at(text: str, *, now: Optional[datetime] = None) -> Optional[datetime]:
    """Best-effort extraction of a quota reset time from free-form CLI output."""
    if not text:
        return None
    iso_match = _RESET_ISO_PATTERN.search(text)
    if iso_match:
        raw = iso_match.group(1).replace("Z", "+00:00").replace("z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass
    rel_match = _RESET_RELATIVE_PATTERN.search(text)
    if rel_match:
        amount = int(rel_match.group(1))
        unit = rel_match.group(2).lower()
        if unit.startswith("h"):
            delta = timedelta(hours=amount)
        elif unit.startswith("m"):
            delta = timedelta(minutes=amount)
        else:
            delta = timedelta(seconds=amount)
        return datetime.now(timezone.utc) + delta
    return _extract_clock_reset_at(text, now or datetime.now(timezone.utc))


# --------------------------------------------------------------------------
# Argv builders (shared with core.harness.remote_worker write mode)
# --------------------------------------------------------------------------


def build_claude_argv(executable: str, req: AgentRequest) -> list[str]:
    """Build the Claude Code CLI argv for read or write mode. Prompt goes on stdin."""
    argv = [executable, "-p", "--output-format", "json"]
    if req.model:
        argv += ["--model", req.model]
    permission_mode = "bypassPermissions" if req.mode == "write" else "plan"
    argv += ["--permission-mode", permission_mode]
    if req.max_turns:
        argv += ["--max-turns", str(req.max_turns)]
    return argv


CODEX_SANDBOX_ENV = "DARKFAC_CODEX_SANDBOX_MODE"
_CODEX_SANDBOX_MODES = ("auto", "danger-full-access", "bypass")


def codex_sandbox_mode() -> str:
    """`DARKFAC_CODEX_SANDBOX_MODE`: `auto` (default), `danger-full-access` or `bypass`.

    Codex's own Linux sandbox (landlock/seccomp) is unavailable inside an unprivileged container,
    so every `codex exec --sandbox ...` call can fail there. In the cloud worker the container IS
    the isolation boundary, so its compose sets `bypass`; on the Desktop/Notebook the variable is
    unset and Codex keeps sandboxing exactly as before. Unknown values fall back to `auto`.
    """
    raw = os.environ.get(CODEX_SANDBOX_ENV, "").strip().lower()
    if not raw:
        return "auto"
    if raw not in _CODEX_SANDBOX_MODES:
        logger.warning("Invalid %s=%r; using 'auto'", CODEX_SANDBOX_ENV, raw)
        return "auto"
    return raw


def build_codex_argv(executable: str, req: AgentRequest, tmp_out: Path) -> list[str]:
    """Build the Codex CLI argv for read or write mode. Prompt goes on stdin via trailing '-'.

    Flags verified against codex-cli 0.159.3 (`codex-rs/exec/src/cli.rs` and
    `codex-rs/utils/cli/src/shared_options.rs` at tag `rust-v0.159.3`):
    `auto` keeps `--sandbox workspace-write|read-only`; `danger-full-access` passes
    `--sandbox danger-full-access`; `bypass` passes `--dangerously-bypass-approvals-and-sandbox`
    (alias `--yolo`) and no `--sandbox` (the flag exists to run in an externally sandboxed
    environment).
    """
    mode = codex_sandbox_mode()
    argv = [executable, "exec"]
    if mode == "bypass":
        argv += ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        if mode == "danger-full-access":
            sandbox = "danger-full-access"
        else:
            sandbox = "workspace-write" if req.mode == "write" else "read-only"
        argv += ["--sandbox", sandbox]
    argv += ["--skip-git-repo-check", "--json"]
    if req.model:
        argv += ["-m", req.model]
    argv += ["-o", str(tmp_out), "-"]
    return argv


GROK_PERMISSION_ENV = "DARKFAC_GROK_PERMISSION_MODE"
_GROK_PERMISSION_MODES = ("default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan")
_GROK_PERMISSION_BY_LOWER = {mode.lower(): mode for mode in _GROK_PERMISSION_MODES}
GROK_DEFAULT_PERMISSION_MODE = "auto"


def grok_permission_mode() -> str:
    """`DARKFAC_GROK_PERMISSION_MODE` for Grok *write* runs: one of the six `--permission-mode` values.

    Default `auto`, the least-privileged mode that works headless (Grok Build 1.0.46, measured on this
    Windows host in throwaway git repos with `--max-turns` capped; the headless prompt never has a human
    to approve a tool call, so anything that would ask is auto-cancelled):
    - `acceptEdits`: FAILS. The very first edit is cancelled (`stopReason: "cancelled"`, exit 0, no file).
    - `dontAsk`: FAILS the same way. `default` was not run: it asks for every edit, so it cannot work headless.
    - `auto`: WORKS. Created files, edited several files, ran `python -m pytest` and `git`/`pip --dry-run`
      shell commands; nothing legitimate was blocked. A call its safety classifier refuses is reported
      to the model instead of aborting the run.
    - `bypassPermissions` (= `--always-approve`): WORKS, it is Grok's documented mode for CI/agents.
    Set `bypassPermissions` here if `auto` ever blocks a routine command; the OS sandbox (`--sandbox`)
    is Landlock/Seatbelt only, so it does not exist on Windows. Case-insensitive; an invalid value logs a
    WARNING and falls back to the default. Read mode never uses this setting.
    """
    raw = os.environ.get(GROK_PERMISSION_ENV, "").strip()
    if not raw:
        return GROK_DEFAULT_PERMISSION_MODE
    mode = _GROK_PERMISSION_BY_LOWER.get(raw.lower())
    if mode is None:
        logger.warning(
            "Invalid %s=%r (valid: %s); using %r",
            GROK_PERMISSION_ENV, raw, ", ".join(_GROK_PERMISSION_MODES), GROK_DEFAULT_PERMISSION_MODE,
        )
        return GROK_DEFAULT_PERMISSION_MODE
    return mode


def build_grok_write_argv(executable: str, req: AgentRequest, prompt_file: Path) -> list[str]:
    """Build the Grok Build argv for write mode (verified against grok 1.0.46 `--help`).

    The prompt goes through `--prompt-file` (never on the command line: Windows caps it at ~32k
    characters and a long ticket prompt would also show up in process listings), the working directory
    is passed both as `--cwd` and as the subprocess cwd, and `--output-format json` yields one JSON
    object with the final text, `usage` and `total_cost_usd` (see `_run_grok_write`).
    """
    argv = [
        executable, "--prompt-file", str(prompt_file), "--cwd", str(req.cwd),
        "--output-format", "json", "--permission-mode", grok_permission_mode(),
    ]
    if req.model:
        argv += ["-m", req.model]
    if req.max_turns:
        argv += ["--max-turns", str(req.max_turns)]
    return argv


def _win_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


_ORIGINAL_SUBPROCESS_RUN = subprocess.run


@dataclass
class BoundedProcessResult:
    """Outcome of a process run bounded by timeout and grace period (USR-114)."""

    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    duration_s: float = 0.0
    timed_out: bool = False


def _kill_process_tree(pid: int) -> None:
    """Terminate the process with PID `pid` and all its descendants (USR-114)."""
    if pid <= 0:
        return
    if sys.platform == "win32":
        try:
            # /F: forcefully terminate
            # /T: terminate the tree (process and all child processes)
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
            )
        except Exception as exc:
            logger.debug("taskkill failed for PID %s: %s", pid, exc)
        try:
            import psutil

            parent = psutil.Process(pid)
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except Exception:
                    pass
            parent.kill()
        except Exception:
            pass
    else:
        try:
            import signal

            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as exc:
            try:
                import signal

                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
            logger.debug("killpg failed for PID %s: %s", pid, exc)


def _run_bounded(
    argv: list[str],
    *,
    input_text: Optional[str] = None,
    cwd: Optional[Path | str] = None,
    timeout_s: float = 1800,
    grace_s: float = 10.0,
    stdin: Optional[Any] = None,
    env: Optional[dict[str, str]] = None,
) -> BoundedProcessResult:
    """Execute `argv` with a hard timeout and tree termination (USR-114).

    On Windows, `subprocess.run(timeout=...)` only terminates the root process, leaving
    grandchildren alive holding pipe handles, which blocks communicate() indefinitely.
    This helper starts the process in a new process group, catches TimeoutExpired on
    communicate(), terminates the entire process tree (/T /F or killpg), and gathers
    partial output within a short grace period (default 10s).
    """
    start = time.perf_counter()

    # Backwards-compatibility for existing tests monkeypatching agent_cli.subprocess.run:
    if subprocess.run is not _ORIGINAL_SUBPROCESS_RUN:
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": timeout_s,
            "cwd": str(cwd) if cwd is not None else None,
        }
        if stdin is not None:
            kwargs["stdin"] = stdin
        elif input_text is not None:
            kwargs["input"] = input_text
        if env is not None:
            kwargs["env"] = env
        try:
            proc = subprocess.run(argv, **kwargs)
            duration = round(time.perf_counter() - start, 3)
            return BoundedProcessResult(
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                returncode=proc.returncode,
                duration_s=duration,
                timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = round(min(time.perf_counter() - start, float(timeout_s + grace_s)), 3)
            return BoundedProcessResult(
                stdout=_partial_output(exc.stdout),
                stderr=_partial_output(exc.stderr),
                returncode=-1,
                duration_s=duration,
                timed_out=True,
            )

    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "cwd": str(cwd) if cwd is not None else None,
        "env": env,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    if stdin is not None:
        kwargs["stdin"] = stdin
    elif input_text is not None:
        kwargs["stdin"] = subprocess.PIPE
    else:
        kwargs["stdin"] = subprocess.DEVNULL

    proc = subprocess.Popen(argv, **kwargs)
    stdout, stderr = "", ""
    timed_out = False
    returncode = 0

    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout_s)
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        _kill_process_tree(proc.pid)
        try:
            extra_out, extra_err = proc.communicate(timeout=grace_s)
            stdout = extra_out or _partial_output(exc.stdout)
            stderr = extra_err or _partial_output(exc.stderr)
        except (subprocess.TimeoutExpired, Exception):
            try:
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()
            except Exception:
                pass
            stdout = _partial_output(exc.stdout)
            stderr = _partial_output(exc.stderr)
        returncode = proc.returncode if proc.returncode is not None else -1

    elapsed = time.perf_counter() - start
    max_allowed = float(timeout_s + grace_s)
    duration_s = round(min(elapsed, max_allowed), 3) if timed_out else round(elapsed, 3)

    return BoundedProcessResult(
        stdout=stdout or "",
        stderr=stderr or "",
        returncode=returncode,
        duration_s=duration_s,
        timed_out=timed_out,
    )


# --------------------------------------------------------------------------
# Per-harness execution
# --------------------------------------------------------------------------


def _run_claude(req: AgentRequest) -> AgentResult:
    executable = find_claude_binary()
    if not executable:
        return AgentResult(
            ok=False,
            text="Claude Code executable not found on PATH or known install locations.",
            harness="claude",
            model=req.model,
            duration_s=0.0,
            error_kind="not_installed",
        )
    argv = build_claude_argv(executable, req)
    try:
        res = _run_bounded(
            argv,
            input_text=req.prompt,
            cwd=req.cwd,
            timeout_s=req.timeout_s,
        )
    except OSError as exc:
        return AgentResult(
            ok=False, text=redact_secrets(str(exc)), harness="claude", model=req.model,
            duration_s=0.0, error_kind="crash",
        )

    if res.timed_out:
        partial = redact_secrets(f"{res.stdout}\n{res.stderr}".strip())
        return AgentResult(
            ok=False, text=f"timed out after {req.timeout_s}s" + (f": {partial}" if partial else ""),
            harness="claude", model=req.model,
            duration_s=res.duration_s, error_kind="timeout",
            stderr_tail=_stderr_tail(res.stderr),
        )

    duration = res.duration_s
    stdout = res.stdout.strip()
    stderr = res.stderr
    diag: dict[str, Any] = {"exit_code": res.returncode, "stderr_tail": _stderr_tail(stderr)}

    parsed: Optional[dict[str, Any]] = None
    if stdout:
        try:
            candidate = json.loads(stdout)
            if isinstance(candidate, dict):
                parsed = candidate
        except json.JSONDecodeError:
            parsed = None

    if parsed is not None:
        is_error = bool(parsed.get("is_error"))
        result_text = str(parsed.get("result") or "")
        usage = parsed.get("usage") if isinstance(parsed.get("usage"), dict) else None
        cost_raw = parsed.get("total_cost_usd")
        cost_usd = float(cost_raw) if isinstance(cost_raw, (int, float)) else None
        if is_error or res.returncode != 0:
            combined = f"{result_text}\n{stderr}"
            return AgentResult(
                ok=False, text=redact_secrets(result_text or stderr.strip()), harness="claude", model=req.model,
                duration_s=duration, usage=usage, cost_usd=cost_usd,
                error_kind=_classify_error(combined), reset_at=_extract_reset_at(combined), **diag,
            )
        return AgentResult(
            ok=True, text=result_text, harness="claude", model=req.model,
            duration_s=duration, usage=usage, cost_usd=cost_usd, **diag,
        )

    combined = f"{stdout}\n{stderr}"
    if res.returncode == 0 and stdout:
        return AgentResult(ok=True, text=stdout, harness="claude", model=req.model, duration_s=duration, **diag)
    return AgentResult(
        ok=False, text=redact_secrets(stdout or stderr.strip()), harness="claude", model=req.model,
        duration_s=duration,
        error_kind=_classify_error(combined), reset_at=_extract_reset_at(combined), **diag,
    )


def _run_codex(req: AgentRequest) -> AgentResult:
    executable = find_codex_binary()
    if not executable:
        return AgentResult(
            ok=False,
            text="Codex executable not found on PATH or known install locations.",
            harness="codex",
            model=req.model,
            duration_s=0.0,
            error_kind="not_installed",
        )

    mocked_tmp = REPO_ROOT / ".factory" / "tmp"
    tmp_dir = mocked_tmp if (REPO_ROOT != project_root()) else (state_root() / "tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_out = tmp_dir / f"codex_line_{uuid.uuid4().hex[:8]}.json"
    argv = build_codex_argv(executable, req, tmp_out)

    try:
        res = _run_bounded(
            argv,
            input_text=req.prompt,
            cwd=req.cwd,
            timeout_s=req.timeout_s,
        )
    except OSError as exc:
        return AgentResult(
            ok=False, text=str(exc), harness="codex", model=req.model,
            duration_s=0.0, error_kind="crash",
        )

    if res.timed_out:
        partial_from_file = ""
        if tmp_out.is_file():
            try:
                partial_from_file = tmp_out.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                pass
            try:
                tmp_out.unlink(missing_ok=True)
            except OSError:
                pass
        partial_stdout = res.stdout.strip()
        partial_stderr = res.stderr.strip()
        combined_parts = [p for p in (partial_from_file or partial_stdout, partial_stderr) if p]
        combined_partial = redact_secrets("\n".join(combined_parts)) if combined_parts else ""
        return AgentResult(
            ok=False,
            text=f"timed out after {req.timeout_s}s" + (f": {combined_partial}" if combined_partial else ""),
            harness="codex",
            model=req.model,
            duration_s=res.duration_s,
            error_kind="timeout",
            stderr_tail=_stderr_tail(res.stderr),
        )

    duration = res.duration_s
    output_text = ""
    usage: Optional[dict[str, Any]] = None
    cost_usd: Optional[float] = None
    if tmp_out.is_file():
        try:
            raw = tmp_out.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            raw = ""
        output_text = raw
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                candidate_usage = event.get("usage")
                if isinstance(candidate_usage, dict) and usage is None:
                    usage = candidate_usage
                candidate_cost = event.get("total_cost_usd")
                if isinstance(candidate_cost, (int, float)) and cost_usd is None:
                    cost_usd = float(candidate_cost)
                if usage is not None and cost_usd is not None:
                    break
        try:
            tmp_out.unlink(missing_ok=True)
        except OSError:
            pass
    if not output_text and res.stdout.strip():
        output_text = res.stdout.strip()

    stderr = res.stderr
    combined = f"{output_text}\n{stderr}"
    diag: dict[str, Any] = {"exit_code": res.returncode, "stderr_tail": _stderr_tail(stderr)}
    if res.returncode != 0:
        return AgentResult(
            ok=False, text=output_text, harness="codex", model=req.model, duration_s=duration,
            usage=usage, cost_usd=cost_usd,
            error_kind=_classify_error(combined), reset_at=_extract_reset_at(combined), **diag,
        )
    return AgentResult(
        ok=True, text=output_text, harness="codex", model=req.model, duration_s=duration,
        usage=usage, cost_usd=cost_usd, **diag,
    )


def _parse_json_object(stdout: str) -> Optional[dict[str, Any]]:
    """The whole stdout as a JSON object, or None when it is empty, not JSON or not an object."""
    if not stdout:
        return None
    try:
        candidate = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return candidate if isinstance(candidate, dict) else None


def _run_grok_write(req: AgentRequest, executable: str) -> AgentResult:
    """Grok Build in write mode: headless `--prompt-file` run with cwd = `req.cwd`.

    `--output-format json` prints one object: `{"text", "stopReason", "sessionId", "requestId",
    "usage": {input_tokens, output_tokens, ...}, "num_turns", "total_cost_usd", "modelUsage"}`; a failed
    run prints `{"type": "error", "message": ...}` and exits non-zero. A call that needs approval the
    headless run cannot give ends with `stopReason: "cancelled"` and exit 0, which is reported as a crash
    (never as a silent success) with the permission mode in the message.
    """
    mocked_tmp = REPO_ROOT / ".factory" / "tmp"
    tmp_dir = mocked_tmp if (REPO_ROOT != project_root()) else (state_root() / "tmp")
    prompt_file = tmp_dir / f"grok_line_{uuid.uuid4().hex[:8]}.prompt.txt"
    start = time.perf_counter()
    try:
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            prompt_file.write_text(req.prompt, encoding="utf-8")
        except OSError as exc:
            return AgentResult(
                ok=False, text=redact_secrets(f"could not write the Grok prompt file: {exc}"),
                harness="grok", model=req.model,
                duration_s=round(time.perf_counter() - start, 3), error_kind="crash",
            )
        argv = build_grok_write_argv(executable, req, prompt_file)
        try:
            res = _run_bounded(
                argv,
                stdin=subprocess.DEVNULL,
                cwd=req.cwd,
                timeout_s=req.timeout_s,
            )
        except OSError as exc:
            return AgentResult(
                ok=False, text=redact_secrets(str(exc)), harness="grok", model=req.model,
                duration_s=0.0, error_kind="crash",
            )
    finally:
        try:
            prompt_file.unlink(missing_ok=True)
        except OSError:
            pass

    if res.timed_out:
        partial = redact_secrets(f"{res.stdout}\n{res.stderr}".strip())
        return AgentResult(
            ok=False, text=f"timed out after {req.timeout_s}s" + (f": {partial}" if partial else ""),
            harness="grok", model=req.model,
            duration_s=res.duration_s, error_kind="timeout",
            stderr_tail=_stderr_tail(res.stderr),
        )

    duration = res.duration_s
    stdout = (res.stdout or "").strip()
    stderr = res.stderr or ""
    diag: dict[str, Any] = {"exit_code": res.returncode, "stderr_tail": _stderr_tail(stderr)}

    parsed = _parse_json_object(stdout)
    usage: Optional[dict[str, Any]] = None
    cost_usd: Optional[float] = None
    stop_reason: Optional[str] = None
    is_error = False
    if parsed is None:
        result_text = stdout
        error_text = stdout  # unstructured output: it may be the only place the failure is described
    else:
        is_error = parsed.get("type") == "error"
        result_text = str(parsed.get("message" if is_error else "text") or "")
        error_text = result_text if is_error else ""  # never classify the agent's own prose
        if isinstance(parsed.get("usage"), dict):
            usage = parsed["usage"]
        cost_raw = parsed.get("total_cost_usd")
        cost_usd = float(cost_raw) if isinstance(cost_raw, (int, float)) and not isinstance(cost_raw, bool) else None
        stop_reason = parsed.get("stopReason") if isinstance(parsed.get("stopReason"), str) else None

    if res.returncode != 0 or is_error:
        combined = f"{error_text}\n{stderr}"
        return AgentResult(
            ok=False, text=redact_secrets(result_text or stderr.strip()), harness="grok", model=req.model,
            duration_s=duration, usage=usage, cost_usd=cost_usd,
            error_kind=_classify_error(combined), reset_at=_extract_reset_at(combined), **diag,
        )
    if stop_reason == "cancelled":
        detail = (
            f"Grok stopped with stopReason=cancelled (permission mode '{grok_permission_mode()}'): "
            "a tool call that needs approval is cancelled in headless runs."
        )
        return AgentResult(
            ok=False, text=redact_secrets(f"{detail} {result_text}".strip()), harness="grok", model=req.model,
            duration_s=duration, usage=usage, cost_usd=cost_usd, error_kind="crash", **diag,
        )
    return AgentResult(
        ok=True, text=redact_secrets(result_text), harness="grok", model=req.model,
        duration_s=duration, usage=usage, cost_usd=cost_usd, **diag,
    )


def _run_grok(req: AgentRequest) -> AgentResult:
    executable = find_grok_binary()
    if not executable:
        return AgentResult(
            ok=False, text="Grok executable not found on PATH or known install locations.",
            harness="grok", model=req.model, duration_s=0.0, error_kind="not_installed",
        )
    if req.mode == "write":
        return _run_grok_write(req, executable)
    # Read mode: one-shot `-p` call that returns plain text (unchanged since HF-27-03).
    argv = [executable, "-p", req.prompt, "--output-format", "plain"]
    if req.model:
        argv += ["-m", req.model]
    try:
        res = _run_bounded(
            argv,
            cwd=req.cwd,
            timeout_s=req.timeout_s,
        )
    except OSError as exc:
        return AgentResult(
            ok=False, text=str(exc), harness="grok", model=req.model,
            duration_s=0.0, error_kind="crash",
        )
    if res.timed_out:
        partial = redact_secrets(f"{res.stdout}\n{res.stderr}".strip())
        return AgentResult(
            ok=False, text=f"timed out after {req.timeout_s}s" + (f": {partial}" if partial else ""),
            harness="grok", model=req.model,
            duration_s=res.duration_s, error_kind="timeout",
            stderr_tail=_stderr_tail(res.stderr),
        )
    duration = res.duration_s
    output_text = (res.stdout or "").strip()
    stderr = res.stderr or ""
    diag: dict[str, Any] = {"exit_code": res.returncode, "stderr_tail": _stderr_tail(stderr)}
    if res.returncode != 0 and not output_text:
        combined = f"{output_text}\n{stderr}"
        return AgentResult(
            ok=False, text="", harness="grok", model=req.model, duration_s=duration,
            error_kind=_classify_error(combined), reset_at=_extract_reset_at(combined), **diag,
        )
    return AgentResult(ok=True, text=output_text, harness="grok", model=req.model, duration_s=duration, **diag)


def _run_antigravity(req: AgentRequest) -> AgentResult:
    # Read-only: `run_agent` refuses `write` via HARNESS_CAPABILITIES before reaching this runner.
    executable = find_antigravity_binary()
    if not executable:
        return AgentResult(
            ok=False, text="Antigravity executable/agentapi not found on PATH or known install locations.",
            harness="antigravity", model=req.model, duration_s=0.0, error_kind="not_installed",
        )
    tier = req.model or "flash"
    argv = [executable, "new-conversation", f"--model={tier}", req.prompt]
    try:
        res = _run_bounded(
            argv,
            cwd=req.cwd,
            timeout_s=req.timeout_s,
        )
    except OSError as exc:
        return AgentResult(
            ok=False, text=str(exc), harness="antigravity", model=req.model,
            duration_s=0.0, error_kind="crash",
        )
    if res.timed_out:
        partial = redact_secrets(f"{res.stdout}\n{res.stderr}".strip())
        return AgentResult(
            ok=False, text=f"timed out after {req.timeout_s}s" + (f": {partial}" if partial else ""),
            harness="antigravity", model=tier,
            duration_s=res.duration_s, error_kind="timeout",
            stderr_tail=_stderr_tail(res.stderr),
        )
    duration = res.duration_s
    output_text = (res.stdout or "").strip()
    stderr = res.stderr or ""
    diag: dict[str, Any] = {"exit_code": res.returncode, "stderr_tail": _stderr_tail(stderr)}
    if res.returncode != 0 and not output_text:
        combined = f"{output_text}\n{stderr}"
        return AgentResult(
            ok=False, text="", harness="antigravity", model=tier, duration_s=duration,
            error_kind=_classify_error(combined), reset_at=_extract_reset_at(combined), **diag,
        )
    return AgentResult(ok=True, text=output_text, harness="antigravity", model=tier, duration_s=duration, **diag)


_DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4.1-flash"
_OPENROUTER_MAX_TOKENS = 4096


def _openrouter_reasoning_enabled() -> bool:
    """Reasoning is OFF by default; DARKFAC_OPENROUTER_REASONING=on|true|1 re-enables it."""
    return os.environ.get("DARKFAC_OPENROUTER_REASONING", "").strip().lower() in {"on", "true", "1"}


def _run_openrouter(req: AgentRequest) -> AgentResult:
    # Read-only: `run_agent` refuses `write` via HARNESS_CAPABILITIES before reaching this runner.
    from core.execution.providers import OpenRouterModelProvider  # local import: keeps agent_cli import-light

    model = req.model or os.environ.get("DARKFAC_OPENROUTER_CHEAP_MODEL") or _DEFAULT_OPENROUTER_MODEL
    provider = OpenRouterModelProvider()
    start = time.perf_counter()
    try:
        # Reasoning models (deepseek-v4.1-flash) can spend the whole completion
        # budget thinking and return `content: null` (finish_reason=length), even
        # at 16384 tokens. Disabling reasoning fixes it (1.5k tokens, valid JSON),
        # so 4096 is ample; raising max_tokens does not help.
        response = provider.generate(
            req.prompt,
            model=model,
            max_tokens=_OPENROUTER_MAX_TOKENS,
            reasoning=None if _openrouter_reasoning_enabled() else {"enabled": False},
        )
    except Exception as exc:  # network/auth/model errors all surface here
        duration = round(time.perf_counter() - start, 3)
        message = str(exc)
        return AgentResult(
            ok=False, text=message, harness="openrouter", model=model, duration_s=duration,
            error_kind=_classify_error(message), reset_at=_extract_reset_at(message),
        )
    duration = round(time.perf_counter() - start, 3)
    usage = {
        "prompt_tokens": response.tokens_prompt,
        "completion_tokens": response.tokens_completion,
        "total_tokens": response.total_tokens,
    }
    cost_usd = response.measured_cost if response.is_measured else response.estimated_cost
    if not (response.text or "").strip():
        truncated = (response.tokens_completion or 0) >= _OPENROUTER_MAX_TOKENS
        logger.warning(
            "openrouter empty content: model=%s completion_tokens=%s max_tokens=%s%s",
            model, response.tokens_completion, _OPENROUTER_MAX_TOKENS,
            " (likely finish_reason=length: reasoning consumed the budget)" if truncated else "",
        )
    return AgentResult(
        ok=True, text=response.text or "", harness="openrouter", model=response.model,
        duration_s=duration, usage=usage, cost_usd=cost_usd,
    )


_HARNESS_RUNNERS: dict[str, Callable[[AgentRequest], AgentResult]] = {
    "claude": _run_claude,
    "codex": _run_codex,
    "grok": _run_grok,
    "antigravity": _run_antigravity,
    "openrouter": _run_openrouter,
}


def _unsupported_mode_result(harness: str, req: AgentRequest) -> AgentResult:
    """Refusal for a mode the harness does not declare; no binary is looked up or spawned."""
    declared = sorted(HARNESS_CAPABILITIES.get(harness, frozenset()))
    detail = "read-only" if declared == ["read"] else f"capabilities: {', '.join(declared) or 'none'}"
    return AgentResult(
        ok=False,
        text=f"Harness '{harness}' does not support '{req.mode}' mode ({detail}).",
        harness=harness, model=req.model, duration_s=0.0, error_kind="unsupported_mode",
    )


def run_agent(req: AgentRequest) -> AgentResult:
    """Run a single agent-CLI invocation and return a normalized result."""
    harness = req.harness.lower().strip()
    runner = _HARNESS_RUNNERS.get(harness)
    if runner is None:
        return AgentResult(
            ok=False,
            text=f"Unsupported harness '{req.harness}'. Supported: {', '.join(sorted(_HARNESS_RUNNERS))}",
            harness=req.harness, model=req.model, duration_s=0.0, error_kind="not_installed",
        )
    if not supports(harness, req.mode):
        return _unsupported_mode_result(harness, req)
    res = runner(req)
    # Read mode exists to *return text* (grill/planning/review answers), so an
    # "ok" result with none is a failure to retry, not a success to parse. Write
    # mode legitimately edits files and may print nothing.
    if res.ok and req.mode == "read" and not (res.text or "").strip():
        logger.warning("Harness %s returned no text for a successful read call; classifying as empty_output", harness)
        res = res.model_copy(update={"ok": False, "error_kind": "empty_output"})

    # Telemetria ponta a ponta: Registrar evento no ModelUsageLedger
    try:
        from core.usage.ledger import ModelUsageLedger, infer_model_tier
        from core.usage.models import ModelCallEvent, ModelModality, ModelTier

        provider_map = {
            "claude": "anthropic",
            "codex": "openai",
            "grok": "xai",
            "antigravity": "google",
            "openrouter": "openrouter",
        }
        prov = provider_map.get(harness, harness)
        model_name = res.model or (req.model or "unknown")
        tier_val = infer_model_tier(prov, model_name)

        input_toks = None
        output_toks = None
        if res.usage and isinstance(res.usage, dict):
            input_toks = res.usage.get("prompt_tokens") or res.usage.get("input_tokens")
            output_toks = res.usage.get("completion_tokens") or res.usage.get("output_tokens")

        mocked_usage = REPO_ROOT / ".factory" / "usage"
        usage_dir = mocked_usage if (REPO_ROOT != project_root()) else (state_root() / "usage")
        ledger = ModelUsageLedger(usage_dir)
        ledger.record(
            ModelCallEvent(
                provider=prov,
                model=model_name,
                tier=ModelTier(tier_val),
                harness=harness,
                modality=ModelModality.TEXT,
                success=res.ok,
                input_tokens=input_toks,
                output_tokens=output_toks,
                cost_usd=res.cost_usd,
                latency_ms=round(res.duration_s * 1000, 1),
                source=f"agent_cli.{harness}",
            )
        )
    except Exception as exc:
        logger.debug("Failed recording agent_cli call to ModelUsageLedger: %s", exc)

    return res
