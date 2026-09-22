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
from typing import Any, Dict, Literal, Optional

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
    available_harnesses: list[str] = Field(default_factory=list, description="Supported AI harnesses detected on this host")


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
    mode: Literal["read", "write"] = Field(
        default="read",
        description="Execution mode: 'read' preserves today's flags (unchanged), "
        "'write' lets the agent modify files (workspace-write / bypassPermissions).",
    )
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
            available_harnesses=get_available_harnesses(),
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
        mode = getattr(req_data, "mode", "read") or "read"

        try:
            if mode == "write":
                # Reuse the HF-27-03 argv builder so the line and this ad hoc
                # endpoint agree on the write-mode flags (workspace-write, --json).
                from core.line.agent_cli import AgentRequest, build_codex_argv

                agent_req = AgentRequest(
                    prompt=full_prompt,
                    cwd=target_cwd,
                    mode="write",
                    harness="codex",
                    model=req_data.model,
                    timeout_s=req_data.timeout_seconds,
                )
                cmd_argv = build_codex_argv(executable, agent_req, tmp_out)
                logger.info("Executing Codex (write) non-interactively on node %s (prompt length: %d)", node_id, len(full_prompt))
                proc = subprocess.run(
                    cmd_argv,
                    input=full_prompt,
                    capture_output=True,
                    text=True,
                    cwd=str(target_cwd),
                    timeout=req_data.timeout_seconds,
                    encoding="utf-8",
                    errors="replace",
                    **subp_kwargs,
                )
            else:
                # Unchanged default behaviour: read-only, ephemeral.
                model_flag = f'-m "{req_data.model}"' if req_data.model else ""
                cmd = f'"{executable}" exec --sandbox read-only --ephemeral --skip-git-repo-check {model_flag} -o "{tmp_out}" -'
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
        mode = getattr(req_data, "mode", "read") or "read"
        try:
            if mode == "write":
                # Reuse the HF-27-03 argv builder so the line and this ad hoc
                # endpoint agree on the write-mode flags (--permission-mode bypassPermissions).
                from core.line.agent_cli import AgentRequest, build_claude_argv

                agent_req = AgentRequest(
                    prompt=full_prompt,
                    cwd=target_cwd,
                    mode="write",
                    harness="claude",
                    model=req_data.model,
                    timeout_s=req_data.timeout_seconds,
                )
                cmd_args = build_claude_argv(executable, agent_req)
                logger.info("Executing Claude Code (write) on node %s (prompt length: %d)", node_id, len(full_prompt))
                proc = subprocess.run(
                    cmd_args,
                    input=full_prompt,
                    capture_output=True,
                    text=True,
                    cwd=str(target_cwd),
                    timeout=req_data.timeout_seconds,
                    encoding="utf-8",
                    errors="replace",
                    **subp_kwargs,
                )
            else:
                # Unchanged default behaviour.
                cmd_args = [executable, "-p", full_prompt]
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
    def execute_generic_harness(req: HarnessExecutionRequest) -> HarnessExecutionResponse:
        """Unified endpoint to execute any supported headless AI harness."""
        return _dispatch_harness(req)

    @worker_app.post("/harness/codex", response_model=CodexExecutionResponse)
    def execute_codex(req: CodexExecutionRequest) -> CodexExecutionResponse:
        """Backward-compatible endpoint specifically executing Codex."""
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
    def execute_grok(req: HarnessExecutionRequest) -> HarnessExecutionResponse:
        """Dedicated endpoint specifically executing Grok Build CLI."""
        req.harness = "grok"
        return _run_grok_headless(req)

    @worker_app.post("/harness/antigravity", response_model=HarnessExecutionResponse)
    def execute_antigravity(req: HarnessExecutionRequest) -> HarnessExecutionResponse:
        """Dedicated endpoint specifically executing Antigravity CLI / agentapi."""
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
