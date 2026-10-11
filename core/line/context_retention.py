"""Retention sweep for the per-run recovery refs `refs/heads/darkfac-context/<run_id>` (USR-169, OA-005 option A).

`core.line.recovery_context.preserve_context` pins the pre-strip commit of every run in the target
repository so a rollback/retry on another worker can still read the run's planning inputs. Nothing ever
removed those refs, so they piled up forever on the remote. This module is the cleanup.

Policy ("clean up 14 days after the terminal state", documented in `docs/runbooks/vps_disk.md`, section 10):

- A ref is removed ONLY when the control store knows the run, the run is in a TERMINAL state, it has no live
  job and it reached that state at least `DARKFAC_CONTEXT_RETENTION_DAYS` days ago (default 14). Age is
  measured from the terminal instant (`completed_at`, else `updated_at`), never from the commit date.
- Everything else keeps the ref (fail-closed): runs this worker is executing now, unknown/orphan runs (there
  is no terminal state to count 14 days from), non-terminal runs, live jobs, a run of another project,
  missing/invalid timestamps, a failed status lookup, and any ref whose name is not a valid
  `darkfac-context/<run_id>`. Branches such as `df/*`, `ticket/*` and the default branch are never touched.
- Removal is safe and idempotent: the run is re-read right before deleting (a reopened run keeps its ref) and
  the delete is a compare-and-swap (`--force-with-lease=<ref>:<sha_read>`), so a ref that was re-pinned in
  the meantime is kept (`lease_lost`), and a ref that is already gone is not an error.
- `DARKFAC_CONTEXT_RETENTION_DRY_RUN=1` lists what would be removed and pushes nothing;
  `DARKFAC_CONTEXT_RETENTION_DAYS=0` disables the sweep (no git call at all).

All git access goes through `core.line.workspace._run_git` (token authentication for GitHub, sanitized
errors). A tiny empty bare repository under the workspace root is the working directory for the remote calls:
the sweep needs neither a worktree nor a checkout nor a fetch of the project mirror.

The sweep is scheduled by `CloudWorker` (every 6h, next to the workspace and artifact sweeps) because it is
the process that holds the control-store connection needed to know which runs are alive.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Optional

from pydantic import BaseModel, ConfigDict, Field

from core.line import recovery_context, workspace
from core.line.workspace import TERMINAL_RUN_STATUSES
from core.projects.models import ProjectDescriptor
from core.projects.registry import normalize_repo_url

logger = logging.getLogger("darkfac.context_retention")

DEFAULT_CONTEXT_RETENTION_DAYS = 14.0
CONTEXT_REF_PREFIX = "refs/heads/darkfac-context/"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_SCRATCH_DIRNAME = ".context-retention.git"
_GIT_TIMEOUT_S = 120
_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")

# Sentinel handed to `_decide` when the control-store lookup raised (distinct from "run unknown" = None).
_LOOKUP_FAILED: Any = object()


class ContextRetentionConfig(BaseModel):
    """Retention knobs resolved from the environment."""

    model_config = ConfigDict(frozen=True)

    terminal_max_age_days: float = DEFAULT_CONTEXT_RETENTION_DAYS
    dry_run: bool = False

    @property
    def enabled(self) -> bool:
        return self.terminal_max_age_days > 0


def _env_days(env: Mapping[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default  # NaN/inf/negative -> default


def context_retention_config_from_env(env: Optional[Mapping[str, str]] = None) -> ContextRetentionConfig:
    """Resolve `DARKFAC_CONTEXT_RETENTION_DAYS` (0 disables) and `DARKFAC_CONTEXT_RETENTION_DRY_RUN`.

    Empty, invalid, negative or non-finite values fall back to the conservative default (14 days).
    """
    env_map: Mapping[str, str] = os.environ if env is None else env
    return ContextRetentionConfig(
        terminal_max_age_days=_env_days(
            env_map, "DARKFAC_CONTEXT_RETENTION_DAYS", DEFAULT_CONTEXT_RETENTION_DAYS
        ),
        dry_run=(env_map.get("DARKFAC_CONTEXT_RETENTION_DRY_RUN") or "").strip().lower() in _TRUTHY,
    )


class ContextSweepEntry(BaseModel):
    """One recovery ref the sweep removed (or would remove in dry-run); `sha` allows re-pinning it by hand."""

    model_config = ConfigDict(frozen=True)

    project_id: str
    run_id: str
    ref: str
    sha: str
    reason: str


class ContextSweepReport(BaseModel):
    """Outcome of one sweep.

    `scanned == kept + len(removed) + len(would_remove)`; `lease_lost` and `already_absent` refs are
    counted in `kept` (with their reason in `kept_reasons`) because this sweep did not delete them.
    """

    dry_run: bool
    scanned: int = 0
    kept: int = 0
    kept_reasons: dict[str, int] = Field(default_factory=dict)
    removed: list[ContextSweepEntry] = Field(default_factory=list)
    would_remove: list[ContextSweepEntry] = Field(default_factory=list)
    lease_lost: int = 0
    errors: list[str] = Field(default_factory=list)

    @property
    def removed_ids(self) -> list[str]:
        return [entry.run_id for entry in (self.would_remove if self.dry_run else self.removed)]


class _Decision(NamedTuple):
    remove: bool
    reason: str


def _keep(reason: str) -> _Decision:
    return _Decision(False, reason)


def _decide(
    run_id: str,
    status: Any,
    *,
    project_id: str,
    protected: frozenset[str],
    moment: float,
    cfg: ContextRetentionConfig,
) -> _Decision:
    """Pure retention decision for one ref: remove (with the reason) or keep (with the reason).

    `status` is the control-store view of the run: a mapping, `None` (run unknown) or `_LOOKUP_FAILED`.
    """
    if run_id in protected:
        return _keep("protected")
    if status is _LOOKUP_FAILED:
        return _keep("status_lookup_failed")
    if status is None:
        return _keep("unknown_run")  # no terminal instant to count the retention from
    if not isinstance(status, Mapping):
        return _keep("status_lookup_failed")
    owner = status.get("project_id")
    if owner not in (None, "") and str(owner) != project_id:
        return _keep("project_mismatch")
    jobs = status.get("jobs") or []
    if any(
        str(job.get("status")) in workspace._LIVE_JOB_STATUSES for job in jobs if isinstance(job, Mapping)
    ):
        return _keep("live_job")
    if str(status.get("status")) not in TERMINAL_RUN_STATUSES:
        return _keep("not_terminal")
    finished = workspace._parse_ts(status.get("completed_at")) or workspace._parse_ts(status.get("updated_at"))
    if finished is None:
        return _keep("no_timestamp")
    if (moment - finished) / 86400.0 < cfg.terminal_max_age_days:
        return _keep("too_recent")
    return _Decision(True, f"terminal_older_than_{cfg.terminal_max_age_days:g}d")


def sweep_log_payload(report: ContextSweepReport, config: ContextRetentionConfig) -> dict[str, Any]:
    """Structured (JSON-serializable) summary logged by every sweep."""
    return {
        "dry_run": report.dry_run,
        "terminal_max_age_days": config.terminal_max_age_days,
        "scanned": report.scanned,
        "kept": report.kept,
        "kept_reasons": dict(report.kept_reasons),
        "removed": [entry.model_dump() for entry in report.removed],
        "would_remove": [entry.model_dump() for entry in report.would_remove],
        "lease_lost": report.lease_lost,
        "errors": report.errors,
    }


# --------------------------------------------------------------------------
# Remote access (read-only listing + compare-and-swap delete)
# --------------------------------------------------------------------------


def _scratch_repo(base: Path, project_id: str) -> Path:
    """An empty bare repository used only as the working directory of the remote calls."""
    path = base / project_id / _SCRATCH_DIRNAME
    if not (path / "HEAD").is_file():
        path.mkdir(parents=True, exist_ok=True)
        workspace._run_git(["init", "--bare", "--quiet"], cwd=path)
    return path


def _parse_ls_remote(output: str) -> list[tuple[str, str]]:
    """`(ref name, sha)` pairs of a `git ls-remote` listing; malformed lines are skipped."""
    pairs: list[tuple[str, str]] = []
    for line in output.splitlines():
        sha, _, name = line.strip().partition("\t")
        if _SHA_RE.fullmatch(sha) and name:
            pairs.append((name, sha))
    return pairs


def _list_context_refs(scratch: Path, repo_url: str) -> list[tuple[str, str]]:
    """Every ref the remote lists for the context namespace pattern (one read-only `ls-remote`)."""
    result = workspace._run_git(
        ["ls-remote", "--refs", repo_url, f"{CONTEXT_REF_PREFIX}*"],
        cwd=scratch,
        repo_url=repo_url,
        timeout=_GIT_TIMEOUT_S,
    )
    return sorted(_parse_ls_remote(result.stdout or ""))


def _remote_sha(scratch: Path, repo_url: str, ref: str) -> Optional[str]:
    """Current remote sha of exactly `ref` (ls-remote matches by tail, so the name is compared in full)."""
    result = workspace._run_git(
        ["ls-remote", "--refs", repo_url, ref], cwd=scratch, repo_url=repo_url, timeout=_GIT_TIMEOUT_S
    )
    for name, sha in _parse_ls_remote(result.stdout or ""):
        if name == ref:
            return sha
    return None


def _run_id_of(ref: str) -> Optional[str]:
    """The run id of a well-formed `refs/heads/darkfac-context/<run_id>` ref name, else None."""
    if not ref.startswith(CONTEXT_REF_PREFIX):
        return None
    run_id = ref[len(CONTEXT_REF_PREFIX):]
    if not recovery_context._RUN_ID_RE.fullmatch(run_id):
        return None
    return run_id if recovery_context.context_ref(run_id) == ref else None


class _DeleteOutcome(NamedTuple):
    state: str  # "removed" | "already_absent" | "lease_lost" | "failed"
    detail: str = ""


def _delete_if_unchanged(scratch: Path, repo_url: str, ref: str, sha: str) -> _DeleteOutcome:
    """Delete `ref` only while it still points at `sha` (compare-and-swap)."""
    push = workspace._run_git(
        ["push", f"--force-with-lease={ref}:{sha}", repo_url, f":{ref}"],
        cwd=scratch,
        repo_url=repo_url,
        check=False,
        timeout=_GIT_TIMEOUT_S,
    )
    if push.returncode == 0:
        return _DeleteOutcome("removed")
    detail = workspace._sanitize((push.stderr or "").strip(), workspace._github_token_for(repo_url))
    current = _remote_sha(scratch, repo_url, ref)
    if current is None:
        return _DeleteOutcome("already_absent")  # removed by another worker in the meantime
    if current != sha:
        return _DeleteOutcome("lease_lost")  # re-pinned after we read it: not ours to delete
    return _DeleteOutcome("failed", f"push exit {push.returncode}: {detail}")


# --------------------------------------------------------------------------
# Sweep
# --------------------------------------------------------------------------


def _record_keep(report: ContextSweepReport, reason: str) -> None:
    report.kept += 1
    report.kept_reasons[reason] = report.kept_reasons.get(reason, 0) + 1


def _lookup(
    run_id: str,
    run_status: Callable[[str], Optional[Mapping[str, Any]]],
    label: str,
    report: ContextSweepReport,
) -> Any:
    try:
        return run_status(run_id)
    except Exception as exc:  # fail closed: unknown store state keeps the evidence
        logger.warning("context sweep: status lookup failed for %s (%s); keeping", label, exc)
        report.errors.append(f"{label}: status lookup failed: {exc}")
        return _LOOKUP_FAILED


def _sweep_ref(
    *,
    project: ProjectDescriptor,
    scratch: Path,
    repo_url: str,
    ref: str,
    sha: str,
    run_status: Callable[[str], Optional[Mapping[str, Any]]],
    protected: frozenset[str],
    moment: float,
    cfg: ContextRetentionConfig,
    report: ContextSweepReport,
) -> None:
    run_id = _run_id_of(ref)
    if run_id is None:
        _record_keep(report, "invalid_ref_name")
        return
    if run_id in protected:  # a run this worker executes now: no store lookup needed
        _record_keep(report, "protected")
        return
    label = f"{project.id}/{run_id}"
    decision = _decide(
        run_id,
        _lookup(run_id, run_status, label, report),
        project_id=project.id,
        protected=protected,
        moment=moment,
        cfg=cfg,
    )
    if not decision.remove:
        _record_keep(report, decision.reason)
        return
    entry = ContextSweepEntry(
        project_id=project.id, run_id=run_id, ref=ref, sha=sha, reason=decision.reason
    )
    if cfg.dry_run:
        report.would_remove.append(entry)
        return
    # The run may have been reopened after the first look: decide again immediately before deleting.
    recheck = _decide(
        run_id,
        _lookup(run_id, run_status, label, report),
        project_id=project.id,
        protected=protected,
        moment=moment,
        cfg=cfg,
    )
    if not recheck.remove:
        _record_keep(report, recheck.reason)
        return
    outcome = _delete_if_unchanged(scratch, repo_url, ref, sha)
    if outcome.state == "removed":
        report.removed.append(entry)
    elif outcome.state == "already_absent":
        _record_keep(report, "already_absent")
    elif outcome.state == "lease_lost":
        report.lease_lost += 1
        _record_keep(report, "lease_lost")
    else:
        logger.warning("context sweep: failed to remove %s (%s): %s", ref, label, outcome.detail)
        report.errors.append(f"{label}: remove failed: {outcome.detail}")
        _record_keep(report, "remove_failed")  # the next sweep repeats it


def _sweep_project(
    *,
    project: ProjectDescriptor,
    base: Path,
    run_status: Callable[[str], Optional[Mapping[str, Any]]],
    protected: frozenset[str],
    moment: float,
    cfg: ContextRetentionConfig,
    report: ContextSweepReport,
) -> None:
    repo_url = normalize_repo_url(project.repo_url or "")
    if not repo_url or repo_url.startswith("-"):
        report.errors.append(f"{project.id}: unusable repo_url; skipped")
        return
    scratch = _scratch_repo(base, project.id)
    for ref, sha in _list_context_refs(scratch, repo_url):
        report.scanned += 1
        try:
            _sweep_ref(
                project=project,
                scratch=scratch,
                repo_url=repo_url,
                ref=ref,
                sha=sha,
                run_status=run_status,
                protected=protected,
                moment=moment,
                cfg=cfg,
                report=report,
            )
        except Exception as exc:  # one bad ref never aborts the sweep
            logger.warning("context sweep: %s/%s failed: %s", project.id, ref, exc)
            report.errors.append(f"{project.id}/{ref}: {exc}")
            _record_keep(report, "sweep_error")


def sweep_recovery_contexts(
    *,
    projects: Iterable[ProjectDescriptor],
    run_status: Callable[[str], Optional[Mapping[str, Any]]],
    protected_run_ids: Iterable[str] = (),
    config: Optional[ContextRetentionConfig] = None,
    now: Optional[float] = None,
    root: Optional[Path] = None,
) -> ContextSweepReport:
    """Apply the retention policy to the `darkfac-context/*` refs of every project that has a `repo_url`.

    `run_status` has the shape of `store.get_run_status` (`status`, `completed_at`, `updated_at`,
    `project_id`, `jobs[].status`; `None` for an unknown run). `now` is epoch seconds (default: the clock).
    A project, ref or git error never aborts the sweep: it is reported in `errors` and the ref is kept.
    """
    cfg = config or context_retention_config_from_env()
    report = ContextSweepReport(dry_run=cfg.dry_run)
    if not cfg.enabled:
        return report
    moment = time.time() if now is None else now
    protected = frozenset(protected_run_ids)
    base = Path(root) if root is not None else workspace.workspace_root()

    for project in projects:
        if not project.repo_url:
            continue
        try:
            _sweep_project(
                project=project,
                base=base,
                run_status=run_status,
                protected=protected,
                moment=moment,
                cfg=cfg,
                report=report,
            )
        except Exception as exc:  # a project (git/network/disk failure) never aborts the sweep
            logger.warning("context sweep: project %s failed: %s", project.id, exc)
            report.errors.append(f"{project.id}: {exc}")

    logger.info("context_retention_sweep %s", json.dumps(sweep_log_payload(report, cfg), sort_keys=True))
    return report
