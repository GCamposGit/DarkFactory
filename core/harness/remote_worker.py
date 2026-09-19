"""
Remote Headless Test Execution Daemon for DarkFac.

Designed to run on the on-premises dedicated server (desktop-g45ipem, Tailscale 100.78.181.90)
or inside a Docker container on Drive E: (3 TB), providing headless test offloading for agents
and developer workstations without cluttering the interactive laptop.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Ensure repo root is in sys.path
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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


class WorkerHealthStatus(BaseModel):
    """Health and operational capability status of the remote test runner node."""
    status: str = Field(default="ok", description="Health status string: ok, degraded, busy")
    node_id: str = Field(default=DEFAULT_WORKER_NODE_ID, description="Target node slug identifier")
    version: str = Field(default="1.0.0", description="Worker service version")
    docker_ready: bool = Field(default=True, description="Whether Docker runtime is accessible on Drive E:")
    project_root: str = Field(..., description="Root directory where tests execute")
    active_runs: int = Field(default=0, description="Currently running test jobs")


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


def find_codex_binary() -> Optional[str]:
    """Locate official Codex CLI executable on Windows or Linux."""
    executable = shutil.which("codex")
    if executable:
        return executable
    user_profile = Path.home()
    candidates = [
        user_profile / ".codex" / "environment" / "bin" / "codex.cmd",
        user_profile / ".codex" / ".sandbox-bin" / "codex.exe",
        user_profile / ".codex" / "plugins" / ".plugin-appserver" / "codex.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


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

    worker_app = FastAPI(
        title="DarkFac Remote Test Execution Worker",
        version="1.0.0",
        description="Headless distributed test runner for Dark Factory infrastructure nodes.",
    )

    @worker_app.get("/health", response_model=WorkerHealthStatus)
    def get_health() -> WorkerHealthStatus:
        """Lightweight health check endpoint used by TestSubagentEngine for fast probing."""
        # Detect Docker Desktop availability on Windows / Linux
        docker_available = False
        try:
            import shutil
            docker_available = shutil.which("docker") is not None
        except Exception:
            docker_available = False

        return WorkerHealthStatus(
            status="ok",
            node_id=node_id,
            version="1.0.0",
            docker_ready=docker_available,
            project_root=str(root_path),
            active_runs=0,
        )

    @worker_app.post("/execute", response_model=DistilledTestReport)
    def execute_test(instruction: TestExecutionInstruction) -> DistilledTestReport:
        """Receive a test instruction, execute pytest headless, and return distilled report."""
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
    def execute_command(req: CommandExecutionRequest) -> CommandExecutionResponse:
        """Execute a shell command headless on the worker node."""
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
    def update_system(req: Optional[SystemUpdateRequest] = None) -> SystemUpdateResponse:
        """Perform git fetch and pull to update worker codebase."""
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
    def restart_daemon() -> Dict[str, str]:
        """Spawns a new headless daemon process and terminates this instance."""
        trigger_daemon_restart(root_path)
        return {"status": "restarting", "node_id": node_id, "message": "Worker is restarting headless in background."}

    @worker_app.post("/harness/codex", response_model=CodexExecutionResponse)
    def execute_codex(req: CodexExecutionRequest) -> CodexExecutionResponse:
        """Execute non-interactive prompt using official local Codex CLI."""
        import tempfile
        import uuid

        executable = find_codex_binary()
        if not executable:
            return CodexExecutionResponse(
                success=False,
                error="Codex executable not found on host. Check PATH or installation in ~/.codex/",
                duration_seconds=0.0,
            )

        full_prompt = req.prompt
        if req.system_prompt:
            full_prompt = f"System Instructions:\n{req.system_prompt}\n\nUser Prompt:\n{req.prompt}"

        tmp_dir = root_path / ".factory" / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_out = tmp_dir / f"codex_out_{uuid.uuid4().hex[:8]}.txt"

        subp_kwargs: Dict[str, Any] = {}
        if sys.platform == "win32":
            subp_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        start_t = time.perf_counter()
        target_cwd = Path(req.cwd).resolve() if req.cwd else root_path
        model_flag = f'-m "{req.model}"' if req.model else ""
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
                timeout=req.timeout_seconds,
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

            detected_model = req.model or "gpt-5.6-sol"
            tokens_used = 0
            if proc.stderr:
                for line in proc.stderr.splitlines():
                    if line.startswith("model:"):
                        detected_model = line.split(":", 1)[1].strip()

            if proc.returncode != 0 and not output_text:
                return CodexExecutionResponse(
                    success=False,
                    text="",
                    model=detected_model,
                    tokens_used=tokens_used,
                    duration_seconds=duration,
                    error=f"Codex exited with code {proc.returncode}: {proc.stderr[:500]}",
                )

            return CodexExecutionResponse(
                success=True,
                text=output_text,
                model=detected_model,
                tokens_used=tokens_used,
                duration_seconds=duration,
            )
        except subprocess.TimeoutExpired:
            dur = round(time.perf_counter() - start_t, 3)
            return CodexExecutionResponse(
                success=False,
                error=f"Codex execution timed out after {req.timeout_seconds}s",
                duration_seconds=dur,
            )
        except Exception as exc:
            dur = round(time.perf_counter() - start_t, 3)
            return CodexExecutionResponse(
                success=False,
                error=f"Codex execution error: {exc}",
                duration_seconds=dur,
            )
        finally:
            if tmp_out.exists():
                try:
                    tmp_out.unlink()
                except Exception:
                    pass

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
