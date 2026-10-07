"""Retention sweep for the `darkfac-artifacts` volume (USR-148, 2026-10-06 VPS disk incident).

Policy (documented in `docs/runbooks/vps_disk.md`, section 7):

- The unit of retention is one workflow directory `<root>/<workflow_id>/` written by
  `core.orchestrator.cloud_artifacts.CloudArtifactStore` (the workflow id is the run id).
- Removed only when the control store says the run is TERMINAL, it has no live job, it finished more than
  `DARKFAC_ARTIFACT_RETENTION_DAYS` ago (default 30) AND nothing under the directory was modified for that
  long.
- A directory the store does not know (orphan) goes only after `DARKFAC_ARTIFACT_ORPHAN_RETENTION_DAYS`
  (default 60, never shorter than the terminal retention) of disk inactivity.
- Never removed: runs this worker is executing now, runs that are active / waiting_* / not terminal or have a
  live job (pending/running/retry/replan/waiting_*), recent runs, and anything whose status lookup raised
  (fail closed). Files directly under the root (ledgers, `disk_guard_state.json`) and dot-directories are never
  touched; symlinks are never followed.
- `DARKFAC_ARTIFACT_RETENTION_DRY_RUN=1` only logs what would be removed (no deletion).
  `DARKFAC_ARTIFACT_RETENTION_DAYS=0` disables the sweep.

The sweep is scheduled by `CloudWorker` (every 6h, next to the workspace sweep) because it is the process
that holds the control-store connection needed to know which runs are alive.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("darkfac.artifact_retention")

DEFAULT_ARTIFACT_RETENTION_DAYS = 30.0
DEFAULT_ARTIFACT_ORPHAN_RETENTION_DAYS = 60.0

# Kept in sync with core.line.workspace (a test asserts parity); duplicated to keep this module dependency-free.
TERMINAL_RUN_STATUSES = frozenset({"completed", "cancelled", "failed", "succeeded"})
LIVE_JOB_STATUSES = frozenset({"pending", "running", "retry", "replan", "waiting_dependency", "waiting_human"})

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class ArtifactRetentionConfig(BaseModel):
    """Retention knobs resolved from the environment."""

    model_config = ConfigDict(frozen=True)

    terminal_max_age_days: float = DEFAULT_ARTIFACT_RETENTION_DAYS
    orphan_max_age_days: float = DEFAULT_ARTIFACT_ORPHAN_RETENTION_DAYS
    dry_run: bool = False

    @property
    def enabled(self) -> bool:
        return self.terminal_max_age_days > 0


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default  # NaN fails the comparison too


def retention_config_from_env(env: Optional[Mapping[str, str]] = None) -> ArtifactRetentionConfig:
    """Resolve `DARKFAC_ARTIFACT_RETENTION_DAYS`, `DARKFAC_ARTIFACT_ORPHAN_RETENTION_DAYS` and
    `DARKFAC_ARTIFACT_RETENTION_DRY_RUN`; invalid values fall back to the conservative defaults."""
    env_map: Mapping[str, str] = os.environ if env is None else env
    terminal = _env_float(env_map, "DARKFAC_ARTIFACT_RETENTION_DAYS", DEFAULT_ARTIFACT_RETENTION_DAYS)
    orphan = _env_float(env_map, "DARKFAC_ARTIFACT_ORPHAN_RETENTION_DAYS", DEFAULT_ARTIFACT_ORPHAN_RETENTION_DAYS)
    return ArtifactRetentionConfig(
        terminal_max_age_days=terminal,
        orphan_max_age_days=max(orphan, terminal),
        dry_run=(env_map.get("DARKFAC_ARTIFACT_RETENTION_DRY_RUN") or "").strip().lower() in _TRUTHY,
    )


class ArtifactSweepEntry(BaseModel):
    """One workflow directory the sweep removed (or would remove in dry-run)."""

    model_config = ConfigDict(frozen=True)

    workflow_id: str
    reason: str
    bytes: int


class ArtifactSweepReport(BaseModel):
    """Outcome of one sweep; `bytes_freed` counts real removals, `bytes_reclaimable` the dry-run ones."""

    dry_run: bool
    scanned: int = 0
    kept: int = 0
    removed: list[ArtifactSweepEntry] = Field(default_factory=list)
    would_remove: list[ArtifactSweepEntry] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    bytes_freed: int = 0
    bytes_reclaimable: int = 0

    @property
    def removed_ids(self) -> list[str]:
        return [entry.workflow_id for entry in (self.would_remove if self.dry_run else self.removed)]


def _parse_ts(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _tree_stats(entry: Path) -> tuple[float, int]:
    """(newest mtime, total file bytes) of a directory tree, without following symlinks."""
    newest = entry.lstat().st_mtime
    total = 0
    for dirpath, dirnames, filenames in os.walk(entry, followlinks=False):
        for name in dirnames:
            try:
                newest = max(newest, os.lstat(os.path.join(dirpath, name)).st_mtime)
            except OSError:
                continue
        for name in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            newest = max(newest, st.st_mtime)
            total += st.st_size
    return newest, total


def sweep_log_payload(report: ArtifactSweepReport, config: ArtifactRetentionConfig) -> dict[str, Any]:
    """Structured (JSON-serializable) summary logged by every sweep."""
    return {
        "dry_run": report.dry_run,
        "terminal_max_age_days": config.terminal_max_age_days,
        "orphan_max_age_days": config.orphan_max_age_days,
        "scanned": report.scanned,
        "kept": report.kept,
        "removed": [e.model_dump() for e in report.removed],
        "would_remove": [e.model_dump() for e in report.would_remove],
        "bytes_freed": report.bytes_freed,
        "bytes_reclaimable": report.bytes_reclaimable,
        "errors": report.errors,
    }


def _decide(
    status: Optional[dict], disk_age_days: float, moment: float, cfg: ArtifactRetentionConfig
) -> Optional[str]:
    """Return the removal reason, or None to keep the directory."""
    if status is None:
        if disk_age_days >= cfg.orphan_max_age_days:
            return f"orphan_inactive_{cfg.orphan_max_age_days:g}d"
        return None
    jobs = status.get("jobs") or []
    if any(str(j.get("status")) in LIVE_JOB_STATUSES for j in jobs if isinstance(j, dict)):
        return None
    if str(status.get("status")) not in TERMINAL_RUN_STATUSES:
        return None
    finished = _parse_ts(status.get("completed_at")) or _parse_ts(status.get("updated_at"))
    if finished is None:
        return None
    if (moment - finished) / 86400.0 < cfg.terminal_max_age_days or disk_age_days < cfg.terminal_max_age_days:
        return None
    return f"terminal_older_than_{cfg.terminal_max_age_days:g}d"


def sweep_stale_artifacts(
    *,
    root: Path,
    run_status: Callable[[str], Optional[dict]],
    protected_run_ids: Iterable[str] = (),
    config: Optional[ArtifactRetentionConfig] = None,
    now: Optional[float] = None,
) -> ArtifactSweepReport:
    """Apply the retention policy to the `<root>/<workflow_id>/` directories. One bad entry never aborts it."""
    cfg = config or retention_config_from_env()
    report = ArtifactSweepReport(dry_run=cfg.dry_run)
    base = Path(root)
    if not cfg.enabled or not base.is_dir():
        return report
    moment = time.time() if now is None else now
    protected = set(protected_run_ids)

    for entry in sorted(base.iterdir()):
        if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
            continue
        report.scanned += 1
        run_id = entry.name
        if run_id in protected:
            report.kept += 1
            continue
        try:
            newest, size = _tree_stats(entry)
        except OSError as exc:
            report.errors.append(f"{run_id}: stat failed: {exc}")
            report.kept += 1
            continue
        try:
            status = run_status(run_id)
        except Exception as exc:  # fail closed: unknown store state keeps the evidence
            logger.warning("artifact sweep: status lookup failed for %s (%s); keeping", run_id, exc)
            report.errors.append(f"{run_id}: status lookup failed: {exc}")
            report.kept += 1
            continue

        reason = _decide(status, (moment - newest) / 86400.0, moment, cfg)
        if reason is None:
            report.kept += 1
            continue
        item = ArtifactSweepEntry(workflow_id=run_id, reason=reason, bytes=size)
        if cfg.dry_run:
            report.would_remove.append(item)
            report.bytes_reclaimable += size
            continue
        try:
            shutil.rmtree(entry)
        except OSError as exc:
            logger.warning("artifact sweep: failed to remove %s: %s", entry, exc)
            report.errors.append(f"{run_id}: remove failed: {exc}")
            report.kept += 1  # partial removal is retried by the next sweep
            continue
        report.removed.append(item)
        report.bytes_freed += size

    logger.info("artifact_retention_sweep %s", json.dumps(sweep_log_payload(report, cfg), sort_keys=True))
    return report
