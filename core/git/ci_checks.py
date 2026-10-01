"""CI gate helpers shared by the autonomous merge paths (USR-85).

The factory merges its own PRs (``core.git.autonomy`` for the inner cycle,
``core.line.stage_integration`` for the production line). Before USR-85 only
the line looked at CI: the inner cycle ran ``gh pr merge`` blindly, so dozens of
PRs landed while ``DarkFac CI`` was red on ``main`` and nobody was told.

This module is pure and dependency-light (it never imports ``core.line``): every
GitHub call goes through an injected ``runner`` so it is unit-testable without a
network. It provides

* parsing/classification of ``gh pr checks --json bucket,name,link,workflow``
  (``green`` | ``pending`` | ``failed`` | ``no_checks`` | ``error``);
* :func:`ensure_green`, the waiting gate that turns those snapshots into a typed
  :class:`CiVerdict` (``green``, ``no_checks``, ``pending_timeout``, ``failed``),
  including the failing job's log tail and the ``base_red`` flag (the same
  check is already red on the last conclusive run of the base branch);
* :func:`check_main`, the read-only probe of the base branch CI that backs
  ``python -m core.git.autonomy check-main``;
* :func:`classify_base_red` and the ``base_red`` wait policy (USR-86): what a caller does when
  the only red checks are the ones already red on the base branch;
* :func:`is_ledger_only`, the predicate behind the ledger-only exception.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Optional, Protocol, Sequence

logger = logging.getLogger("darkfac.git.ci_checks")

CI_WAIT_ENV = "DARKFAC_CI_WAIT_SECONDS"
NO_CHECKS_GRACE_ENV = "DARKFAC_CI_NO_CHECKS_GRACE_SECONDS"
DEFAULT_CI_WAIT_SECONDS = 1200.0
DEFAULT_POLL_SECONDS = 20.0
# Right after a push/PR creation GitHub needs a few seconds to register the
# workflow runs: "no checks reported" inside this window is not yet proof that
# the repository has no CI.
DEFAULT_NO_CHECKS_GRACE_SECONDS = 30.0

LOG_TAIL_CHARS = 2000
LOG_TAIL_LINES = 150
_MAX_LOG_RUNS = 2

DEFAULT_MAIN_WORKFLOW = "DarkFac CI"

# Paths whose changes carry no executable code: the ticket ledger and roadmap.
LEDGER_PREFIXES: tuple[str, ...] = (".factory/demands/", ".factory/roadmap/")

_RED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
_INCONCLUSIVE_CONCLUSIONS = frozenset({"cancelled", "skipped", "stale", ""})
# `bucket` values of `gh pr checks` that are neither failures nor still running.
_SETTLED_BUCKETS = frozenset({"pass", "skipping", "cancel"})

# `DarkFac CI` runs `pr-validation` on pull requests and `main-validation` on
# pushes: the same verification under two job names. Used to decide whether a
# red PR check is the very same failure already present on the base branch.
_PR_JOB_TO_BASE_JOB = {"pr-validation": "main-validation"}

_TOKEN_LIKE = re.compile(
    r"(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]+", re.IGNORECASE
)
_RUN_LINK_PATTERN = re.compile(r"/runs/(\d+)")
_GH_LOG_PREFIX = re.compile(
    r"^[^\t\n]*\t[^\t\n]*\t\d{4}-\d{2}-\d{2}T[\d:.]+Z ?", re.MULTILINE
)

CheckState = Literal["green", "pending", "failed", "no_checks", "error"]
CiStatus = Literal["green", "no_checks", "pending_timeout", "failed"]
MainState = Literal["green", "red", "unknown"]


class GhResultLike(Protocol):
    """Minimal result shape shared by ``subprocess.CompletedProcess`` and ``GhResult``."""

    returncode: int
    stdout: str
    stderr: str


GhRunner = Callable[[Sequence[str], Path], GhResultLike]


# ----------------------------------------------------------------------
# Text helpers
# ----------------------------------------------------------------------


def sanitize(text: str) -> str:
    """Redact GitHub token lookalikes so logs and reports never leak a secret."""
    return _TOKEN_LIKE.sub("[REDACTED_TOKEN]", text or "")


def tail_chars(text: str, limit: int) -> str:
    """Keep the last ``limit`` characters (the useful end of a log)."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[-limit:]


def condense_log(raw: str, max_chars: int = LOG_TAIL_CHARS) -> str:
    """Turn a ``gh run view --log-failed`` dump into a short, sanitized tail.

    Drops the ``<job>\\t<step>\\t<timestamp>`` prefix gh puts on every line and
    everything after the last ``##[error]`` annotation (post-job cleanup noise
    that would otherwise push the actual failure out of the tail). The distinct
    ``##[error]`` messages are also listed up front so an early ``fatal:`` line
    survives a long tail.
    """
    # Redact before truncating so a cut can never leave half a token behind.
    text = sanitize(_GH_LOG_PREFIX.sub("", raw or ""))
    lines = text.splitlines()
    error_lines = [i for i, line in enumerate(lines) if "##[error]" in line]
    header = ""
    if error_lines:
        lines = lines[: error_lines[-1] + 1]
        messages: list[str] = []
        for i in error_lines:
            message = lines[i].split("##[error]", 1)[1].strip()[:200]
            if message and message not in messages:
                messages.append(message)
        if messages:
            header = tail_chars(f"Errors: {' | '.join(messages[:5])}", max_chars // 2) + "\n---\n"
    body = tail_chars("\n".join(lines).strip(), max_chars - len(header))
    return header + body


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %.0fs.", name, raw, default)
        return default


# ----------------------------------------------------------------------
# `gh pr checks` parsing
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class CheckSnapshot:
    """Classified outcome of one ``gh pr checks`` call."""

    state: CheckState
    checks: tuple[dict[str, Any], ...] = ()
    failing: tuple[dict[str, Any], ...] = ()
    pending: tuple[dict[str, Any], ...] = ()
    error: str = ""


def check_label(check: dict[str, Any]) -> str:
    """Human label ``workflow / job`` (or just the job name) for a check."""
    name = str(check.get("name") or "?")
    workflow = str(check.get("workflow") or "")
    return f"{workflow} / {name}" if workflow else name


def classify_checks(checks: Any) -> CheckSnapshot:
    """Classify the decoded JSON list produced by ``gh pr checks --json``."""
    if not isinstance(checks, list) or not checks:
        return CheckSnapshot(state="no_checks")
    entries = tuple(c for c in checks if isinstance(c, dict))
    failing = tuple(c for c in entries if c.get("bucket") == "fail")
    if failing:
        return CheckSnapshot(state="failed", checks=entries, failing=failing)
    pending = tuple(c for c in entries if c.get("bucket") not in _SETTLED_BUCKETS)
    if pending:
        return CheckSnapshot(state="pending", checks=entries, pending=pending)
    return CheckSnapshot(state="green", checks=entries)


def parse_checks_output(returncode: int, stdout: str, stderr: str) -> CheckSnapshot:
    """Classify a finished ``gh pr checks`` invocation (exit code + streams)."""
    if returncode != 0:
        combined = f"{stdout}\n{stderr}".lower()
        if "no checks reported" in combined or "no commit found" in combined:
            return CheckSnapshot(state="no_checks")
        return CheckSnapshot(state="error", error=stderr if stderr.strip() else stdout)
    try:
        decoded = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return CheckSnapshot(state="error", error="gh pr checks returned invalid JSON")
    return classify_checks(decoded)


def query_checks(pr_number: int, cwd: Path, runner: GhRunner) -> CheckSnapshot:
    """Run ``gh pr checks`` for a PR and classify the result."""
    result = runner(
        ["pr", "checks", str(pr_number), "--json", "bucket,name,link,workflow"], cwd
    )
    return parse_checks_output(result.returncode, result.stdout or "", result.stderr or "")


def extract_run_id(check: dict[str, Any]) -> Optional[str]:
    """Return the Actions run id embedded in a check's ``link``, if any."""
    match = _RUN_LINK_PATTERN.search(str(check.get("link") or ""))
    return match.group(1) if match else None


def fetch_failed_log(
    runner: GhRunner, cwd: Path, failing_check: dict[str, Any], *, max_lines: int = LOG_TAIL_LINES
) -> str:
    """Download the last ``max_lines`` of the failing job's log (``--log-failed``)."""
    run_id = extract_run_id(failing_check)
    if run_id is None:
        return "(nao foi possivel localizar o run id do check falho)"
    result = runner(["run", "view", run_id, "--log-failed"], cwd)
    stdout = result.stdout or ""
    text = stdout if stdout.strip() else (result.stderr or "")
    return "\n".join(text.splitlines()[-max_lines:])


# ----------------------------------------------------------------------
# Base branch comparison
# ----------------------------------------------------------------------


def _load_json(result: GhResultLike) -> Any:
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        return None


def latest_conclusive_run(
    runner: GhRunner,
    cwd: Path,
    branch: str,
    workflow: str = "",
    *,
    limit: int = 10,
) -> Optional[dict[str, Any]]:
    """Return the newest completed run of ``branch`` that was not cancelled/skipped."""
    args = ["run", "list", "--branch", branch, "--status", "completed", "--limit", str(limit)]
    if workflow:
        args += ["--workflow", workflow]
    args += ["--json", "databaseId,conclusion,headSha,url,workflowName,displayTitle"]
    runs = _load_json(runner(args, cwd))
    if not isinstance(runs, list):
        return None
    for run in runs:
        if isinstance(run, dict) and str(run.get("conclusion") or "") not in _INCONCLUSIVE_CONCLUSIONS:
            return run
    return None


def run_jobs(runner: GhRunner, cwd: Path, run_id: Any) -> Optional[list[dict[str, Any]]]:
    """List the jobs of a run (``gh run view <id> --json jobs``); ``None`` on failure."""
    data = _load_json(runner(["run", "view", str(run_id), "--json", "jobs"], cwd))
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return None
    return [j for j in data["jobs"] if isinstance(j, dict)]


def red_jobs_of(jobs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter a job list down to the failed ones."""
    return [j for j in jobs if str(j.get("conclusion") or "") in _RED_CONCLUSIONS]


def base_job_candidates(name: str) -> list[str]:
    """Names under which a PR check may appear in a base-branch (push) run."""
    candidates = [name]
    for pr_name, base_name in _PR_JOB_TO_BASE_JOB.items():
        if name == pr_name or name.startswith(f"{pr_name} "):
            candidates.append(base_name + name[len(pr_name):])
    return candidates


def base_branch_is_red(
    runner: GhRunner, cwd: Path, base_branch: str, failing: Sequence[dict[str, Any]]
) -> bool:
    """True when EVERY failing PR check is already red on the base branch.

    For each failing workflow the last conclusive run of ``base_branch`` is
    inspected; the PR check must match (same job, allowing the PR/push name
    aliases) a failed job there. If any failing check is green, unknown or
    absent on the base, the PR itself broke something and this returns False.
    """
    if not failing:
        return False
    by_workflow: dict[str, list[dict[str, Any]]] = {}
    for check in failing:
        by_workflow.setdefault(str(check.get("workflow") or ""), []).append(check)
    for workflow, group in by_workflow.items():
        run = latest_conclusive_run(runner, cwd, base_branch, workflow)
        if run is None or str(run.get("conclusion") or "") not in _RED_CONCLUSIONS:
            return False
        jobs = run_jobs(runner, cwd, run.get("databaseId"))
        if jobs is None:
            return False
        red_names = {str(j.get("name") or "") for j in red_jobs_of(jobs)}
        for check in group:
            if not red_names.intersection(base_job_candidates(str(check.get("name") or ""))):
                return False
    return True


def classify_base_red(
    runner: GhRunner, cwd: Path, base_branch: str, failing: Sequence[dict[str, Any]]
) -> bool:
    """:func:`base_branch_is_red` that never raises and never waits for CI.

    Every ``gh`` failure (timeout, missing binary, malformed JSON) means "could not prove the base is
    red", so it returns False: the caller then treats the red check as the PR's own fault, which is
    the conservative reading (the agent gets the log and iterates).
    """
    try:
        return base_branch_is_red(runner, cwd, base_branch, failing)
    except Exception as exc:
        logger.warning("Could not compare with %s: %s", base_branch, sanitize(str(exc)))
        return False


# ----------------------------------------------------------------------
# `base_red` wait policy (USR-86)
# ----------------------------------------------------------------------
#
# A PR whose only red checks are already red on the base branch is not the agent's fault: no change
# the agent makes can fix it, so iterating the development stage would only burn agent calls. The
# production line therefore returns `retry` with ``base_red not_before=<iso>`` (an hourly wait, the
# same ``not_before`` convention as ``ci_pending``) up to BASE_RED_MAX_RETRIES times, and escalates
# to `waiting_human(kind=infra)` afterwards. The wait owns its cap (counted by the integration
# stage), so ``core.workflow.successors`` exempts it from the run wall-clock bound that rules
# ``ci_pending``: six hourly waits would otherwise be cut short by the 6 h run budget.

BASE_RED_CAUSE = "base_red"
BASE_RED_RETRY_MINUTES = 60
BASE_RED_MAX_RETRIES = 6

_BASE_RED_CAUSE_PATTERN = re.compile(rf"^{BASE_RED_CAUSE}(?:\s|$)")


def base_red_cause_code(not_before: datetime) -> str:
    """``base_red not_before=<iso>`` (45 characters, well under the 64 persisted)."""
    if not_before.tzinfo is None:
        not_before = not_before.replace(tzinfo=timezone.utc)
    stamp = not_before.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return f"{BASE_RED_CAUSE} not_before={stamp}"


def is_base_red_cause(cause_code: Optional[str]) -> bool:
    """True for a `retry` cause code produced by :func:`base_red_cause_code`."""
    return bool(cause_code) and _BASE_RED_CAUSE_PATTERN.match(cause_code or "") is not None


# ----------------------------------------------------------------------
# The gate
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class CiVerdict:
    """Typed outcome of :func:`ensure_green`."""

    status: CiStatus
    failed_checks: tuple[str, ...] = ()
    pending_checks: tuple[str, ...] = ()
    log_tail: str = ""
    base_red: bool = False
    waited_s: float = 0.0
    detail: str = ""

    @property
    def passed(self) -> bool:
        """True when merging is allowed (green, or the repo reports no checks)."""
        return self.status in ("green", "no_checks")


def resolve_wait_seconds(timeout_s: Optional[float] = None) -> float:
    """Explicit value, else ``DARKFAC_CI_WAIT_SECONDS``, else 1200 s."""
    if timeout_s is not None:
        return max(0.0, float(timeout_s))
    return _env_seconds(CI_WAIT_ENV, DEFAULT_CI_WAIT_SECONDS)


def _poll_checks(pr_number: int, cwd: Path, runner: GhRunner) -> CheckSnapshot:
    try:
        return query_checks(pr_number, cwd, runner)
    except Exception as exc:  # a flaky gh must never turn into a merge
        return CheckSnapshot(state="error", error=sanitize(str(exc)))


def _failed_log_tail(runner: GhRunner, cwd: Path, failing: Sequence[dict[str, Any]]) -> str:
    """Sanitized log tail of the failing job(s), at most two distinct runs."""
    seen: set[str] = set()
    chosen: list[dict[str, Any]] = []
    for check in failing:
        run_id = extract_run_id(check) or ""
        if run_id in seen:
            continue
        seen.add(run_id)
        chosen.append(check)
        if len(chosen) == _MAX_LOG_RUNS:
            break
    per_run = LOG_TAIL_CHARS // max(len(chosen), 1)
    parts: list[str] = []
    for check in chosen:
        try:
            raw = fetch_failed_log(runner, cwd, check)
        except Exception as exc:
            raw = f"(failed to fetch log: {exc})"
        tail = condense_log(raw, per_run)
        parts.append(f"[{check_label(check)}]\n{tail}" if len(chosen) > 1 else tail)
    return "\n\n".join(parts)


def ensure_green(
    pr_number: int,
    cwd: Path,
    runner: GhRunner,
    *,
    timeout_s: Optional[float] = None,
    poll_s: Optional[float] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    clock_fn: Optional[Callable[[], float]] = None,
    base_branch: str = "main",
    no_checks_grace_s: Optional[float] = None,
) -> CiVerdict:
    """Wait for a PR's checks and return whether merging is allowed.

    * any failing check returns ``failed`` immediately (fail fast), carrying the
      failing check names, a sanitized log tail and ``base_red``;
    * pending checks (and transient ``gh`` errors) are polled every ``poll_s``
      until ``timeout_s`` (default ``DARKFAC_CI_WAIT_SECONDS``, 1200 s), then the
      verdict is ``pending_timeout``;
    * "no checks reported" is only accepted as ``no_checks`` after a short grace
      window (``DARKFAC_CI_NO_CHECKS_GRACE_SECONDS``, 30 s, capped by the
      timeout) because GitHub registers workflow runs a few seconds after a push;
    * all checks settled without failure returns ``green``.

    Elapsed time is ``max(clock delta, total requested sleep)`` so a stubbed
    ``sleep_fn`` cannot turn the wait into a busy loop on a real clock.
    """
    timeout = resolve_wait_seconds(timeout_s)
    poll = DEFAULT_POLL_SECONDS if poll_s is None else max(0.0, float(poll_s))
    grace = (
        _env_seconds(NO_CHECKS_GRACE_ENV, DEFAULT_NO_CHECKS_GRACE_SECONDS)
        if no_checks_grace_s is None
        else max(0.0, float(no_checks_grace_s))
    )
    grace = min(grace, timeout)
    sleep = sleep_fn or time.sleep
    clock = clock_fn or time.monotonic

    started = clock()
    slept = 0.0
    last_error = ""
    while True:
        snapshot = _poll_checks(pr_number, cwd, runner)
        elapsed = max(clock() - started, slept)

        if snapshot.state == "failed":
            labels = tuple(check_label(c) for c in snapshot.failing)
            log_tail = _failed_log_tail(runner, cwd, snapshot.failing)
            base_red = classify_base_red(runner, cwd, base_branch, snapshot.failing)
            logger.warning(
                "CI failed on PR #%s: %s (base_red=%s)", pr_number, ", ".join(labels), base_red
            )
            return CiVerdict(
                status="failed",
                failed_checks=labels,
                pending_checks=tuple(check_label(c) for c in snapshot.pending),
                log_tail=log_tail,
                base_red=base_red,
                waited_s=elapsed,
            )
        if snapshot.state == "green":
            return CiVerdict(status="green", waited_s=elapsed)
        if snapshot.state == "no_checks" and elapsed >= grace:
            return CiVerdict(
                status="no_checks", waited_s=elapsed, detail="gh reports no checks for the PR"
            )
        if snapshot.state == "error":
            last_error = snapshot.error

        if elapsed >= timeout:
            pending = tuple(check_label(c) for c in snapshot.pending)
            detail = f"{snapshot.state} after {elapsed:.0f}s"
            if snapshot.state == "error" and last_error:
                detail += f": {tail_chars(sanitize(last_error), 300)}"
            return CiVerdict(
                status="pending_timeout", pending_checks=pending, waited_s=elapsed, detail=detail
            )

        remaining = timeout - elapsed
        if snapshot.state == "no_checks":
            remaining = min(remaining, grace - elapsed)
        pause = max(min(poll, remaining), 0.0)
        sleep(pause)
        slept += pause


# ----------------------------------------------------------------------
# Ledger-only exception
# ----------------------------------------------------------------------


def is_ledger_only(paths: Iterable[str]) -> bool:
    """True when the change set is non-empty and touches only ledger/roadmap files."""
    normalized = [p.strip().replace("\\", "/") for p in paths if p and p.strip()]
    if not normalized:
        return False
    return all(
        ".." not in p.split("/") and any(p.startswith(prefix) for prefix in LEDGER_PREFIXES)
        for p in normalized
    )


# ----------------------------------------------------------------------
# Base branch probe (backs `check-main`)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class MainStatus:
    """State of the base branch CI, as seen on its last conclusive run."""

    state: MainState
    branch: str
    workflow: str
    sha: str = ""
    run_id: Optional[int] = None
    run_url: str = ""
    conclusion: str = ""
    red_jobs: tuple[tuple[str, str], ...] = ()
    detail: str = ""

    @property
    def short_sha(self) -> str:
        return self.sha[:7]

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view (used by ``check-main --json``)."""
        return {
            "state": self.state,
            "branch": self.branch,
            "workflow": self.workflow,
            "sha": self.sha,
            "short_sha": self.short_sha,
            "run_id": self.run_id,
            "run_url": self.run_url,
            "conclusion": self.conclusion,
            "red_jobs": [{"name": n, "url": u} for n, u in self.red_jobs],
            "detail": self.detail,
        }


def check_main(
    runner: GhRunner,
    cwd: Path,
    *,
    branch: str = "main",
    workflow: str = DEFAULT_MAIN_WORKFLOW,
) -> MainStatus:
    """Inspect the last conclusive ``workflow`` run on ``branch``."""
    listing = runner(
        [
            "run", "list", "--branch", branch, "--workflow", workflow, "--status", "completed",
            "--limit", "10", "--json", "databaseId,conclusion,headSha,url,workflowName,displayTitle",
        ],
        cwd,
    )
    if listing.returncode != 0:
        return MainStatus(
            state="unknown",
            branch=branch,
            workflow=workflow,
            detail=tail_chars(sanitize(listing.stderr or listing.stdout or "gh run list failed"), 500),
        )
    try:
        runs = json.loads(listing.stdout or "[]")
    except json.JSONDecodeError:
        runs = None
    run = None
    if isinstance(runs, list):
        for candidate in runs:
            if isinstance(candidate, dict) and str(candidate.get("conclusion") or "") not in _INCONCLUSIVE_CONCLUSIONS:
                run = candidate
                break
    if run is None:
        return MainStatus(
            state="unknown",
            branch=branch,
            workflow=workflow,
            detail=f"no conclusive {workflow!r} run found on {branch}",
        )

    conclusion = str(run.get("conclusion") or "")
    run_id = run.get("databaseId")
    common: dict[str, Any] = {
        "branch": branch,
        "workflow": workflow,
        "sha": str(run.get("headSha") or ""),
        "run_id": int(run_id) if isinstance(run_id, int) else None,
        "run_url": str(run.get("url") or ""),
        "conclusion": conclusion,
    }
    if conclusion == "success":
        return MainStatus(state="green", **common)

    jobs = run_jobs(runner, cwd, run_id)
    red = tuple(
        (str(j.get("name") or "?"), str(j.get("url") or "")) for j in red_jobs_of(jobs or [])
    )
    detail = "" if jobs is not None else "could not list the jobs of the failed run"
    return MainStatus(state="red", red_jobs=red, detail=detail, **common)
