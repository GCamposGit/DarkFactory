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
import sys
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
