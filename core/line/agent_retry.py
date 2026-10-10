"""Shared agent-failure policy for every caller that runs a coding agent (USR-68).

`DevelopmentStage` (the production line) and `run_ticket.py` (the launcher) used to disagree about what
a failed agent call means: the line excluded the crashing (harness, model) pair and put the harness in a
short cooldown, while the launcher printed the agent's (often empty) text and gave up. This module is the
one place that owns the policy, so both import it instead of re-implementing it:

- `exclude_route` / `pick_route`: the set of (harness, model) pairs that failed for the current job and
  the pick that skips them (falling back to them only when nothing else is routable, for the line);
- `is_stage_retryable`: failures that say nothing about the job (quota, login, missing binary), which the
  line retries later instead of spending one of a ticket's validate iterations;
- `classify_failure` + `run_with_retry`: the launcher loop. Transient failures (`empty_output`,
  `timeout`, `crash`) repeat on the same route with backoff and then exclude the harness; a capability or
  binary problem (`unsupported_mode`, `not_installed`) and a login problem exclude it at once; a quota
  failure is recorded as a cooldown (`routing.record_result`, honouring `reset_at`) and excludes it.
  `protected_path` (USR-205) stops on the first attempt: no backoff, no other harness, and the worktree
  is left as the agent left it.

Everything is injectable (`run_func`, `pick_func`, `sleep_fn`, `record_func`) so tests drive it with fake
agents and no real waiting.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Optional, Sequence

from pydantic import BaseModel, Field

from core.line import diagnostics
from core.line.agent_cli import AgentRequest, AgentResult, run_agent
from core.line.routing import RoutingConfig, pick, record_result

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

Route = tuple[str, Optional[str]]
FailureClass = Literal["transient", "exclude", "cooldown"]

# Failures that say nothing about the job: the line retries the whole stage later instead of burning
# one of the ticket's validate iterations.
STAGE_RETRY_ERRORS = frozenset({"rate_limited", "auth_expired", "not_installed"})

# `run_with_retry` classes (anything else, including a missing kind, is treated as transient).
COOLDOWN_ERRORS = frozenset({"rate_limited"})
EXCLUDE_NOW_ERRORS = frozenset({"unsupported_mode", "not_installed", "auth_expired", "no_authenticated_harness"})

TRANSIENT_RETRIES = 2
# Timeout policy (USR-114): at most 1 retry for timeouts, and if implementation changes are
# already present in the worktree, the flow follows to validate rather than repeating from scratch.
TIMEOUT_RETRIES = 1
BACKOFF_SECONDS: tuple[float, ...] = (30.0, 120.0)
MAX_ROUTES = 3

_OUTPUT_HEAD_CHARS = 500


def default_worktree_has_changes(cwd: Path) -> bool:
    """True when `cwd` has uncommitted implementation changes outside internal state (USR-114)."""
    try:
        resolved_cwd = cwd.resolve()
        if resolved_cwd == REPO_ROOT.resolve():
            # The shared repo root is not a ticket worktree; never treat root changes as ticket changes
            return False
        from core.git.autonomy import GitAutonomyManager

        mgr = GitAutonomyManager(REPO_ROOT)
        changes = [
            p
            for p in mgr.changed_paths(cwd)
            if not p.startswith(".factory/") and not p.startswith(".darkfac/")
        ]
        return len(changes) > 0
    except Exception:
        return False


# --------------------------------------------------------------------------
# Policy shared with DevelopmentStage
# --------------------------------------------------------------------------


def is_stage_retryable(result: AgentResult) -> bool:
    """True for a failed call the stage should retry later (quota, login, missing binary)."""
    return not result.ok and result.error_kind in STAGE_RETRY_ERRORS


def is_protected_path_block(result: AgentResult) -> bool:
    """True when the agent was cancelled while editing a guard-protected path (USR-205).

    Callers stop on this result: it is a human block, not a transient failure and not a reason
    to try another harness on the same scope.
    """
    return not result.ok and result.error_kind == "protected_path"


def exclude_route(excluded: set[Route], harness: str, model: Optional[str]) -> None:
    """Remember a (harness, model) pair that failed, so the next pick for this job skips it."""
    excluded.add((harness, model))


def pick_route(
    pick_func: Callable[..., Optional[Route]],
    stage: str,
    host_caps: Iterable[str],
    config: Optional[RoutingConfig],
    excluded: set[Route],
    *,
    retry_without_exclusions: bool = True,
    **pick_kwargs: Any,
) -> Optional[Route]:
    """Next (harness, model) skipping `excluded`.

    When nothing else is routable the line falls back to the excluded pairs (`retry_without_exclusions`)
    rather than stopping; the launcher passes False and gives up instead.
    """
    if excluded:
        route = pick_func(stage, host_caps, config=config, exclude=set(excluded), **pick_kwargs)
        if route is not None:
            return route
        if not retry_without_exclusions:
            return None
        logger.warning("Every %s route already failed for this ticket; retrying without exclusions", stage)
    return pick_func(stage, host_caps, config=config, **pick_kwargs)


# --------------------------------------------------------------------------
# Launcher loop
# --------------------------------------------------------------------------


def classify_failure(result: AgentResult) -> FailureClass:
    """`cooldown` (quota), `exclude` (capability/binary/login: retrying the harness is pointless) or `transient`."""
    kind = result.error_kind
    if kind in COOLDOWN_ERRORS:
        return "cooldown"
    if kind in EXCLUDE_NOW_ERRORS:
        return "exclude"
    return "transient"


def backoff_delay(
    result: AgentResult,
    retry_no: int,
    *,
    transient_retries: int = TRANSIENT_RETRIES,
    timeout_retries: int = TIMEOUT_RETRIES,
    backoff_s: Sequence[float] = BACKOFF_SECONDS,
) -> Optional[float]:
    """Seconds to wait before repeating the same route, or None when the route must not be repeated (USR-151).

    This is the ONLY place that decides a backoff, and `run_with_retry` calls `sleep_fn` only with the
    value returned here. Only a `transient` failure (`empty_output`, `crash`, `timeout`, a missing or
    unknown `error_kind`) that still has retries left waits; `not_installed`, `unsupported_mode`, login
    failures and quota failures return None, so the loop falls straight through to the next healthy route
    without a single sleep.
    """
    if result.error_kind in {"cancelled", "protected_path"}:
        return None
    if classify_failure(result) != "transient":
        return None
    effective_retries = timeout_retries if result.error_kind == "timeout" else transient_retries
    if retry_no >= effective_retries:
        return None
    return backoff_s[min(retry_no, len(backoff_s) - 1)] if backoff_s else 0.0


class AgentAttempt(BaseModel):
    """One agent invocation, with the evidence needed to diagnose it (secret-redacted)."""

    number: int
    harness: str
    model: Optional[str] = None
    ok: bool
    error_kind: Optional[str] = None
    exit_code: Optional[int] = None
    duration_s: float = 0.0
    stderr_tail: str = ""
    output_head: str = ""

    @classmethod
    def from_result(cls, number: int, result: AgentResult) -> "AgentAttempt":
        return cls(
            number=number,
            harness=result.harness,
            model=result.model,
            ok=result.ok,
            error_kind=result.error_kind,
            exit_code=result.exit_code,
            duration_s=result.duration_s,
            stderr_tail=diagnostics.redacted_head(result.stderr_tail),
            output_head=diagnostics.redacted_head(result.text, _OUTPUT_HEAD_CHARS),
        )


class RetryReport(BaseModel):
    """Outcome of `run_with_retry`: the final result plus every attempt that led to it."""

    ok: bool
    result: Optional[AgentResult] = None
    route: Optional[Route] = None
    attempts: list[AgentAttempt] = Field(default_factory=list)
    # USR-205: the launcher must not delete the worktree after a protected-path block.
    preserve_worktree: bool = False

    @property
    def total_duration_s(self) -> float:
        return round(sum(a.duration_s for a in self.attempts), 3)


def run_with_retry(
    build_request: Callable[[str, Optional[str]], AgentRequest],
    route: Optional[Route],
    *,
    host_caps: Iterable[str],
    stage: str = "development",
    mode: str = "write",
    config: Optional[RoutingConfig] = None,
    run_func: Callable[[AgentRequest], AgentResult] = run_agent,
    pick_func: Callable[..., Optional[Route]] = pick,
    sleep_fn: Callable[[float], None] = time.sleep,
    record_func: Callable[..., None] = record_result,
    pinned: bool = False,
    max_routes: int = MAX_ROUTES,
    transient_retries: int = TRANSIENT_RETRIES,
    timeout_retries: int = TIMEOUT_RETRIES,
    backoff_s: Sequence[float] = BACKOFF_SECONDS,
    has_changes_fn: Optional[Callable[[Path], bool]] = None,
    on_event: Optional[Callable[[str], None]] = None,
) -> RetryReport:
    """Run an agent on `route` until one attempt succeeds, walking to the next healthy route on failure.

    - transient failure (`empty_output`, `crash`, unknown): repeat on the same route up to
      `transient_retries` times, sleeping `backoff_s[i]` (30s, then 120s) before repeat i, then exclude
      the route and move on;
    - `timeout` failure (USR-114): repeat at most `timeout_retries` (1) time on the same route. Furthermore,
      if implementation changes are already present in the worktree, advance directly to validation instead
      of repeating from scratch or burning quota.
    - `unsupported_mode` / `not_installed` / login failures: exclude the route immediately, never sleeping
      (the only path that sleeps is `backoff_delay`, which returns a delay for transient failures alone);
    - quota (`rate_limited`): `record_func` (routing's `record_result`) stores a cooldown from `reset_at`,
      and the route is excluded.

    At most `max_routes` routes are tried. `pinned` (an explicit `--harness`) keeps the caller's choice:
    transient retries still happen but the loop never falls through to another harness. `pick_func` must
    accept `exclude=`, `config=` and `mode=`. `build_request(harness, model)` builds each request.
    """
    caps = list(host_caps)
    excluded: set[Route] = set()
    attempts: list[AgentAttempt] = []

    def emit(message: str) -> None:
        logger.warning(message)
        if on_event is not None:
            on_event(message)

    if route is None:
        route = pick_func(stage, caps, config=config, mode=mode)

    last_result: Optional[AgentResult] = None
    last_route: Optional[Route] = route
    routes_tried = 0
    while route is not None and routes_tried < max_routes:
        routes_tried += 1
        harness, model = route
        max_loop_retries = max(transient_retries, timeout_retries)
        stopped_due_to_changes = False
        for retry_no in range(max_loop_retries + 1):
            req = build_request(harness, model)
            result = run_func(req)
            record_func(result, config=config)
            attempt = AgentAttempt.from_result(len(attempts) + 1, result)
            attempts.append(attempt)
            last_result, last_route = result, route
            if result.ok:
                return RetryReport(ok=True, result=result, route=route, attempts=attempts)

            if result.error_kind == "cancelled":
                # USR-152: the run was cancelled; never retry, back off or fall back to another route.
                return RetryReport(ok=False, result=result, route=route, attempts=attempts)

            if is_protected_path_block(result):
                # USR-205: the same edit is cancelled again on every harness. Stop on the first
                # attempt and leave whatever the agent already wrote in the worktree.
                emit(
                    "Protected-path edit was cancelled"
                    + (f" ({result.protected_path})" if result.protected_path else "")
                    + "; stopping on the first attempt without retry or another harness"
                )
                return RetryReport(
                    ok=False, result=result, route=route, attempts=attempts, preserve_worktree=True,
                )

            failure = classify_failure(result)
            emit(
                f"Agent attempt {attempt.number} failed: harness={harness} model={model or '-'} "
                f"error_kind={result.error_kind or '-'} exit_code={attempt.exit_code if attempt.exit_code is not None else '-'} "
                f"duration_s={attempt.duration_s} class={failure}"
            )

            # USR-114: Timeout policy: at most 1 retry.
            # If implementation changes are already present in the worktree after a timeout,
            # stop retrying immediately rather than repeating the route from scratch.
            if result.error_kind == "timeout" and mode == "write":
                check_fn = has_changes_fn or default_worktree_has_changes
                if check_fn(req.cwd):
                    emit(
                        f"Timeout on {harness}, but implementation changes are already present in worktree; "
                        "stopping retries so work is not overwritten or repeated from scratch"
                    )
                    stopped_due_to_changes = True
                    break

            delay = backoff_delay(
                result, retry_no,
                transient_retries=transient_retries, timeout_retries=timeout_retries, backoff_s=backoff_s,
            )
            if delay is None:
                break
            emit(f"Transient failure on {harness}; retrying the same route in {delay:g}s")
            sleep_fn(delay)

        if stopped_due_to_changes:
            break

        exclude_route(excluded, harness, model)
        if pinned:
            break
        route = pick_route(
            pick_func, stage, caps, config, excluded, retry_without_exclusions=False, mode=mode
        )
        if route is not None:
            emit(f"Falling back to {route[0]}" + (f":{route[1]}" if route[1] else ""))

    if last_result is None:
        last_result = AgentResult(
            ok=False,
            text=f"no route available for the '{stage}' stage in '{mode}' mode",
            harness="none", duration_s=0.0, error_kind="no_authenticated_harness",
        )
    return RetryReport(ok=False, result=last_result, route=last_route, attempts=attempts)
