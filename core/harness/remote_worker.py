"""
Remote Headless Test Execution Daemon for DarkFac.

Designed to run on the on-premises dedicated server (desktop-g45ipem, Tailscale 100.78.181.90)
or inside a Docker container on Drive E: (3 TB), providing headless test offloading for agents
and developer workstations without cluttering the interactive laptop.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hmac
import json
import logging
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Ensure repo root is in sys.path
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.harness import cache as harness_cache
from core.harness.markers import MARKER_HARNESS_RESULT
from core.harness.runner import _selected_steps as _harness_selected_steps
from core.harness.runner import load_config as _load_harness_config
from core.harness.test_subagent import (
    DistilledTestReport,
    TestExecutionInstruction,
    TestSubagentEngine,
)

logger = logging.getLogger("core.harness.remote_worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

DEFAULT_WORKER_NODE_ID = "onprem-z97-server"
DEFAULT_WORKER_PORT = 8080
DEFAULT_WORKER_HOST = "0.0.0.0"
ENV_WORKER_TOKEN = "DARKFAC_WORKER_TOKEN"
ENV_WORKER_STATE_DIR = "DARKFAC_WORKER_STATE_DIR"
JOB_LOG_KEEP = 20
JOB_WATCHDOG_POLL_SEC = 1.0
DEFAULT_JOB_TIMEOUT_SEC = 1800.0
JOB_TIMEOUT_BUFFER_SEC = 120.0


class WorkerHealthStatus(BaseModel):
    """Health and operational capability status of the remote test runner node."""
    status: str = Field(default="ok", description="Health status string: ok, degraded, busy")
    node_id: str = Field(default=DEFAULT_WORKER_NODE_ID, description="Target node slug identifier")
    version: str = Field(default="1.0.0", description="Worker service version")
    docker_ready: bool = Field(default=True, description="Whether Docker runtime is accessible on Drive E:")
    project_root: str = Field(..., description="Root directory where tests execute")
    active_runs: int = Field(default=0, description="Currently running test jobs")
    available_harnesses: list[str] = Field(default_factory=list, description="Supported AI harnesses detected on this host")
    # HF-27-11: remote dispatch of the official validation harness -----------
    platform_family: str = Field(default="windows", description="'windows' or 'posix', for cache-key bundle negotiation")
    python_version: str = Field(default="", description="'major.minor' of the interpreter running this worker")
    hostname: str = Field(default="", description="socket.gethostname() of this worker, for self-dispatch detection")
    busy: bool = Field(default=False, description="True while a harness job is actively running (single worker thread)")
    queue_length: int = Field(default=0, description="Number of jobs queued or running on this worker")
    known_shas: list[str] = Field(
        default_factory=list,
        description="origin/main SHA + recent validated candidate SHAs, for thin `git bundle --not` negotiation",
    )
    harness_version: str = Field(default="1", description="Remote harness job protocol version")


class CommandExecutionRequest(BaseModel):
    """Payload to execute an arbitrary system command on the remote node."""
    command: str = Field(..., description="Command string or script to run")
    cwd: Optional[str] = Field(default=None, description="Working directory (defaults to repo root)")
    timeout_seconds: int = Field(default=120, description="Command execution timeout in seconds")


class CommandExecutionResponse(BaseModel):
    """Result of command execution on remote worker node."""
    exit_code: int = Field(..., description="Process return code")
    stdout: str = Field(default="", description="Captured standard output")
    stderr: str = Field(default="", description="Captured standard error")
    duration_seconds: float = Field(..., description="Execution duration in seconds")
    success: bool = Field(..., description="True if exit code is 0")


class SystemUpdateRequest(BaseModel):
    """Request to update repo on worker node via git pull."""
    branch: Optional[str] = Field(default=None, description="Branch to checkout/pull (defaults to current)")


class SystemUpdateResponse(BaseModel):
    """Result of repo update on worker node."""
    success: bool = Field(..., description="True if git pull succeeded")
    output: str = Field(default="", description="Git command output")
    current_commit: str = Field(default="", description="Commit SHA after update")


class HarnessExecutionRequest(BaseModel):
    """Generic payload to execute a prompt on any supported headless harness."""
    harness: str = Field(default="codex", description="Target harness identifier: codex, grok, antigravity, claude, deepseek")
    prompt: str = Field(..., description="Prompt or instructions for the harness")
    system_prompt: Optional[str] = Field(default=None, description="Optional system instructions")
    timeout_seconds: int = Field(default=120, description="Execution timeout in seconds")
    model: Optional[str] = Field(default=None, description="Optional model override")
    cwd: Optional[str] = Field(default=None, description="Working directory")
    options: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary harness options")


class HarnessExecutionResponse(BaseModel):
    """Standardized response from any headless harness execution."""
    success: bool = Field(..., description="True if harness executed successfully")
    harness: str = Field(..., description="Harness used for execution")
    text: str = Field(default="", description="Generated response content or code")
    model: str = Field(default="", description="Model identified by harness")
    tokens_used: int = Field(default=0, description="Tokens consumed")
    duration_seconds: float = Field(default=0.0, description="Execution duration in seconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    node_id: str = Field(default="", description="Worker node ID where execution ran")


class CodexExecutionRequest(BaseModel):
    """Payload to execute prompt non-interactively using local Codex CLI."""
    prompt: str = Field(..., description="Prompt or instructions for Codex agent")
    system_prompt: Optional[str] = Field(default=None, description="Optional system instructions")
    timeout_seconds: int = Field(default=120, description="Execution timeout in seconds")
    model: Optional[str] = Field(default=None, description="Optional model override")
    cwd: Optional[str] = Field(default=None, description="Working directory")


class CodexExecutionResponse(BaseModel):
    """Result of Codex execution on the node."""
    success: bool = Field(..., description="True if Codex executed successfully")
    text: str = Field(default="", description="Generated text / code / patch")
    model: str = Field(default="gpt-5.6-sol", description="Actual model used by Codex")
    tokens_used: int = Field(default=0, description="Tokens recorded by Codex CLI")
    duration_seconds: float = Field(..., description="Duration of execution in seconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


# --- HF-27-11: async harness job protocol (remote dispatch server half) ----


def _check_auth(request: Request) -> None:
    """Optional bearer-token auth for mutating endpoints. Constant-time
    compare; the token itself is never logged. A no-op when
    ``DARKFAC_WORKER_TOKEN`` is unset on this worker (``/health`` always
    stays open, regardless)."""

    token = os.environ.get(ENV_WORKER_TOKEN, "").strip()
    if not token:
        return
    provided = request.headers.get("authorization", "")
    expected = f"Bearer {token}"
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def default_worker_state_dir() -> Path:
    """Machine-local scratch dir for bundles/worktrees/job logs (not inside
    the repo -- a stray worktree here must never pollute `git status`)."""

    configured = os.environ.get(ENV_WORKER_STATE_DIR)
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "DarkFac" / "worker"
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state_home) if xdg_state_home else Path.home() / ".local" / "state"
    return base / "darkfac" / "worker"


def known_validated_shas(root_path: Path, *, limit: int = 20) -> list[str]:
    """origin/main SHA (if resolvable) + this host's most recently cached
    PASS candidate SHAs, for the client's thin ``git bundle --not`` bundle
    negotiation."""

    shas: list[str] = []
    proc = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=str(root_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode == 0:
        sha = proc.stdout.strip().lower()
        if sha:
            shas.append(sha)

    with contextlib.suppress(Exception):
        store = harness_cache._read_local_store()  # noqa: SLF001 - worker-internal negotiation only
        dated: list[tuple[float, str]] = []
        for record in store.values():
            if not isinstance(record, dict):
                continue
            result = record.get("result") or {}
            candidate_sha = (result.get("candidate_sha") or record.get("candidate_sha") or "").lower()
            if candidate_sha:
                dated.append((float(record.get("created_at", 0.0)), candidate_sha))
        dated.sort(key=lambda item: item[0], reverse=True)
        for _created_at, candidate_sha in dated[:limit]:
            if candidate_sha not in shas:
                shas.append(candidate_sha)

    return shas[:limit]


def _extract_harness_result(log_text: str) -> Optional[Dict[str, Any]]:
    """Parse the last ``[HARNESS_RESULT] {...}`` line out of a job's log."""

    result: Optional[Dict[str, Any]] = None
    prefix = f"{MARKER_HARNESS_RESULT} "
    for line in log_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            with contextlib.suppress(json.JSONDecodeError):
                result = json.loads(stripped[len(prefix):])
    return result


def _job_timeout_sec(worktree_path: Path, *, quick: bool, include_holdout: bool) -> float:
    try:
        config, _config_hash = _load_harness_config(worktree_path / "harness.config.json")
        steps = _harness_selected_steps(config, quick=quick, include_holdout=include_holdout)
        if steps:
            return float(sum(step.timeout_sec for step in steps)) + JOB_TIMEOUT_BUFFER_SEC
    except Exception as exc:  # noqa: BLE001 - never let timeout computation crash a job
        logger.warning("Could not compute job timeout from worktree config: %s", exc)
    return DEFAULT_JOB_TIMEOUT_SEC


@dataclass
class HarnessJob:
    """One remote-dispatched harness run: queued -> running -> done|failed|cancelled."""

    job_id: str
    candidate_sha: str
    tree_sha: str
    quick: bool
    include_holdout: bool
    requesting_host: str
    ref_name: str
    status: str = "queued"  # queued | running | done | failed | cancelled
    log_chunks: list[str] = dataclass_field(default_factory=list)
    result_json: Optional[Dict[str, Any]] = None
    returncode: Optional[int] = None
    error: Optional[str] = None
    worktree_path: Optional[Path] = None
    process: Optional["subprocess.Popen[str]"] = None
    cancel_requested: bool = False
    created_at: float = dataclass_field(default_factory=time.time)
    lock: threading.Lock = dataclass_field(default_factory=threading.Lock)

    def append_log(self, text: str) -> None:
        with self.lock:
            self.log_chunks.append(text)

    def log_slice(self, offset: int) -> tuple[str, int]:
        with self.lock:
            full = "".join(self.log_chunks)
        return full[offset:], len(full)


class JobManager:
    """Single FIFO worker thread executing one harness job at a time on this
    node (the runner's own machine-wide `suite_lock` also serialises any
    OTHER pytest/harness invocation on the box against these jobs)."""

    def __init__(self, root_path: Path, state_dir: Optional[Path] = None) -> None:
        self.root_path = root_path
        self.state_dir = state_dir or default_worker_state_dir()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "logs").mkdir(parents=True, exist_ok=True)
        (self.state_dir / "worktrees").mkdir(parents=True, exist_ok=True)
        self.jobs: Dict[str, HarnessJob] = {}
        self._jobs_lock = threading.Lock()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="darkfac-worker-jobs"
        )
        self._worker_thread.start()

    # --- introspection for /health -----------------------------------------

    def is_busy(self) -> bool:
        with self._jobs_lock:
            return any(job.status == "running" for job in self.jobs.values())

    def queue_length(self) -> int:
        with self._jobs_lock:
            return sum(1 for job in self.jobs.values() if job.status in ("queued", "running"))

    # --- submit / status / cancel -------------------------------------------

    def submit(self, bundle_bytes: bytes, metadata: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
        candidate_sha = str(metadata.get("candidate_sha", "")).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{7,64}", candidate_sha or ""):
            return 400, {"error": "invalid or missing candidate_sha"}

        bundle_path = self.state_dir / f"bundle-{uuid.uuid4().hex}.bundle"
        bundle_path.write_bytes(bundle_bytes)
        try:
            # `git bundle verify` fails BOTH for a genuinely corrupt bundle
            # AND for a well-formed *thin* bundle whose prerequisite commits
            # this repo doesn't have yet -- the two are indistinguishable
            # from the exit code alone. Don't hard-reject here: fall through
            # to the actual fetch attempt below, which fails the same way
            # for both cases and is handled uniformly (retry after `git
            # fetch origin`, else 409 so the client resends a full bundle).
            verify = subprocess.run(
                ["git", "bundle", "verify", str(bundle_path)],
                cwd=str(self.root_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if verify.returncode != 0:
                logger.info("git bundle verify (non-fatal, may just be thin): %s", verify.stderr.strip()[:500])

            ref_name = f"refs/darkfac/validate/{candidate_sha}"
            fetch = subprocess.run(
                ["git", "fetch", str(bundle_path), f"HEAD:{ref_name}"],
                cwd=str(self.root_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if fetch.returncode != 0:
                subprocess.run(
                    ["git", "fetch", "origin"],
                    cwd=str(self.root_path),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                fetch_retry = subprocess.run(
                    ["git", "fetch", str(bundle_path), f"HEAD:{ref_name}"],
                    cwd=str(self.root_path),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                if fetch_retry.returncode != 0:
                    return 409, {
                        "missing_prerequisites": True,
                        "detail": fetch_retry.stderr.strip()[:500],
                    }
        finally:
            with contextlib.suppress(OSError):
                bundle_path.unlink()

        job = HarnessJob(
            job_id=uuid.uuid4().hex,
            candidate_sha=candidate_sha,
            tree_sha=str(metadata.get("tree_sha", "")),
            quick=bool(metadata.get("quick", True)),
            include_holdout=bool(metadata.get("include_holdout", False)),
            requesting_host=str(metadata.get("requesting_host", "unknown")),
            ref_name=ref_name,
        )
        with self._jobs_lock:
            self.jobs[job.job_id] = job
        self._queue.put(job.job_id)
        logger.info(
            "Queued harness job %s for candidate %s (from %s)",
            job.job_id, candidate_sha[:12], job.requesting_host,
        )
        return 200, {"job_id": job.job_id}

    def status(self, job_id: str, offset: int) -> Optional[Dict[str, Any]]:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        log_chunk, next_offset = job.log_slice(offset)
        return {
            "status": job.status,
            "log_chunk": log_chunk,
            "next_offset": next_offset,
            "result_json": job.result_json,
            "returncode": job.returncode,
            "error": job.error,
        }

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        job.cancel_requested = True
        if job.status == "queued":
            job.status = "cancelled"
        process = job.process
        if process is not None and process.poll() is None:
            with contextlib.suppress(Exception):
                process.terminate()
        return True

    # --- worker thread --------------------------------------------------------

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self.jobs.get(job_id)
            if job is None:
                continue
            if job.cancel_requested:
                job.status = "cancelled"
                continue
            try:
                self._run_job(job)
            except Exception as exc:  # noqa: BLE001 - the worker thread must never die
                logger.error("Unhandled error running job %s: %s", job_id, exc, exc_info=True)
                job.status = "failed"
                job.error = str(exc)

    def _run_job(self, job: HarnessJob) -> None:
        job.status = "running"
        worktree_path = self.state_dir / "worktrees" / job.job_id
        job.worktree_path = worktree_path
        log_file_path = self.state_dir / "logs" / f"{job.job_id}.log"

        try:
            add = subprocess.run(
                ["git", "worktree", "add", "--detach", str(worktree_path), job.ref_name],
                cwd=str(self.root_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if add.returncode != 0:
                job.status = "failed"
                job.error = f"git worktree add failed: {add.stderr.strip()[:1000]}"
                job.append_log(job.error)
                return

            timeout_sec = _job_timeout_sec(worktree_path, quick=job.quick, include_holdout=job.include_holdout)
            cmd = [sys.executable, "core/harness/runner.py", "--quick", "--local"]
            if job.include_holdout:
                cmd.append("--holdout")
            env = dict(os.environ)
            env["DARKFAC_HARNESS_WORKER_JOB"] = "1"

            with open(log_file_path, "w", encoding="utf-8") as log_fh:
                process = subprocess.Popen(
                    cmd,
                    cwd=str(worktree_path),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                job.process = process
                deadline = time.monotonic() + timeout_sec
                stop_watchdog = threading.Event()

                def _watchdog() -> None:
                    while not stop_watchdog.wait(JOB_WATCHDOG_POLL_SEC):
                        if job.cancel_requested or time.monotonic() > deadline:
                            with contextlib.suppress(Exception):
                                process.kill()
                            return

                watchdog = threading.Thread(target=_watchdog, daemon=True)
                watchdog.start()
                try:
                    assert process.stdout is not None
                    for line in process.stdout:
                        job.append_log(line)
                        log_fh.write(line)
                finally:
                    stop_watchdog.set()
                    process.wait()

            job.returncode = process.returncode

            if job.cancel_requested:
                job.status = "cancelled"
                return

            full_log = "".join(job.log_chunks)
            job.result_json = _extract_harness_result(full_log)
            job.status = "done" if process.returncode == 0 and job.result_json is not None else "failed"
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = str(exc)
            job.append_log(f"[WORKER ERROR] {exc}")
        finally:
            self._cleanup_worktree(job)
            self._prune_old_job_logs()

    def _cleanup_worktree(self, job: HarnessJob) -> None:
        if job.worktree_path is not None and job.worktree_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(job.worktree_path)],
                cwd=str(self.root_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        subprocess.run(
            ["git", "update-ref", "-d", job.ref_name],
            cwd=str(self.root_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def _prune_old_job_logs(self) -> None:
        logs_dir = self.state_dir / "logs"
        try:
            log_files = sorted(logs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for stale in log_files[JOB_LOG_KEEP:]:
            with contextlib.suppress(OSError):
                stale.unlink()


def _safe_is_file(path: Path) -> bool:
    """Safely check if a path is a file without raising PermissionError on Windows."""
    try:
        return path.is_file()
    except (OSError, PermissionError):
        return False


def find_codex_binary() -> Optional[str]:
    """Locate official Codex CLI executable on Windows or Linux."""
    executable = shutil.which("codex") or shutil.which("codex.cmd") or shutil.which("codex.exe")
    if executable:
        return executable
    user_profile = Path.home()

    # 1. Inspect ~/.codex/config.toml for explicit CODEX_CLI_PATH
    config_toml = user_profile / ".codex" / "config.toml"
    if _safe_is_file(config_toml):
        try:
            for line in config_toml.read_text(encoding="utf-8", errors="replace").splitlines():
                if "CODEX_CLI_PATH" in line and "=" in line:
                    parts = line.split("=", 1)
                    raw_val = parts[1].strip().strip("'\"")
                    if raw_val:
                        candidate_p = Path(raw_val)
                        if _safe_is_file(candidate_p):
                            return str(candidate_p)
        except Exception as exc:
            logger.debug("Failed reading ~/.codex/config.toml: %s", exc)

    # 2. Check AppData/Local/OpenAI/Codex/bin/*/codex.exe
    openai_bin = user_profile / "AppData" / "Local" / "OpenAI" / "Codex" / "bin"
    try:
        if openai_bin.is_dir():
            for p in openai_bin.glob("*/codex.exe"):
                if _safe_is_file(p):
                    return str(p)
    except Exception as exc:
        logger.debug("Error probing OpenAI Codex bin dir: %s", exc)

    # 3. Known standard install locations
    candidates = [
        user_profile / ".codex" / "environment" / "bin" / "codex.cmd",
        user_profile / ".codex" / ".sandbox-bin" / "codex.exe",
        user_profile / ".codex" / "plugins" / ".plugin-appserver" / "codex.exe",
    ]
    for c in candidates:
        if _safe_is_file(c):
            return str(c)
    return None


def find_grok_binary() -> Optional[str]:
    """Locate official Grok CLI executable on Windows or Linux."""
    executable = shutil.which("grok") or shutil.which("grok.exe")
    if executable:
        return executable
    user_profile = Path.home()
    candidates = [
        user_profile / ".grok" / "bin" / "grok.exe",
        user_profile / ".grok" / "bin" / "grok.EXE",
    ]
    for c in candidates:
        if _safe_is_file(c):
            return str(c)
    return None


def find_antigravity_binary() -> Optional[str]:
    """Locate Antigravity CLI executable or agentapi wrapper on Windows or Linux."""
    executable = shutil.which("agentapi") or shutil.which("agentapi.bat")
    if executable:
        return executable
    user_profile = Path.home()
    candidates = [
        user_profile / ".gemini" / "antigravity" / "bin" / "agentapi.bat",
        user_profile / "AppData" / "Roaming" / "Antigravity" / "bin" / "agy-node.cmd",
        user_profile / "AppData" / "Local" / "Programs" / "antigravity" / "resources" / "bin" / "language_server.exe",
    ]
    for c in candidates:
        if _safe_is_file(c):
            return str(c)
    return None


def find_claude_binary() -> Optional[str]:
    """Locate Claude Code CLI executable on Windows or Linux."""
    executable = shutil.which("claude") or shutil.which("claude.exe")
    if executable:
        return executable
    user_profile = Path.home()
    candidates = [
        user_profile / ".claude" / "bin" / "claude.exe",
        user_profile / "AppData" / "Roaming" / "npm" / "claude.cmd",
    ]
    for c in candidates:
        if _safe_is_file(c):
            return str(c)
    return None


def find_deepseek_binary() -> Optional[str]:
    """Locate DeepSeek CLI executable on Windows or Linux."""
    executable = shutil.which("deepseek") or shutil.which("deepseek.exe")
    if executable:
        return executable
    user_profile = Path.home()
    candidates = [
        user_profile / ".deepseek" / "bin" / "deepseek.exe",
    ]
    for c in candidates:
        if _safe_is_file(c):
            return str(c)
    return None


def get_available_harnesses() -> list[str]:
    """Return list of supported AI harnesses installed and ready on this node."""
    available: list[str] = []
    finders = [
        ("codex", find_codex_binary),
        ("grok", find_grok_binary),
        ("antigravity", find_antigravity_binary),
        ("claude", find_claude_binary),
        ("deepseek", find_deepseek_binary),
    ]
    for name, fn in finders:
        try:
            if fn():
                available.append(name)
        except Exception as exc:
            logger.warning("Error detecting %s harness on this host: %s", name, exc)
    return available



def trigger_daemon_restart(root_path: Path) -> None:
    """Spawns a new headless daemon process and terminates this instance."""
    import threading

    def _deferred():
        time.sleep(1.0)
        subp_kwargs: Dict[str, Any] = {}
        if sys.platform == "win32":
            subp_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        restart_script = root_path / "scripts" / "start_onprem_worker.ps1"
        if restart_script.exists():
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(restart_script), "-Headless"],
                cwd=str(root_path),
                **subp_kwargs,
            )
        os._exit(0)

    t = threading.Thread(target=_deferred, daemon=True)
    t.start()


def create_worker_app(
    project_root: Optional[Path] = None,
    node_id: str = DEFAULT_WORKER_NODE_ID,
) -> FastAPI:
    """Instantiate and configure the FastAPI application for the remote test worker."""
    root_path = project_root or REPO_ROOT
    engine = TestSubagentEngine(project_root=root_path)
    job_manager = JobManager(root_path)

    worker_app = FastAPI(
        title="DarkFac Remote Test Execution Worker",
        version="1.0.0",
        description="Headless distributed test runner for Dark Factory infrastructure nodes.",
    )
    worker_app.state.job_manager = job_manager

    @worker_app.get("/health", response_model=WorkerHealthStatus)
    def get_health() -> WorkerHealthStatus:
        """Lightweight health check endpoint used by TestSubagentEngine (and
        core.harness.remote_dispatch) for fast probing. Always open, even
        when DARKFAC_WORKER_TOKEN is set -- a client must be able to tell a
        worker is alive before it has anything to authenticate."""
        # Detect Docker Desktop availability on Windows / Linux
        docker_available = False
        try:
            import shutil
            docker_available = shutil.which("docker") is not None
        except Exception:
            docker_available = False

        return WorkerHealthStatus(
            status="busy" if job_manager.is_busy() else "ok",
            node_id=node_id,
            version="1.0.0",
            docker_ready=docker_available,
            project_root=str(root_path),
            active_runs=1 if job_manager.is_busy() else 0,
            available_harnesses=get_available_harnesses(),
            platform_family=harness_cache.platform_family(),
            python_version=harness_cache.python_version_tag(),
            hostname=socket.gethostname(),
            busy=job_manager.is_busy(),
            queue_length=job_manager.queue_length(),
            known_shas=known_validated_shas(root_path),
            harness_version="1",
        )

    @worker_app.post("/harness/jobs")
    async def submit_harness_job(request: Request) -> JSONResponse:
        """Async job submission: client sends a `git bundle` as the raw
        binary body plus base64 JSON job metadata in the `X-Job-Meta`
        header. Returns `{job_id}` on 200, or 409
        `{missing_prerequisites: true}` when the bundle is too thin for
        this worker's repo state (client retries with a full bundle)."""
        _check_auth(request)
        meta_header = request.headers.get("x-job-meta", "")
        try:
            metadata = json.loads(base64.b64decode(meta_header).decode("utf-8")) if meta_header else {}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid X-Job-Meta header: {exc}") from exc

        bundle_bytes = await request.body()
        if not bundle_bytes:
            raise HTTPException(status_code=400, detail="empty bundle body")

        status_code, body = job_manager.submit(bundle_bytes, metadata)
        return JSONResponse(status_code=status_code, content=body)

    @worker_app.get("/harness/jobs/{job_id}")
    def get_harness_job(job_id: str, request: Request, offset: int = 0) -> JSONResponse:
        """Poll a job's status; returns a log chunk starting at `offset`
        plus `next_offset` for the following poll (streams without holding
        one HTTP request open for the whole run)."""
        _check_auth(request)
        payload = job_manager.status(job_id, offset)
        if payload is None:
            raise HTTPException(status_code=404, detail="job not found")
        return JSONResponse(status_code=200, content=payload)

    @worker_app.delete("/harness/jobs/{job_id}")
    def cancel_harness_job(job_id: str, request: Request) -> JSONResponse:
        """Cancel a queued or running job (client-side Ctrl+C/timeout)."""
        _check_auth(request)
        if not job_manager.cancel(job_id):
            raise HTTPException(status_code=404, detail="job not found")
        return JSONResponse(status_code=200, content={"status": "cancelled"})

    @worker_app.post("/execute", response_model=DistilledTestReport)
    def execute_test(instruction: TestExecutionInstruction, request: Request) -> DistilledTestReport:
        """Receive a test instruction, execute pytest headless, and return distilled report."""
        _check_auth(request)
        try:
            # Force local execution within this node
            local_instruction = instruction.model_copy(update={"worker_mode": "local"})
            report = engine.execute(local_instruction)
            # Annotate with worker metadata
            report.worker_id = node_id
            report.execution_mode = "remote_onprem_offload"
            return report
        except Exception as exc:
            logger.error("Error executing test remotely: %s", exc, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Remote worker test execution failed: {exc}",
            ) from exc

    @worker_app.post("/system/exec", response_model=CommandExecutionResponse)
    def execute_command(req: CommandExecutionRequest, request: Request) -> CommandExecutionResponse:
        """Execute a shell command headless on the worker node."""
        _check_auth(request)
        target_cwd = Path(req.cwd).resolve() if req.cwd else root_path
        start_t = time.perf_counter()
        logger.info("Executing remote command on node %s: %s (cwd: %s)", node_id, req.command, target_cwd)

        subp_kwargs: Dict[str, Any] = {}
        if sys.platform == "win32":
            subp_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            res = subprocess.run(
                req.command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=str(target_cwd),
                timeout=req.timeout_seconds,
                encoding="utf-8",
                errors="replace",
                **subp_kwargs,
            )
            dur = round(time.perf_counter() - start_t, 3)
            return CommandExecutionResponse(
                exit_code=res.returncode,
                stdout=res.stdout or "",
                stderr=res.stderr or "",
                duration_seconds=dur,
                success=res.returncode == 0,
            )
        except subprocess.TimeoutExpired as exc:
            dur = round(time.perf_counter() - start_t, 3)
            return CommandExecutionResponse(
                exit_code=124,
                stdout=exc.stdout or "" if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "") + "\n[COMMAND TIMEOUT]",
                duration_seconds=dur,
                success=False,
            )
        except Exception as exc:
            dur = round(time.perf_counter() - start_t, 3)
            return CommandExecutionResponse(
                exit_code=1,
                stdout="",
                stderr=f"[EXECUTION ERROR]: {exc}",
                duration_seconds=dur,
                success=False,
            )

    @worker_app.post("/system/update", response_model=SystemUpdateResponse)
    def update_system(request: Request, req: Optional[SystemUpdateRequest] = None) -> SystemUpdateResponse:
        """Perform git fetch and pull to update worker codebase."""
        _check_auth(request)
        subp_kwargs: Dict[str, Any] = {}
        if sys.platform == "win32":
            subp_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            target_branch = req.branch if req and req.branch else ""
            if target_branch:
                subprocess.run(
                    f"git checkout {target_branch}",
                    shell=True,
                    capture_output=True,
                    text=True,
                    cwd=str(root_path),
                    **subp_kwargs,
                )
            pull_res = subprocess.run(
                "git pull",
                shell=True,
                capture_output=True,
                text=True,
                cwd=str(root_path),
                **subp_kwargs,
            )
            rev_res = subprocess.run(
                "git rev-parse HEAD",
                shell=True,
                capture_output=True,
                text=True,
                cwd=str(root_path),
                **subp_kwargs,
            )
            commit = rev_res.stdout.strip() if rev_res.returncode == 0 else "unknown"
            output = (pull_res.stdout or "") + ("\n" + pull_res.stderr if pull_res.stderr else "")
            return SystemUpdateResponse(
                success=pull_res.returncode == 0,
                output=output.strip(),
                current_commit=commit,
            )
        except Exception as exc:
            return SystemUpdateResponse(
                success=False,
                output=f"Update failed: {exc}",
                current_commit="",
            )

    @worker_app.post("/system/restart")
    def restart_daemon(request: Request) -> Dict[str, str]:
        """Spawns a new headless daemon process and terminates this instance."""
        _check_auth(request)
        trigger_daemon_restart(root_path)
        return {"status": "restarting", "node_id": node_id, "message": "Worker is restarting headless in background."}

    def _run_codex_headless(req_data: HarnessExecutionRequest) -> HarnessExecutionResponse:
        import uuid

        executable = find_codex_binary()
        if not executable:
            return HarnessExecutionResponse(
                success=False,
                harness="codex",
                error="Codex executable not found on host. Check PATH or installation in ~/.codex/",
                duration_seconds=0.0,
                node_id=node_id,
            )

        full_prompt = req_data.prompt
        if req_data.system_prompt:
            full_prompt = f"System Instructions:\n{req_data.system_prompt}\n\nUser Prompt:\n{req_data.prompt}"

        tmp_dir = root_path / ".factory" / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_out = tmp_dir / f"codex_out_{uuid.uuid4().hex[:8]}.txt"

        subp_kwargs: Dict[str, Any] = {}
        if sys.platform == "win32":
            subp_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        start_t = time.perf_counter()
        target_cwd = Path(req_data.cwd).resolve() if req_data.cwd else root_path
        model_flag = f'-m "{req_data.model}"' if req_data.model else ""
        cmd = f'"{executable}" exec --sandbox read-only --ephemeral --skip-git-repo-check {model_flag} -o "{tmp_out}" -'

        try:
            logger.info("Executing Codex non-interactively on node %s (prompt length: %d)", node_id, len(full_prompt))
            proc = subprocess.run(
                cmd,
                shell=True,
                input=full_prompt,
                capture_output=True,
                text=True,
                cwd=str(target_cwd),
                timeout=req_data.timeout_seconds,
                encoding="utf-8",
                errors="replace",
                **subp_kwargs,
            )
            duration = round(time.perf_counter() - start_t, 3)

            output_text = ""
            if tmp_out.is_file():
                try:
                    output_text = tmp_out.read_text(encoding="utf-8").strip()
                except Exception as exc:
                    logger.debug("Failed to read tmp_out: %s", exc)

            if not output_text and proc.stdout.strip():
                output_text = proc.stdout.strip()

            detected_model = req_data.model or "gpt-5.6-sol"
            tokens_used = 0
            if proc.stderr:
                for line in proc.stderr.splitlines():
                    if line.startswith("model:"):
                        detected_model = line.split(":", 1)[1].strip()

            if proc.returncode != 0 and not output_text:
                return HarnessExecutionResponse(
                    success=False,
                    harness="codex",
                    text="",
                    model=detected_model,
                    tokens_used=tokens_used,
                    duration_seconds=duration,
                    error=f"Codex exited with code {proc.returncode}: {proc.stderr[:500]}",
                    node_id=node_id,
                )

            return HarnessExecutionResponse(
                success=True,
                harness="codex",
                text=output_text,
                model=detected_model,
                tokens_used=tokens_used,
                duration_seconds=duration,
                node_id=node_id,
            )
        except subprocess.TimeoutExpired:
            dur = round(time.perf_counter() - start_t, 3)
            return HarnessExecutionResponse(
                success=False,
                harness="codex",
                error=f"Codex execution timed out after {req_data.timeout_seconds}s",
                duration_seconds=dur,
                node_id=node_id,
            )
        except Exception as exc:
            dur = round(time.perf_counter() - start_t, 3)
            return HarnessExecutionResponse(
                success=False,
                harness="codex",
                error=f"Codex execution error: {exc}",
                duration_seconds=dur,
                node_id=node_id,
            )
        finally:
            if tmp_out.exists():
                try:
                    tmp_out.unlink()
                except Exception:
                    pass

    def _run_grok_headless(req_data: HarnessExecutionRequest) -> HarnessExecutionResponse:
        executable = find_grok_binary()
        if not executable:
            return HarnessExecutionResponse(
                success=False,
                harness="grok",
                error="Grok executable not found on host. Check PATH or installation in ~/.grok/bin/grok.exe",
                duration_seconds=0.0,
                node_id=node_id,
            )

        full_prompt = req_data.prompt
        if req_data.system_prompt:
            full_prompt = f"{req_data.system_prompt}\n\n{req_data.prompt}"

        subp_kwargs: Dict[str, Any] = {}
        if sys.platform == "win32":
            subp_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        start_t = time.perf_counter()
        target_cwd = Path(req_data.cwd).resolve() if req_data.cwd else root_path
        cmd_args = [executable, "-p", full_prompt, "--output-format", "plain"]
        if req_data.model:
            cmd_args.extend(["-m", req_data.model])

        try:
            logger.info("Executing Grok non-interactively on node %s (prompt length: %d)", node_id, len(full_prompt))
            proc = subprocess.run(
                cmd_args,
                capture_output=True,
                text=True,
                cwd=str(target_cwd),
                timeout=req_data.timeout_seconds,
                encoding="utf-8",
                errors="replace",
                **subp_kwargs,
            )
            duration = round(time.perf_counter() - start_t, 3)
            output_text = proc.stdout.strip()
            detected_model = req_data.model or "grok-4.6"

            if proc.returncode != 0 and not output_text:
                err_msg = proc.stderr.strip() or f"Grok exited with code {proc.returncode}"
                return HarnessExecutionResponse(
                    success=False,
                    harness="grok",
                    text="",
                    model=detected_model,
                    duration_seconds=duration,
                    error=err_msg[:500],
                    node_id=node_id,
                )

            return HarnessExecutionResponse(
                success=True,
                harness="grok",
                text=output_text,
                model=detected_model,
                duration_seconds=duration,
                node_id=node_id,
            )
        except subprocess.TimeoutExpired:
            dur = round(time.perf_counter() - start_t, 3)
            return HarnessExecutionResponse(
                success=False,
                harness="grok",
                error=f"Grok execution timed out after {req_data.timeout_seconds}s",
                duration_seconds=dur,
                node_id=node_id,
            )
        except Exception as exc:
            dur = round(time.perf_counter() - start_t, 3)
            return HarnessExecutionResponse(
                success=False,
                harness="grok",
                error=f"Grok execution error: {exc}",
                duration_seconds=dur,
                node_id=node_id,
            )

    def _run_antigravity_headless(req_data: HarnessExecutionRequest) -> HarnessExecutionResponse:
        executable = find_antigravity_binary()
        if not executable:
            return HarnessExecutionResponse(
                success=False,
                harness="antigravity",
                error="Antigravity executable or agentapi not found on host. Check ~/.gemini/antigravity/bin/",
                duration_seconds=0.0,
                node_id=node_id,
            )

        full_prompt = req_data.prompt
        if req_data.system_prompt:
            full_prompt = f"{req_data.system_prompt}\n\n{req_data.prompt}"

        subp_kwargs: Dict[str, Any] = {}
        if sys.platform == "win32":
            subp_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        start_t = time.perf_counter()
        target_cwd = Path(req_data.cwd).resolve() if req_data.cwd else root_path
        tier = req_data.model or "flash"
        cmd_args = [executable, "new-conversation", f"--model={tier}", full_prompt]

        try:
            logger.info("Executing Antigravity on node %s (model: %s, prompt length: %d)", node_id, tier, len(full_prompt))
            proc = subprocess.run(
                cmd_args,
                capture_output=True,
                text=True,
                cwd=str(target_cwd),
                timeout=req_data.timeout_seconds,
                encoding="utf-8",
                errors="replace",
                **subp_kwargs,
            )
            duration = round(time.perf_counter() - start_t, 3)
            stdout = proc.stdout.strip()

            if proc.returncode != 0 and not stdout:
                err_msg = proc.stderr.strip() or f"Antigravity exited with code {proc.returncode}"
                return HarnessExecutionResponse(
                    success=False,
                    harness="antigravity",
                    text=stdout,
                    model=tier,
                    duration_seconds=duration,
                    error=err_msg[:500],
                    node_id=node_id,
                )

            return HarnessExecutionResponse(
                success=True,
                harness="antigravity",
                text=stdout,
                model=tier,
                duration_seconds=duration,
                node_id=node_id,
            )
        except subprocess.TimeoutExpired:
            dur = round(time.perf_counter() - start_t, 3)
            return HarnessExecutionResponse(
                success=False,
                harness="antigravity",
                error=f"Antigravity execution timed out after {req_data.timeout_seconds}s",
                duration_seconds=dur,
                node_id=node_id,
            )
        except Exception as exc:
            dur = round(time.perf_counter() - start_t, 3)
            return HarnessExecutionResponse(
                success=False,
                harness="antigravity",
                error=f"Antigravity execution error: {exc}",
                duration_seconds=dur,
                node_id=node_id,
            )

    def _run_claude_headless(req_data: HarnessExecutionRequest) -> HarnessExecutionResponse:
        executable = find_claude_binary()
        if not executable:
            return HarnessExecutionResponse(
                success=False,
                harness="claude",
                error="Claude Code executable not installed on this host. Pending account/CLI activation.",
                duration_seconds=0.0,
                node_id=node_id,
            )
        full_prompt = req_data.prompt if not req_data.system_prompt else f"{req_data.system_prompt}\n\n{req_data.prompt}"
        subp_kwargs: Dict[str, Any] = {}
        if sys.platform == "win32":
            subp_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        start_t = time.perf_counter()
        target_cwd = Path(req_data.cwd).resolve() if req_data.cwd else root_path
        cmd_args = [executable, "-p", full_prompt]
        try:
            logger.info("Executing Claude Code on node %s (prompt length: %d)", node_id, len(full_prompt))
            proc = subprocess.run(
                cmd_args,
                capture_output=True,
                text=True,
                cwd=str(target_cwd),
                timeout=req_data.timeout_seconds,
                encoding="utf-8",
                errors="replace",
                **subp_kwargs,
            )
            duration = round(time.perf_counter() - start_t, 3)
            output_text = proc.stdout.strip()
            if proc.returncode != 0 and not output_text:
                return HarnessExecutionResponse(
                    success=False,
                    harness="claude",
                    text="",
                    model=req_data.model or "claude-sonnet",
                    duration_seconds=duration,
                    error=proc.stderr.strip()[:500] or f"Claude exited with code {proc.returncode}",
                    node_id=node_id,
                )
            return HarnessExecutionResponse(
                success=True,
                harness="claude",
                text=output_text,
                model=req_data.model or "claude-sonnet",
                duration_seconds=duration,
                node_id=node_id,
            )
        except Exception as exc:
            return HarnessExecutionResponse(
                success=False,
                harness="claude",
                error=f"Claude execution error: {exc}",
                duration_seconds=round(time.perf_counter() - start_t, 3),
                node_id=node_id,
            )

    def _run_deepseek_headless(req_data: HarnessExecutionRequest) -> HarnessExecutionResponse:
        executable = find_deepseek_binary()
        if not executable:
            return HarnessExecutionResponse(
                success=False,
                harness="deepseek",
                error="DeepSeek CLI executable not installed on this host. Pending account/CLI activation.",
                duration_seconds=0.0,
                node_id=node_id,
            )
        full_prompt = req_data.prompt if not req_data.system_prompt else f"{req_data.system_prompt}\n\n{req_data.prompt}"
        subp_kwargs: Dict[str, Any] = {}
        if sys.platform == "win32":
            subp_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        start_t = time.perf_counter()
        target_cwd = Path(req_data.cwd).resolve() if req_data.cwd else root_path
        cmd_args = [executable, "-p", full_prompt]
        try:
            logger.info("Executing DeepSeek on node %s (prompt length: %d)", node_id, len(full_prompt))
            proc = subprocess.run(
                cmd_args,
                capture_output=True,
                text=True,
                cwd=str(target_cwd),
                timeout=req_data.timeout_seconds,
                encoding="utf-8",
                errors="replace",
                **subp_kwargs,
            )
            duration = round(time.perf_counter() - start_t, 3)
            output_text = proc.stdout.strip()
            if proc.returncode != 0 and not output_text:
                return HarnessExecutionResponse(
                    success=False,
                    harness="deepseek",
                    text="",
                    model=req_data.model or "deepseek-coder",
                    duration_seconds=duration,
                    error=proc.stderr.strip()[:500] or f"DeepSeek exited with code {proc.returncode}",
                    node_id=node_id,
                )
            return HarnessExecutionResponse(
                success=True,
                harness="deepseek",
                text=output_text,
                model=req_data.model or "deepseek-coder",
                duration_seconds=duration,
                node_id=node_id,
            )
        except Exception as exc:
            return HarnessExecutionResponse(
                success=False,
                harness="deepseek",
                error=f"DeepSeek execution error: {exc}",
                duration_seconds=round(time.perf_counter() - start_t, 3),
                node_id=node_id,
            )

    def _dispatch_harness(req_data: HarnessExecutionRequest) -> HarnessExecutionResponse:
        h = req_data.harness.lower().strip()
        if h == "codex":
            return _run_codex_headless(req_data)
        elif h == "grok":
            return _run_grok_headless(req_data)
        elif h == "antigravity":
            return _run_antigravity_headless(req_data)
        elif h == "claude":
            return _run_claude_headless(req_data)
        elif h == "deepseek":
            return _run_deepseek_headless(req_data)
        else:
            return HarnessExecutionResponse(
                success=False,
                harness=h,
                error=f"Unsupported harness identifier: '{req_data.harness}'. Supported: codex, grok, antigravity, claude, deepseek",
                node_id=node_id,
            )

    @worker_app.post("/harness/execute", response_model=HarnessExecutionResponse)
    def execute_generic_harness(req: HarnessExecutionRequest, request: Request) -> HarnessExecutionResponse:
        """Unified endpoint to execute any supported headless AI harness."""
        _check_auth(request)
        return _dispatch_harness(req)

    @worker_app.post("/harness/codex", response_model=CodexExecutionResponse)
    def execute_codex(req: CodexExecutionRequest, request: Request) -> CodexExecutionResponse:
        """Backward-compatible endpoint specifically executing Codex."""
        _check_auth(request)
        gen_req = HarnessExecutionRequest(
            harness="codex",
            prompt=req.prompt,
            system_prompt=req.system_prompt,
            timeout_seconds=req.timeout_seconds,
            model=req.model,
            cwd=req.cwd,
        )
        res = _run_codex_headless(gen_req)
        return CodexExecutionResponse(
            success=res.success,
            text=res.text,
            model=res.model or "gpt-5.6-sol",
            tokens_used=res.tokens_used,
            duration_seconds=res.duration_seconds,
            error=res.error,
        )

    @worker_app.post("/harness/grok", response_model=HarnessExecutionResponse)
    def execute_grok(req: HarnessExecutionRequest, request: Request) -> HarnessExecutionResponse:
        """Dedicated endpoint specifically executing Grok Build CLI."""
        _check_auth(request)
        req.harness = "grok"
        return _run_grok_headless(req)

    @worker_app.post("/harness/antigravity", response_model=HarnessExecutionResponse)
    def execute_antigravity(req: HarnessExecutionRequest, request: Request) -> HarnessExecutionResponse:
        """Dedicated endpoint specifically executing Antigravity CLI / agentapi."""
        _check_auth(request)
        req.harness = "antigravity"
        return _run_antigravity_headless(req)

    return worker_app


app = create_worker_app()


def main() -> int:
    """CLI entrypoint to launch the worker daemon directly via PowerShell or terminal."""
    parser = argparse.ArgumentParser(description="DarkFac On-Premises Remote Test Worker Daemon")
    parser.add_argument("--host", default=DEFAULT_WORKER_HOST, help=f"Host address to bind (default: {DEFAULT_WORKER_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_WORKER_PORT, help=f"Port to listen on (default: {DEFAULT_WORKER_PORT})")
    parser.add_argument("--node-id", default=DEFAULT_WORKER_NODE_ID, help="Node ID name")
    parser.add_argument("--project-root", type=Path, default=REPO_ROOT, help="Project root directory")
    args = parser.parse_args()

    import uvicorn

    worker_instance = create_worker_app(project_root=args.project_root, node_id=args.node_id)
    print("=" * 70)
    print(f" [DarkFac Test Worker Daemon] Node: {args.node_id}")
    print(f" --> Listening on: http://{args.host}:{args.port}")
    print(f" --> Health check: http://{args.host}:{args.port}/health")
    print(f" --> Execution endpoint: http://{args.host}:{args.port}/execute")
    print("=" * 70)

    uvicorn.run(worker_instance, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
