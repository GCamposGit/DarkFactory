"""Remote visibility for line-stage agent failures.

A failed iteration used to leave nothing behind but a terse cause code: the log file was written to
the worker's local worktree and never committed, and the worker only logged "Job execution
completed". This module gives every stage the same three tools:

- `format_attempt_log` / `record_attempt`: a per-attempt record (stage, iteration, harness, model,
  error kind, duration and the redacted first ~2000 characters of the agent/validate output);
- `persist`: commit and push ONLY the run's context directory (`.darkfac/runs/<run_id>/`) to the
  run branch, so the evidence is readable on GitHub without also committing a failing iteration's
  half-made edits;
- `worker_snippet`: the ~300-character redacted one-liner the cloud worker logs per failed stage.

Everything is best-effort: diagnostics must never change a stage's outcome.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from core.line import workspace
from core.line.agent_cli import redact_secrets

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 2000
SNIPPET_CHARS = 300
_MAX_RECORDED_ATTEMPTS = 10


def redacted_head(text: Optional[str], limit: int = MAX_OUTPUT_CHARS) -> str:
    """Secret-redacted first `limit` characters of `text`, with a marker when truncated."""
    clean = redact_secrets(text or "")
    if len(clean) <= limit:
        return clean
    return f"{clean[:limit]}\n... [truncated, {len(clean)} chars total]"


def worker_snippet(text: Optional[str], limit: int = SNIPPET_CHARS) -> str:
    """Single-line, redacted, length-capped text for a worker log line."""
    one_line = " ".join(redact_secrets(text or "").split())
    return one_line if len(one_line) <= limit else f"{one_line[:limit]}..."


def format_attempt_log(
    stage: str,
    *,
    iteration: Optional[int],
    harness: Optional[str],
    model: Optional[str],
    error_kind: Optional[str],
    duration_s: Optional[float],
    output: Optional[str],
    note: str = "",
) -> str:
    lines = [
        f"# {stage} attempt log",
        "",
        f"- iteration: {iteration if iteration is not None else '-'}",
        f"- harness: {harness or '-'}",
        f"- model: {model or '-'}",
        f"- error_kind: {error_kind or '-'}",
        f"- duration_s: {duration_s if duration_s is not None else '-'}",
    ]
    if note:
        lines.append(f"- note: {note}")
    lines += ["", f"## Output (redacted, first {MAX_OUTPUT_CHARS} chars)", "", "```text", redacted_head(output), "```", ""]
    return "\n".join(lines)


def _attempts_file(stage: str) -> str:
    return f"agent-attempts-{stage}.json"


def load_attempts(ws: workspace.RunWorkspace, stage: str) -> list[dict[str, Any]]:
    path = workspace.context_dir(ws) / _attempts_file(stage)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def record_attempt(
    ws: workspace.RunWorkspace,
    stage: str,
    *,
    harness: Optional[str],
    model: Optional[str],
    error_kind: Optional[str],
    duration_s: Optional[float],
    output: Optional[str],
    note: str = "",
) -> list[dict[str, Any]]:
    """Append one failed agent attempt to `.darkfac/runs/<run>/agent-attempts-<stage>.json`.

    The same file drives the harness fallback of the next attempt (`failed_pairs`) and is what
    an operator reads on the run branch. Returns the full (capped) attempt list.
    """
    attempts = load_attempts(ws, stage)
    attempts.append(
        {
            "attempt": len(attempts) + 1,
            "harness": harness,
            "model": model,
            "error_kind": error_kind,
            "duration_s": duration_s,
            "note": note,
            "output": redacted_head(output),
        }
    )
    attempts = attempts[-_MAX_RECORDED_ATTEMPTS:]
    workspace.write_context(ws, _attempts_file(stage), json.dumps(attempts, indent=2, ensure_ascii=False))
    return attempts


def failed_pairs(attempts: list[dict[str, Any]]) -> set[tuple[str, Optional[str]]]:
    """`(harness, model)` pairs of recorded failed attempts, for `routing.pick(exclude=...)`."""
    return {(a["harness"], a.get("model")) for a in attempts if a.get("harness")}


def persist(ws: workspace.RunWorkspace, message: str, job_key: str) -> bool:
    """Commit + push the run's context directory only. Never raises; returns whether it pushed."""
    try:
        sha = workspace.commit_paths(ws, message, job_key, [workspace.context_dir(ws)])
        if sha is None:
            return False
        workspace.push(ws)
        return True
    except Exception as exc:  # noqa: BLE001 - diagnostics must never change a stage outcome
        logger.warning("Could not persist diagnostics for run %s: %s", ws.run_id, type(exc).__name__)
        return False
