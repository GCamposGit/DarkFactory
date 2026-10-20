#!/usr/bin/env python3
"""Supervisor do worker de testes na VM grokbox (USR-315). Stdlib pura, sem systemd.

Uso na VM (ver docs/runbooks/grokbox_test_worker.md):
    nohup python3 scripts/grokbox_supervisor.py >> /workspace/darkfac/run/supervisor.log 2>&1 &

Mantem ``core/harness/remote_worker.py`` de pe, relanca se morrer e o recarrega quando o HEAD de
``project_root`` muda e o worker esta ocioso (``/health`` busy=false). Nunca reinicia com job rodando.
A decisao e a funcao pura ``decide``; o resto e I/O fino.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("grokbox_supervisor")

LAUNCH = "launch"
RELOAD = "reload"
WAIT = "wait"
NOOP = "noop"
STOP = "stop"


@dataclass(frozen=True)
class State:
    """Fotografia observada num ciclo. ``None`` em health_* = /health inalcancavel."""

    head_sha: str  # git rev-parse HEAD de project_root ("" se ilegivel)
    process_alive: bool
    launch_sha: str  # HEAD no momento em que o filho foi lancado
    health_sha: str | None = None
    health_busy: bool | None = None
    stop_requested: bool = False


def decide(state: State) -> str:
    """Retorna a acao: STOP, LAUNCH, RELOAD, WAIT ou NOOP."""
    if state.stop_requested:
        return STOP
    if not state.process_alive:
        return LAUNCH
    head = (state.head_sha or "").strip().lower()
    if not head:
        return NOOP
    # /health computes git_sha from HEAD at request time, not from the code the worker loaded, so after a
    # pull it already reports the new sha with the old process: only the sha WE launched is trustworthy.
    running = (state.launch_sha or "").strip().lower()
    if running == head:
        return NOOP
    # HEAD mudou: so recarrega com certeza de que esta ocioso.
    if state.health_busy is False:
        return RELOAD
    return WAIT  # busy=true ou /health inalcancavel: nunca matar sem saber


def read_head(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip().lower() if out.returncode == 0 else ""


def read_health(url: str, timeout: float) -> tuple[str | None, bool | None]:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None, None
    sha = str(data.get("git_sha") or "").strip().lower() or None
    busy = data.get("busy")
    return sha, (busy if isinstance(busy, bool) else None)


def build_command(args: argparse.Namespace) -> list[str]:
    return [
        args.python, "core/harness/remote_worker.py", "--host", args.host, "--port", str(args.port),
        "--node-id", args.node_id, "--project-root", str(args.project_root),
    ]


def launch(args: argparse.Namespace) -> subprocess.Popen:
    env = dict(os.environ, PYTHONUTF8="1")
    logger.info("launching worker: %s", " ".join(build_command(args)))
    return subprocess.Popen(build_command(args), cwd=args.project_root, env=env)


def terminate(proc: subprocess.Popen, grace: float = 20.0) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env = os.environ.get
    root = env("GROKBOX_PROJECT_ROOT", "/workspace/darkfac/DarkFactory")
    p = argparse.ArgumentParser(description="Supervisor do worker de testes do grokbox")
    p.add_argument("--project-root", type=Path, default=Path(root))
    p.add_argument("--python", default=env("GROKBOX_PYTHON", str(Path(root) / ".venv" / "bin" / "python")))
    p.add_argument("--host", default=env("GROKBOX_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(env("GROKBOX_PORT", "8080")))
    p.add_argument("--node-id", default=env("GROKBOX_NODE_ID", "grokbox"))
    p.add_argument("--interval", type=float, default=float(env("GROKBOX_POLL_SEC", "10")))
    p.add_argument("--health-timeout", type=float, default=5.0)
    p.add_argument("--stop-file", type=Path, default=Path(env("GROKBOX_STOP_FILE", "/workspace/darkfac/run/supervisor.stop")))
    p.add_argument("--pid-file", type=Path, default=Path(env("GROKBOX_PID_FILE", "/workspace/darkfac/run/supervisor.pid")))
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    args.pid_file.parent.mkdir(parents=True, exist_ok=True)
    args.pid_file.write_text(str(os.getpid()), encoding="utf-8")
    url = f"http://127.0.0.1:{args.port}"
    proc: subprocess.Popen | None = None
    launch_sha = ""
    try:
        while True:
            head = read_head(args.project_root)
            alive = proc is not None and proc.poll() is None
            sha, busy = read_health(url, args.health_timeout)
            if not alive and sha is not None:
                # Worker de fora (ex.: supervisor antigo) ocupa a porta: nao lancar outro por cima.
                logger.warning("worker nao gerenciado responde em %s (sha %s); aguardando", url, sha[:12])
                time.sleep(args.interval)
                continue
            action = decide(State(head, alive, launch_sha, sha, busy, args.stop_file.exists()))
            if action == STOP:
                logger.info("stop file present (%s); supervisor exiting, worker left running", args.stop_file)
                return 0
            if action == RELOAD and proc is not None:
                _, busy_now = read_health(url, args.health_timeout)  # rechecagem antes de matar
                if busy_now is False:
                    logger.info("HEAD %s != worker %s, idle: reloading", head[:12], (sha or launch_sha)[:12])
                    terminate(proc)
                    action = LAUNCH
            if action == LAUNCH:
                launch_sha = head
                proc = launch(args)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            args.pid_file.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
