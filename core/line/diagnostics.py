"""Remote visibility for line-stage agent failures.

A failed iteration used to leave nothing behind but a terse cause code: the log file was written to
the worker's local worktree and never committed, and the worker only logged "Job execution
completed". This module gives every stage the same three tools:

- `format_attempt_log` / `record_attempt`: a per-attempt record (stage, iteration, harness, model,
  error kind, duration and the redacted first ~2000 characters of the agent/validate output, plus, for
  validate runs, the redacted last ~3000 characters of the RAW command output: the distilled report only
  sees pytest lines, so a non-test failure such as the runner's "worktree is dirty" check lives there);
- `persist`: commit and push ONLY the run's context directory (`.darkfac/runs/<run_id>/`) to the
  run branch, so the evidence is readable on GitHub without also committing a failing iteration's
  half-made edits;
- `worker_snippet`: the ~300-character redacted one-liner the cloud worker logs per failed stage.

Everything is best-effort: diagnostics must never change a stage's outcome.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from core.line import workspace
from core.line.agent_cli import redact_secrets

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 2000
RAW_TAIL_CHARS = 3000
SNIPPET_CHARS = 300
REMOTE_SECTION_MAX_LINES = 40
REMOTE_SECTION_MAX_CHARS = 3000
REMOTE_LINE_PREFIXES = ("[REMOTE]", "[line_validate]")
_MAX_RECORDED_ATTEMPTS = 10
# The live board shows at most 12 evidence refs per stage (core.workflow.line_live.MAX_EVIDENCE_REFS).
MAX_FAILURE_TEST_REFS = 8
_MAX_REF_CHARS = 200
# pytest's short test summary: `FAILED tests/x.py::test_y - AssertionError`, `ERROR tests/x.py::test_y`.
_FAILED_TEST_LINE = re.compile(r"^(?:FAILED|ERROR)[ \t]+(\S+)", re.MULTILINE)


def redacted_head(text: Optional[str], limit: int = MAX_OUTPUT_CHARS) -> str:
    """Secret-redacted first `limit` characters of `text`, with a marker when truncated."""
    clean = redact_secrets(text or "")
    if len(clean) <= limit:
        return clean
    return f"{clean[:limit]}\n... [truncated, {len(clean)} chars total]"


def redacted_tail(text: Optional[str], limit: int = RAW_TAIL_CHARS) -> str:
    """Secret-redacted LAST `limit` characters of `text`, with a marker when truncated.

    Redaction runs before truncation so a token straddling the cut is never left half-visible. The tail
    is where a runner prints what a distilled test report cannot see (`[ERROR] ...`, `[HARNESS_FAIL]`).
    """
    clean = redact_secrets(text or "").rstrip()
    if len(clean) <= limit:
        return clean
    return f"[truncated, {len(clean)} chars total] ...\n{clean[-limit:]}"


def remote_dispatch_lines(
    text: Optional[str],
    *,
    max_lines: int = REMOTE_SECTION_MAX_LINES,
    max_chars: int = REMOTE_SECTION_MAX_CHARS,
) -> list[str]:
    """Redacted `[REMOTE]` / `[line_validate]` lines of a raw validate output, in order.

    The raw tail only keeps the END of a long pytest run, so the lines that explain WHY the suite ran
    locally instead of on the Desktop test worker (worker unreachable, busy, unavailable, no remote
    used) were lost. They are collected from the whole output; when over the caps the newest lines win.
    """
    found = [
        line.strip()
        for line in redact_secrets(text or "").splitlines()
        if line.strip().startswith(REMOTE_LINE_PREFIXES)
    ]
    found = found[-max_lines:]
    while len(found) > 1 and sum(len(line) + 1 for line in found) > max_chars:
        found.pop(0)
    return [line if len(line) <= max_chars else f"{line[:max_chars]}..." for line in found]


def remote_dispatch_section(raw_output: Optional[str]) -> list[str]:
    """Markdown lines of the remote-dispatch section of a validate attempt log."""
    lines = remote_dispatch_lines(raw_output)
    body = "\n".join(lines) if lines else "(no [REMOTE] line in the output: remote dispatch was not attempted)"
    return ["## Remote dispatch lines (redacted)", "", "```text", body, "```", ""]


def failed_test_ids(output: Optional[str]) -> list[str]:
    """Distinct pytest node ids named in the `FAILED`/`ERROR` lines of a command output, in order."""
    seen: dict[str, None] = {}
    for match in _FAILED_TEST_LINE.finditer(redact_secrets(output or "")):
        seen.setdefault(match.group(1)[:_MAX_REF_CHARS], None)
    return list(seen)


def validation_failure_refs(
    output: Optional[str],
    *,
    exit_code: Optional[int] = None,
    log_name: Optional[str] = None,
    limit: int = MAX_FAILURE_TEST_REFS,
) -> list[str]:
    """Short, redacted `evidence_refs` that say WHAT a failed validate run broke.

    A stage that ends `failed`/`retry` only exposes its cause code on the live board
    (`validate_exhausted`, `retry:development` + `clean_validate_failed:<sha>`), which says neither which
    tests failed nor where to read the log. These refs carry the failing test ids (capped, with a
    "+N more" marker), the exit code and the name of the attempt log committed to the run branch.
    """
    ids = failed_test_ids(output)
    refs = [f"validate_failed:{node_id}" for node_id in ids[:limit]]
    if len(ids) > limit:
        refs.append(f"validate_failed:+{len(ids) - limit} more")
    if exit_code is not None:
        refs.append(f"validate_exit:{exit_code}")
    if log_name:
        refs.append(f"validate_log:{log_name}")
    return refs


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
    raw_tail: Optional[str] = None,
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
    if raw_tail is not None:
        lines += [
            f"## Raw output tail (redacted, last {RAW_TAIL_CHARS} chars)", "", "```text", redacted_tail(raw_tail), "```", "",
        ]
        lines += remote_dispatch_section(raw_tail)
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
