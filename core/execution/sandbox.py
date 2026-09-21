"""Execution sandbox providing path containment, network boundaries and process lifecycle safety.

Conforms to Section 2 of DEVELOPMENT_PLAN_2026-09-05:
- Worktrees separate Git changes, but a sandbox is required for file, network and process containment.
- Enforces allowed paths, prevents escapes, kills process trees on timeout.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROTECTED_FACTORY_FILES = {
    "mission.md",
    "factory_rules.md",
    "factory_governance.md",
}


class SandboxSecurityError(PermissionError):
    """Raised when an operation attempts to breach sandbox boundaries."""


class ProcessTimeoutError(TimeoutError):
    """Raised when an executed process exceeds its allotted deadline."""


@dataclass(frozen=True)
class ProcessExecutionResult:
    """Immutable outcome of a sandboxed process invocation."""

    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


class PathContainment:
    """Enforces fine-grained filesystem boundaries for an execution run."""

    def __init__(
        self,
        root_dir: Path | str,
        allowed_paths: list[str] | None = None,
        *,
        protect_governance: bool = True,
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.allowed_paths = [p.strip() for p in (allowed_paths or []) if p.strip()]
        self.protect_governance = protect_governance

    def is_path_allowed(self, target_path: Path | str, *, for_write: bool = True) -> bool:
        try:
            resolved_target = (self.root_dir / target_path).resolve()
        except Exception:
            return False

        # 1. Must be inside root_dir (no directory traversal escape)
        try:
            rel = resolved_target.relative_to(self.root_dir).as_posix()
        except ValueError:
            return False

        # 2. Check protected factory governance files
        if for_write and self.protect_governance:
            base_name = resolved_target.name.lower()
            if base_name in PROTECTED_FACTORY_FILES:
                return False

        # 3. If explicit allowed_paths are specified, target must match one prefix/path
        if self.allowed_paths:
            allowed = False
            for rule in self.allowed_paths:
                rule_normalized = rule.replace("\\", "/").rstrip("/")
                if rel == rule_normalized or rel.startswith(f"{rule_normalized}/"):
                    allowed = True
                    break
            return allowed

        return True

    def assert_path_allowed(self, target_path: Path | str, *, for_write: bool = True) -> Path:
        resolved = (self.root_dir / target_path).resolve()
        if not self.is_path_allowed(target_path, for_write=for_write):
            raise SandboxSecurityError(
                f"Path '{target_path}' (resolved: '{resolved}') violates sandbox containment rules."
            )
        return resolved


class NetworkContainment:
    """Enforces network egress restrictions during sandboxed execution."""

    def __init__(
        self,
        allow_network: bool = False,
        allow_loopback: bool = True,
        allowed_hosts: list[str] | None = None,
    ) -> None:
        self.allow_network = allow_network
        self.allow_loopback = allow_loopback
        self.allowed_hosts = set(allowed_hosts or [])

    def is_destination_allowed(self, host: str) -> bool:
        host_lower = host.strip().lower()
        if host_lower in {"localhost", "127.0.0.1", "::1"}:
            return self.allow_loopback

        if not self.allow_network:
            return False

        if host_lower in self.allowed_hosts:
            return True

        return False

    def assert_destination_allowed(self, host: str) -> None:
        if not self.is_destination_allowed(host):
            raise SandboxSecurityError(
                f"Network destination '{host}' is forbidden under sandbox egress policy."
            )


def kill_process_tree(pid: int) -> None:
    """Reliably terminates a process and all its descendants on Windows and POSIX."""
    if pid <= 0:
        return

    if sys.platform == "win32":
        try:
            # /F forces termination, /T kills the entire process tree
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception as exc:
            logger.debug(f"taskkill failed for pid {pid}: {exc}")
    else:
        try:
            target_group = os.getpgid(pid)
            if target_group == os.getpgrp():
                # Never terminate the caller's own process group.  A child that
                # was not isolated must be killed individually so a timeout
                # cannot take down pytest, the shell, or a CI runner.
                logger.warning(
                    "Refusing to kill caller process group %s for child %s",
                    target_group,
                    pid,
                )
                os.kill(pid, signal.SIGKILL)
            else:
                os.killpg(target_group, signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception as exc:
                logger.debug(f"kill failed for pid {pid}: {exc}")


class ProcessSandbox:
    """Executes external commands with process tree lifecycle management and timeouts."""

    def __init__(
        self,
        working_dir: Path | str,
        path_containment: PathContainment | None = None,
        default_timeout_seconds: float = 60.0,
    ) -> None:
        self.working_dir = Path(working_dir).resolve()
        self.path_containment = path_containment or PathContainment(self.working_dir)
        self.default_timeout = default_timeout_seconds

    def run_command(
        self,
        command: list[str] | str,
        *,
        timeout_seconds: float | None = None,
        env: dict[str, str] | None = None,
        shell: bool = False,
    ) -> ProcessExecutionResult:
        timeout = timeout_seconds if timeout_seconds is not None else self.default_timeout
        import time

        run_env = dict(os.environ)
        if env:
            run_env.update(env)
        # Enforce UTF-8 output encoding across subprocesses
        run_env["PYTHONIOENCODING"] = "utf-8"
        run_env["PYTHONUTF8"] = "1"

        start_time = time.perf_counter()
        proc = None
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(self.working_dir),
                env=run_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=shell,
                start_new_session=sys.platform != "win32",
            )
            stdout, stderr = proc.communicate(timeout=timeout)
            duration = max(0.001, time.perf_counter() - start_time)
            return ProcessExecutionResult(
                exit_code=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration,
                timed_out=False,
            )
        except subprocess.TimeoutExpired:
            duration = max(0.001, time.perf_counter() - start_time)
            if proc is not None:
                kill_process_tree(proc.pid)
                try:
                    stdout, stderr = proc.communicate(timeout=2.0)
                except Exception:
                    stdout, stderr = "", "Process killed due to timeout."
            else:
                stdout, stderr = "", "Process timed out."
            return ProcessExecutionResult(
                exit_code=-1,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration,
                timed_out=True,
            )
        except Exception as exc:
            duration = max(0.001, time.perf_counter() - start_time)
            if proc is not None:
                kill_process_tree(proc.pid)
            return ProcessExecutionResult(
                exit_code=1,
                stdout="",
                stderr=f"Execution error: {exc}",
                duration_seconds=duration,
                timed_out=False,
            )
