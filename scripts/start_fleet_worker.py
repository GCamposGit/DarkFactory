"""Distributed Fleet Worker Lifecycle and Inventory Management (USR-286 / USR-277).

Manages lifecycle (start, drain, stop, status, probe, rollback) for the three-host
DarkFac fleet topology:
- Grokbox (Linux, Priority 1, Capacity 1)
- Notebook (Windows, Priority 2, Capacity 1)
- Desktop (Windows, Priority 3, Capacity 1)

Key contracts:
1. Exactly 1 cycle per host, maximum 3 cycles fleet-wide.
2. Priority: Grokbox (1) > Notebook (2) > Desktop (3).
3. Decoupled from model routing: does not pin harness or model.
4. Idempotent bootstrap: safe to invoke repeatedly without duplicate processes.
5. Graceful drain: stops claiming new cycles while letting in-flight work finish.
6. Safe rolling update: restarting one host preserves peer host cycles.
7. Rollback: suspends admission while preserving worktrees and evidence.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("darkfac.fleet_worker")

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_PIDS_DIR = _REPO_ROOT / ".factory" / "pids"
_FLEET_CONFIG_PATH = _REPO_ROOT / ".factory" / "config" / "line_fleet.json"

FLEET_HOST_PRIORITIES: dict[str, int] = {
    "grokbox": 1,
    "notebook": 2,
    "desktop": 3,
}

FLEET_PRIORITY_LABELS: dict[str, str] = {
    "grokbox": "primary",
    "notebook": "secondary",
    "desktop": "fallback",
}


class HostInventory(BaseModel):
    """Static and detected capabilities of a fleet host machine."""

    host_id: str = Field(description="Logical host identifier: grokbox, notebook, desktop")
    hostname: str = Field(description="Host machine network name")
    platform: str = Field(description="OS platform: linux or windows")
    priority: int = Field(ge=1, description="Host priority: 1=Grokbox, 2=Notebook, 3=Desktop")
    max_slots: int = Field(default=1, description="Capacity ceiling: 1 cycle per host")
    database_connected: bool = Field(default=False, description="Postgres control store connectivity")
    database_status: str = Field(default="unknown", description="Postgres probe status: ready, blocked, or error")
    database_diagnostic: Optional[str] = Field(default=None, description="Sanitized diagnostic message")
    projects_accessible: dict[str, bool] = Field(default_factory=dict, description="Project path reachability")
    tooling: dict[str, bool] = Field(default_factory=dict, description="Availability of git, gh, node, python")
    harnesses: dict[str, bool] = Field(default_factory=dict, description="Detected CLI harnesses on PATH")
    tags: list[str] = Field(default_factory=list, description="Host descriptive tags")
    probed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WorkerLifecycleStatus(BaseModel):
    """Runtime status of a local fleet worker daemon."""

    host_id: str
    running: bool = False
    pid: Optional[int] = None
    draining: bool = False
    priority: int = 2
    max_slots: int = 1
    pid_file: Optional[str] = None
    drain_file: Optional[str] = None
    log_file: Optional[str] = None


def discover_local_host_id() -> str:
    """Infer logical host_id from environment, hostname, or OS family."""
    env_host = os.environ.get("DARKFAC_HOST_ID", "").strip().lower()
    if env_host in FLEET_HOST_PRIORITIES:
        return env_host

    hostname = platform.node().lower()
    if "grok" in hostname or "vm-418" in hostname:
        return "grokbox"
    if "desktop" in hostname or "g45ipem" in hostname:
        return "desktop"
    if "notebook" in hostname or "ai_notebook" in hostname or "ai-notebook" in hostname:
        return "notebook"

    # Default to platform family heuristic
    if sys.platform.startswith("linux"):
        return "grokbox"
    return "notebook"


def get_pid_file(host_id: str) -> Path:
    return _PIDS_DIR / f"fleet_worker_{host_id}.pid"


def get_drain_file(host_id: str) -> Path:
    return _PIDS_DIR / f"fleet_worker_{host_id}.drain"


def get_log_file(host_id: str) -> Path:
    log_dir = _REPO_ROOT / ".factory" / "test_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"fleet_worker_{host_id}.log"


def is_pid_alive(pid: int) -> bool:
    """Check if process with given PID is currently alive."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            process = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not process:
                return False
            kernel32.CloseHandle(process)
            return True
        except Exception:
            try:
                proc = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return str(pid) in proc.stdout
            except Exception:
                return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def probe_host_inventory(
    host_id: Optional[str] = None,
    database_url: Optional[str] = None,
    repo_root: Optional[Path] = None,
) -> HostInventory:
    """Perform a structured probe of the host machine's capabilities and tooling."""
    root = repo_root or _REPO_ROOT
    hid = host_id or discover_local_host_id()
    plat = "linux" if hid == "grokbox" else "windows"
    priority = FLEET_HOST_PRIORITIES.get(hid, 2)

    # 1. Tooling check
    tooling = {
        "git": bool(shutil.which("git")),
        "gh": bool(shutil.which("gh")),
        "node": bool(shutil.which("node")),
        "python": bool(shutil.which("python")),
    }

    # 2. Harnesses check (presence on PATH, unpinned)
    harnesses = {
        "claude": bool(shutil.which("claude")),
        "codex": bool(shutil.which("codex")),
        "grok": bool(shutil.which("grok")),
        "antigravity": bool(shutil.which("antigravity")),
    }

    # 3. Database connectivity
    db_url = database_url or os.environ.get("DARKFAC_HF02_DATABASE_URL")
    db_connected = False
    db_status = "blocked"
    db_diagnostic: Optional[str] = None
    try:
        from core.orchestrator.cloud_db import probe_cloud_database, sanitize_database_url

        probe = probe_cloud_database(db_url)
        db_status = getattr(probe, "status", "error")
        db_connected = (db_status == "ready")
        if not db_connected:
            err_msg = getattr(probe, "error_message", None)
            db_diagnostic = sanitize_database_url(str(err_msg or f"database probe status: {db_status}"))
    except Exception as exc:
        from core.orchestrator.cloud_db import sanitize_database_url

        db_status = "error"
        db_connected = False
        db_diagnostic = sanitize_database_url(str(exc))

    # 4. Project accessibility
    projects_acc: dict[str, bool] = {"darkfac": root.exists()}
    projects_json = root / ".factory" / "projects.json"
    if projects_json.exists():
        try:
            data = json.loads(projects_json.read_text(encoding="utf-8"))
            for p in data.get("projects", []):
                pid = p.get("id")
                path_str = p.get("path")
                if pid and path_str:
                    projects_acc[pid] = Path(path_str).exists()
        except Exception:
            pass

    tags = ["fleet", hid, plat]
    if hid == "desktop":
        tags.append("test_worker")
    elif hid == "grokbox":
        tags.append("remote_linux")

    return HostInventory(
        host_id=hid,
        hostname=platform.node(),
        platform=plat,
        priority=priority,
        max_slots=1,
        database_connected=db_connected,
        database_status=db_status,
        database_diagnostic=db_diagnostic,
        projects_accessible=projects_acc,
        tooling=tooling,
        harnesses=harnesses,
        tags=tags,
    )


def get_worker_status(host_id: Optional[str] = None) -> WorkerLifecycleStatus:
    """Inspect current lifecycle status of the fleet worker on this host."""
    hid = host_id or discover_local_host_id()
    pid_file = get_pid_file(hid)
    drain_file = get_drain_file(hid)
    log_file = get_log_file(hid)
    priority = FLEET_HOST_PRIORITIES.get(hid, 2)

    pid: Optional[int] = None
    alive = False
    if pid_file.exists():
        try:
            content = pid_file.read_text(encoding="utf-8").strip()
            if content.isdigit():
                pid = int(content)
                alive = is_pid_alive(pid)
        except Exception:
            alive = False

    draining = drain_file.exists()

    return WorkerLifecycleStatus(
        host_id=hid,
        running=alive,
        pid=pid if alive else None,
        draining=draining,
        priority=priority,
        max_slots=1,
        pid_file=str(pid_file),
        drain_file=str(drain_file) if draining else None,
        log_file=str(log_file),
    )


def bootstrap_fleet_worker(
    host_id: Optional[str] = None,
    headless: bool = False,
    database_url: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Start or verify the fleet worker daemon idempotently."""
    hid = host_id or discover_local_host_id()
    status = get_worker_status(hid)

    # 1. Idempotency: if already running, do nothing and return existing PID
    if status.running:
        logger.info("Fleet worker for host '%s' already running (PID %s).", hid, status.pid)
        return {
            "status": "already_running",
            "host_id": hid,
            "pid": status.pid,
            "priority": status.priority,
            "max_slots": 1,
            "draining": status.draining,
        }

    # Clear stale drain file if starting afresh
    drain_file = get_drain_file(hid)
    if drain_file.exists():
        drain_file.unlink(missing_ok=True)

    _PIDS_DIR.mkdir(parents=True, exist_ok=True)
    pid_file = get_pid_file(hid)
    log_file = get_log_file(hid)

    priority_label = FLEET_PRIORITY_LABELS.get(hid, "secondary")
    priority_num = FLEET_HOST_PRIORITIES.get(hid, 2)

    if dry_run:
        return {
            "status": "dry_run_ready",
            "host_id": hid,
            "priority": priority_num,
            "priority_label": priority_label,
            "max_slots": 1,
        }

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(_REPO_ROOT)
    env["DARKFAC_HOST_ID"] = hid
    env["DARKFAC_WORKER_ID"] = f"fleet-{hid}"
    env["DARKFAC_WORKER_PRIORITY"] = priority_label
    env["DARKFAC_MAX_CONCURRENT_SLOTS"] = "1"
    if database_url:
        env["DARKFAC_HF02_DATABASE_URL"] = database_url

    cmd = [
        sys.executable,
        "-u",
        "-m",
        "core.orchestrator.cloud_worker",
    ]

    log_handle = open(log_file, "a", encoding="utf-8", buffering=1)
    if headless:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS

        proc = subprocess.Popen(
            cmd,
            cwd=str(_REPO_ROOT),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        pid = proc.pid
        pid_file.write_text(str(pid), encoding="utf-8")
        logger.info("Spawned headless fleet worker '%s' (PID %s).", hid, pid)
        return {
            "status": "started_headless",
            "host_id": hid,
            "pid": pid,
            "priority": priority_num,
            "max_slots": 1,
            "log_file": str(log_file),
        }
    else:
        # Foreground mode
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        try:
            from core.orchestrator.cloud_worker import CloudWorker

            worker = CloudWorker(
                worker_id=f"fleet-{hid}",
                host_id=hid,
                max_slots=1,
                autodetect_tooling=True,
            )
            worker.run()
        finally:
            pid_file.unlink(missing_ok=True)
        return {
            "status": "exited",
            "host_id": hid,
            "priority": priority_num,
            "max_slots": 1,
        }


def drain_fleet_worker(host_id: Optional[str] = None, reason: str = "administrative_drain") -> dict[str, Any]:
    """Signal worker to stop claiming new cycles while letting in-flight work finish."""
    hid = host_id or discover_local_host_id()
    _PIDS_DIR.mkdir(parents=True, exist_ok=True)
    drain_file = get_drain_file(hid)
    drain_data = {
        "host_id": hid,
        "drained_at": datetime.now(UTC).isoformat(),
        "reason": reason,
    }
    drain_file.write_text(json.dumps(drain_data, indent=2), encoding="utf-8")

    status = get_worker_status(hid)
    logger.info("Host '%s' set to DRAINING status. Active PID: %s", hid, status.pid)
    return {
        "status": "draining",
        "host_id": hid,
        "active_pid": status.pid,
        "reason": reason,
        "drain_file": str(drain_file),
    }


def stop_fleet_worker(host_id: Optional[str] = None, force: bool = False) -> dict[str, Any]:
    """Gracefully terminate local fleet worker process."""
    hid = host_id or discover_local_host_id()
    pid_file = get_pid_file(hid)
    drain_file = get_drain_file(hid)

    status = get_worker_status(hid)
    if not status.running or not status.pid:
        pid_file.unlink(missing_ok=True)
        drain_file.unlink(missing_ok=True)
        return {"status": "not_running", "host_id": hid}

    pid = status.pid
    if sys.platform == "win32":
        flags = ["/F", "/PID", str(pid)] if force else ["/PID", str(pid)]
        subprocess.run(["taskkill", *flags], capture_output=True, check=False)
    else:
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.kill(pid, sig)
        except OSError:
            pass

    # Clean up lock/state files
    pid_file.unlink(missing_ok=True)
    drain_file.unlink(missing_ok=True)

    return {"status": "stopped", "host_id": hid, "pid": pid}


def rollback_fleet_worker(host_id: Optional[str] = None) -> dict[str, Any]:
    """Suspend admission for this host, preserving worktrees and evidence."""
    hid = host_id or discover_local_host_id()

    # 1. Set drain to prevent any new cycle admissions
    drain_result = drain_fleet_worker(hid, reason="fleet_rollback")

    # 2. Verify worktrees and evidence paths are kept intact
    worktrees_dir = _REPO_ROOT / ".worktrees"
    evidence_dir = _REPO_ROOT / ".factory" / "test_logs"

    return {
        "status": "admission_suspended",
        "host_id": hid,
        "drain": drain_result,
        "worktrees_preserved": worktrees_dir.exists(),
        "evidence_preserved": evidence_dir.exists(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="DarkFac Distributed Fleet Worker CLI (USR-286)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # probe
    p_probe = subparsers.add_parser("probe", help="Probe host inventory and tooling")
    p_probe.add_argument("--host", choices=["grokbox", "notebook", "desktop"], default=None)
    p_probe.add_argument("--json", action="store_true", help="Output JSON format")

    # status
    p_status = subparsers.add_parser("status", help="Get worker lifecycle status")
    p_status.add_argument("--host", choices=["grokbox", "notebook", "desktop"], default=None)
    p_status.add_argument("--json", action="store_true", help="Output JSON format")

    # start
    p_start = subparsers.add_parser("start", help="Bootstrap and start fleet worker")
    p_start.add_argument("--host", choices=["grokbox", "notebook", "desktop"], default=None)
    p_start.add_argument("--headless", action="store_true", help="Run detached background daemon")
    p_start.add_argument("--db-url", default=None, help="Postgres database URL")

    # drain
    p_drain = subparsers.add_parser("drain", help="Drain worker (finish in-flight, reject new)")
    p_drain.add_argument("--host", choices=["grokbox", "notebook", "desktop"], default=None)
    p_drain.add_argument("--reason", default="administrative_drain", help="Reason for drain")

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop fleet worker process")
    p_stop.add_argument("--host", choices=["grokbox", "notebook", "desktop"], default=None)
    p_stop.add_argument("--force", action="store_true", help="Force terminate process")

    # rollback
    p_rollback = subparsers.add_parser("rollback", help="Suspend admission and preserve state")
    p_rollback.add_argument("--host", choices=["grokbox", "notebook", "desktop"], default=None)

    args = parser.parse_args()

    if args.command == "probe":
        inv = probe_host_inventory(args.host)
        if args.json:
            print(inv.model_dump_json(indent=2))
        else:
            print(f"Host ID             : {inv.host_id}")
            print(f"Platform            : {inv.platform} ({inv.hostname})")
            print(f"Priority / MaxSlots : {inv.priority} / {inv.max_slots}")
            print(f"Database Connected  : {inv.database_connected}")
            print(f"Tooling (git/gh/py) : {inv.tooling}")
            print(f"Harnesses Detected  : {inv.harnesses}")
            print(f"Projects Reachable  : {inv.projects_accessible}")

    elif args.command == "status":
        st = get_worker_status(args.host)
        if args.json:
            print(st.model_dump_json(indent=2))
        else:
            print(f"Host ID   : {st.host_id}")
            print(f"Running   : {st.running} (PID: {st.pid})")
            print(f"Draining  : {st.draining}")
            print(f"Priority  : {st.priority} (MaxSlots: {st.max_slots})")
            print(f"Log File  : {st.log_file}")

    elif args.command == "start":
        res = bootstrap_fleet_worker(args.host, headless=args.headless, database_url=args.db_url)
        print(json.dumps(res, indent=2))

    elif args.command == "drain":
        res = drain_fleet_worker(args.host, reason=args.reason)
        print(json.dumps(res, indent=2))

    elif args.command == "stop":
        res = stop_fleet_worker(args.host, force=args.force)
        print(json.dumps(res, indent=2))

    elif args.command == "rollback":
        res = rollback_fleet_worker(args.host)
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
