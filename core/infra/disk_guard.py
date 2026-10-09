"""Scheduled VPS disk guard: monitor, alert (80%/90%) and prune safely (2026-10-06 incident hardening).

Runs as a daemon thread of the `darkfac-backup-cron` service (see `core.infra.backup_cron`) or standalone:

    python -m core.infra.disk_guard --daemon --check-interval-minutes 30 --prune-interval-hours 6

Each cycle:
1. Read the HOST disk usage (inside a container `/` reports the filesystem backing /var/lib/docker, i.e.
   the VPS disk).
2. Prune unused Docker images (and the build cache when free space < `DARKFAC_VPS_MIN_FREE_GB`, default
   8 GB) through the Dokploy API, only when no deployment is running (`clean_vps_if_needed`). The images
   prune also runs unconditionally every `--prune-interval-hours` (default 6h).
3. Re-read the disk and send the owner-bot alert for the resulting level (80% warning, 90% critical, at most
   once per level per 6h) including the top consumers visible from here and `docker system df` when the
   Dokploy API provides it.

Volumes and running containers are never touched. A missing `DOKPLOY_API_URL`/`DOKPLOY_API_KEY` is reported
loudly in the alert and in the logs instead of being silently ignored (the USR-122 failure mode).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.infra import vps_cleanup as vc

logger = logging.getLogger("darkfac.infra.disk_guard")

DEFAULT_CHECK_INTERVAL_MINUTES = 30.0
DEFAULT_PRUNE_INTERVAL_HOURS = 6.0
DEFAULT_REPORT_PATHS = "/workspaces,/app/.factory/artifacts,/app/.factory/test_logs"
DEFAULT_TEST_LOG_DIR = "/app/.factory/test_logs"
DEFAULT_TEST_LOG_RETENTION_DAYS = 14.0


def default_state_path(env: Optional[Dict[str, str]] = None) -> Path:
    env_map = os.environ if env is None else env
    explicit = (env_map.get("DARKFAC_DISK_GUARD_STATE") or "").strip()
    if explicit:
        return Path(explicit)
    factory = Path(env_map.get("FACTORY_DIR") or ".factory")
    return factory / "artifacts" / "disk_guard_state.json"


def _report_paths(env: Dict[str, str]) -> List[str]:
    raw = env.get("DARKFAC_DISK_REPORT_PATHS") or DEFAULT_REPORT_PATHS
    return [p.strip() for p in raw.split(",") if p.strip()]


def run_cycle(
    *,
    env: Optional[Dict[str, str]] = None,
    transport: Optional[vc.Transport] = None,
    disk_probe: Optional[Callable[[], Dict[str, Any]]] = None,
    notifier: Optional[Callable[[str, str, str], bool]] = None,
    state_path: Optional[Path] = None,
    prune_due: bool = False,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """One guard cycle. Never raises; returns a summary dict."""
    env_map: Dict[str, str] = dict(os.environ) if env is None else dict(env)
    disk_path = env_map.get("DARKFAC_DISK_PATH") or "/"
    probe = disk_probe or (lambda: vc.get_local_disk_usage(disk_path))
    summary: Dict[str, Any] = {"cleanup": None, "alert": None}

    try:
        usage_before = probe()
        summary["usage_before"] = usage_before

        if transport is None and vc.running_under_pytest():
            api_url, api_key = None, None  # tests never use real Dokploy credentials
        else:
            api_url, api_key = vc.read_dokploy_credentials(env_map)
        active_transport = transport
        if active_transport is None and api_url and api_key:
            active_transport = vc.make_dokploy_transport(api_url, api_key, timeout=vc.PRUNE_TIMEOUT_SECONDS)
        notes: List[str] = []
        if active_transport is None:
            notes.append(
                "ATENCAO: DOKPLOY_API_URL/DOKPLOY_API_KEY ausentes neste servico; a limpeza automatica esta "
                "DESATIVADA (configure as variaveis no Dokploy)."
            )
            logger.warning("disk_guard: Dokploy credentials missing; automatic cleanup disabled")

        if active_transport is not None:
            summary["cleanup"] = vc.clean_vps_if_needed(
                transport=active_transport,
                disk_probe=probe,
                min_free_gb=vc.min_free_gb_from_env(env_map),
                force_images=prune_due,
                env=env_map,
                stage="scheduled",
            )
        else:
            summary["cleanup"] = {"action": "skipped", "reason": "no_credentials"}

        if prune_due:
            # Retention for unbounded test logs (volume darkfac-test-logs); logs are diagnostics, 14 days is plenty.
            try:
                retention = float(env_map.get("DARKFAC_TESTLOG_RETENTION_DAYS") or DEFAULT_TEST_LOG_RETENTION_DAYS)
            except ValueError:
                retention = DEFAULT_TEST_LOG_RETENTION_DAYS
            summary["test_logs"] = vc.prune_old_files(
                Path(env_map.get("DARKFAC_TESTLOG_DIR") or DEFAULT_TEST_LOG_DIR), max_age_days=retention, now=now
            )

        usage = probe()
        summary["usage"] = usage

        if vc.disk_alert_level(float(usage.get("disk_percent", 0.0))) is not None:
            consumers = [
                f"{path}: {vc.format_size_gb(size)}"
                for path, size in vc.collect_top_consumers(_report_paths(env_map))
            ]
            if active_transport is not None:
                consumers.extend(vc.format_docker_disk_usage(vc.fetch_docker_disk_usage(active_transport)))
            cleanup = summary.get("cleanup") or {}
            if cleanup.get("action") == "skipped" and cleanup.get("reason", "").startswith("deploy_in_progress"):
                notes.append("Limpeza adiada: deploy em andamento (" + cleanup["reason"].split(":", 1)[1] + ").")
            summary["alert"] = vc.evaluate_and_alert_disk(
                usage,
                state_path=(
                    state_path
                    if state_path is not None
                    else (None if vc.running_under_pytest() else default_state_path(env_map))
                ),
                now=now,
                consumers=consumers,
                notifier=notifier,
                mount=disk_path,
                notes=notes,
            )
    except Exception as exc:
        logger.error("disk_guard cycle failed: %s", exc, exc_info=True)
        summary["error"] = str(exc)
    return summary


def run_daemon(
    *,
    check_interval_minutes: float = DEFAULT_CHECK_INTERVAL_MINUTES,
    prune_interval_hours: float = DEFAULT_PRUNE_INTERVAL_HOURS,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
    max_cycles: Optional[int] = None,
    cycle_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> None:
    """Loops `run_cycle`; the unconditional images prune happens every `prune_interval_hours`."""
    interval = max(60.0, check_interval_minutes * 60.0)
    prune_every = max(interval, prune_interval_hours * 3600.0)
    runner = cycle_fn or run_cycle
    last_prune: Optional[float] = None
    cycles = 0
    logger.info(
        "Disk guard started: check every %.0f min, scheduled prune every %.1f h", interval / 60.0, prune_every / 3600.0
    )
    while max_cycles is None or cycles < max_cycles:
        current = clock_fn()
        prune_due = last_prune is None or current - last_prune >= prune_every
        result = runner(prune_due=prune_due)
        cleanup = (result or {}).get("cleanup") or {}
        # A finished prune (action=cleaned) or a disk that did not need one (disk_ok) resets the
        # timer. A running deploy, missing credentials, or action=incomplete after a cleanUnusedImages
        # read timeout stays due and is retried on the next cycle, not inside the timed-out call.
        if prune_due and (cleanup.get("action") == "cleaned" or cleanup.get("reason") == "disk_ok"):
            last_prune = current
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        sleep_fn(interval)


def start_background_guard(
    *,
    check_interval_minutes: float = DEFAULT_CHECK_INTERVAL_MINUTES,
    prune_interval_hours: float = DEFAULT_PRUNE_INTERVAL_HOURS,
) -> Optional[Any]:
    """Starts the guard loop in a daemon thread (used by the backup-cron daemon). 0 minutes disables it."""
    if check_interval_minutes <= 0:
        return None
    import threading

    thread = threading.Thread(
        target=run_daemon,
        kwargs={"check_interval_minutes": check_interval_minutes, "prune_interval_hours": prune_interval_hours},
        name="darkfac-disk-guard",
        daemon=True,
    )
    thread.start()
    return thread


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Dark Factory VPS disk guard")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and print the summary")
    parser.add_argument("--daemon", action="store_true", help="Run continuously")
    parser.add_argument("--prune", action="store_true", help="With --once: force the images prune now")
    parser.add_argument("--check-interval-minutes", type=float, default=DEFAULT_CHECK_INTERVAL_MINUTES)
    parser.add_argument("--prune-interval-hours", type=float, default=DEFAULT_PRUNE_INTERVAL_HOURS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if args.daemon:
        run_daemon(
            check_interval_minutes=args.check_interval_minutes, prune_interval_hours=args.prune_interval_hours
        )
        return 0
    summary = run_cycle(prune_due=args.prune)
    print(summary)
    return 1 if summary.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
