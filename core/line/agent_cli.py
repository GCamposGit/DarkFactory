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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

from core.harness.remote_worker import (
    find_antigravity_binary,
    find_claude_binary,
    find_codex_binary,
    find_grok_binary,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

ErrorKind = Optional[Literal["rate_limited", "auth_expired", "not_installed", "timeout", "crash"]]


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


# --------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------

_RATE_LIMIT_PATTERN = re.compile(r"rate[\s_-]?limit|usage[\s_-]?limit|\b429\b", re.IGNORECASE)
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


def _classify_error(text: str) -> ErrorKind:
    """Classify a combined stdout/stderr/error-message blob into an ErrorKind."""
    if not text:
        return "crash"
    if _RATE_LIMIT_PATTERN.search(text):
        return "rate_limited"
    if _AUTH_PATTERN.search(text):
        return "auth_expired"
    return "crash"


def _extract_reset_at(text: str) -> Optional[datetime]:
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
    return None


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


def build_codex_argv(executable: str, req: AgentRequest, tmp_out: Path) -> list[str]:
    """Build the Codex CLI argv for read or write mode. Prompt goes on stdin via trailing '-'."""
    sandbox = "workspace-write" if req.mode == "write" else "read-only"
    argv = [executable, "exec", "--sandbox", sandbox, "--skip-git-repo-check", "--json"]
    if req.model:
        argv += ["-m", req.model]
    argv += ["-o", str(tmp_out), "-"]
    return argv


def _win_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


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
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            argv,
            input=req.prompt,
            capture_output=True,
            text=True,
            cwd=str(req.cwd),
            timeout=req.timeout_s,
            encoding="utf-8",
            errors="replace",
            **_win_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return AgentResult(
            ok=False, text="", harness="claude", model=req.model,
            duration_s=round(time.perf_counter() - start, 3), error_kind="timeout",
        )
    except OSError as exc:
        return AgentResult(
            ok=False, text=str(exc), harness="claude", model=req.model,
            duration_s=round(time.perf_counter() - start, 3), error_kind="crash",
        )
    duration = round(time.perf_counter() - start, 3)
    stdout = (proc.stdout or "").strip()
    stderr = proc.stderr or ""

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
        if is_error or proc.returncode != 0:
            combined = f"{result_text}\n{stderr}"
            return AgentResult(
                ok=False, text=result_text, harness="claude", model=req.model,
                duration_s=duration, usage=usage, cost_usd=cost_usd,
                error_kind=_classify_error(combined), reset_at=_extract_reset_at(combined),
            )
        return AgentResult(
            ok=True, text=result_text, harness="claude", model=req.model,
            duration_s=duration, usage=usage, cost_usd=cost_usd,
        )

    combined = f"{stdout}\n{stderr}"
    if proc.returncode == 0 and stdout:
        return AgentResult(ok=True, text=stdout, harness="claude", model=req.model, duration_s=duration)
    return AgentResult(
        ok=False, text=stdout, harness="claude", model=req.model, duration_s=duration,
        error_kind=_classify_error(combined), reset_at=_extract_reset_at(combined),
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

    tmp_dir = REPO_ROOT / ".factory" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_out = tmp_dir / f"codex_line_{uuid.uuid4().hex[:8]}.json"
    argv = build_codex_argv(executable, req, tmp_out)

    start = time.perf_counter()
    try:
        proc = subprocess.run(
            argv,
            input=req.prompt,
            capture_output=True,
            text=True,
            cwd=str(req.cwd),
            timeout=req.timeout_s,
            encoding="utf-8",
            errors="replace",
            **_win_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return AgentResult(
            ok=False, text="", harness="codex", model=req.model,
            duration_s=round(time.perf_counter() - start, 3), error_kind="timeout",
        )
    except OSError as exc:
        return AgentResult(
            ok=False, text=str(exc), harness="codex", model=req.model,
            duration_s=round(time.perf_counter() - start, 3), error_kind="crash",
        )

    duration = round(time.perf_counter() - start, 3)
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
    if not output_text and (proc.stdout or "").strip():
        output_text = proc.stdout.strip()

    stderr = proc.stderr or ""
    combined = f"{output_text}\n{stderr}"
    if proc.returncode != 0:
        return AgentResult(
            ok=False, text=output_text, harness="codex", model=req.model, duration_s=duration,
            usage=usage, cost_usd=cost_usd,
            error_kind=_classify_error(combined), reset_at=_extract_reset_at(combined),
        )
    return AgentResult(
        ok=True, text=output_text, harness="codex", model=req.model, duration_s=duration,
        usage=usage, cost_usd=cost_usd,
    )


def _run_grok(req: AgentRequest) -> AgentResult:
    if req.mode == "write":
        return AgentResult(
            ok=False,
            text="Grok Build has no reliable headless write mode on this host; only 'read' is supported.",
            harness="grok", model=req.model, duration_s=0.0, error_kind="not_installed",
        )
    executable = find_grok_binary()
    if not executable:
        return AgentResult(
            ok=False, text="Grok executable not found on PATH or known install locations.",
            harness="grok", model=req.model, duration_s=0.0, error_kind="not_installed",
        )
    argv = [executable, "-p", req.prompt, "--output-format", "plain"]
    if req.model:
        argv += ["-m", req.model]
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, cwd=str(req.cwd), timeout=req.timeout_s,
            encoding="utf-8", errors="replace", **_win_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return AgentResult(
            ok=False, text="", harness="grok", model=req.model,
            duration_s=round(time.perf_counter() - start, 3), error_kind="timeout",
        )
    except OSError as exc:
        return AgentResult(
            ok=False, text=str(exc), harness="grok", model=req.model,
            duration_s=round(time.perf_counter() - start, 3), error_kind="crash",
        )
    duration = round(time.perf_counter() - start, 3)
    output_text = (proc.stdout or "").strip()
    stderr = proc.stderr or ""
    if proc.returncode != 0 and not output_text:
        combined = f"{output_text}\n{stderr}"
        return AgentResult(
            ok=False, text="", harness="grok", model=req.model, duration_s=duration,
            error_kind=_classify_error(combined), reset_at=_extract_reset_at(combined),
        )
    return AgentResult(ok=True, text=output_text, harness="grok", model=req.model, duration_s=duration)


def _run_antigravity(req: AgentRequest) -> AgentResult:
    if req.mode == "write":
        return AgentResult(
            ok=False,
            text="Antigravity has no reliable headless write mode on this host; only 'read' is supported.",
            harness="antigravity", model=req.model, duration_s=0.0, error_kind="not_installed",
        )
    executable = find_antigravity_binary()
    if not executable:
        return AgentResult(
            ok=False, text="Antigravity executable/agentapi not found on PATH or known install locations.",
            harness="antigravity", model=req.model, duration_s=0.0, error_kind="not_installed",
        )
    tier = req.model or "flash"
    argv = [executable, "new-conversation", f"--model={tier}", req.prompt]
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, cwd=str(req.cwd), timeout=req.timeout_s,
            encoding="utf-8", errors="replace", **_win_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return AgentResult(
            ok=False, text="", harness="antigravity", model=req.model,
            duration_s=round(time.perf_counter() - start, 3), error_kind="timeout",
        )
    except OSError as exc:
        return AgentResult(
            ok=False, text=str(exc), harness="antigravity", model=req.model,
            duration_s=round(time.perf_counter() - start, 3), error_kind="crash",
        )
    duration = round(time.perf_counter() - start, 3)
    output_text = (proc.stdout or "").strip()
    stderr = proc.stderr or ""
    if proc.returncode != 0 and not output_text:
        combined = f"{output_text}\n{stderr}"
        return AgentResult(
            ok=False, text="", harness="antigravity", model=tier, duration_s=duration,
            error_kind=_classify_error(combined), reset_at=_extract_reset_at(combined),
        )
    return AgentResult(ok=True, text=output_text, harness="antigravity", model=tier, duration_s=duration)


_DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4.1-flash"


def _run_openrouter(req: AgentRequest) -> AgentResult:
    if req.mode != "read":
        return AgentResult(
            ok=False,
            text="OpenRouter agent_cli support is read-only (review/grill/distill stages only).",
            harness="openrouter", model=req.model, duration_s=0.0, error_kind="crash",
        )
    from core.execution.providers import OpenRouterModelProvider  # local import: keeps agent_cli import-light

    model = req.model or os.environ.get("DARKFAC_OPENROUTER_CHEAP_MODEL") or _DEFAULT_OPENROUTER_MODEL
    provider = OpenRouterModelProvider()
    start = time.perf_counter()
    try:
        response = provider.generate(req.prompt, model=model)
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
    return AgentResult(
        ok=True, text=response.text, harness="openrouter", model=response.model,
        duration_s=duration, usage=usage, cost_usd=cost_usd,
    )


_HARNESS_RUNNERS: dict[str, Callable[[AgentRequest], AgentResult]] = {
    "claude": _run_claude,
    "codex": _run_codex,
    "grok": _run_grok,
    "antigravity": _run_antigravity,
    "openrouter": _run_openrouter,
}


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
    return runner(req)
