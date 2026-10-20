"""Thin, failure-proof client of scripts/execution.py (registry USR-208) used by hooks and the session wrapper (USR-209).

Every function swallows errors and uses short timeouts: a registry problem never blocks a session.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_ENV = "DARKFAC_EXECUTION_CLI"
CALL_TIMEOUT_S = 5.0
_TICKET_RE = re.compile(r"ticket/(usr-\d+)", re.IGNORECASE)


def cli_path() -> Path:
    override = os.environ.get(CLI_ENV, "").strip()
    return Path(override) if override else REPO_ROOT / "scripts" / "execution.py"


def _run(args: list[str], timeout: float = CALL_TIMEOUT_S) -> str:
    cli = cli_path()
    if not cli.is_file():
        return ""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, str(cli), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        return proc.stdout or ""
    except Exception:  # noqa: BLE001
        return ""


def detect_ticket(cwd: str | None = None) -> str | None:
    """Ticket id from DARKFAC_TICKET_ID or from a ticket/usr-NNN branch of ``cwd``."""
    env = os.environ.get("DARKFAC_TICKET_ID", "").strip()
    if env:
        return env.upper()
    try:
        proc = subprocess.run(  # noqa: S603,S607
            ["git", "-C", cwd or os.getcwd(), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
            stdin=subprocess.DEVNULL,
        )
        match = _TICKET_RE.search(proc.stdout or "")
        return match.group(1).upper() if match else None
    except Exception:  # noqa: BLE001
        return None


def start(
    source: str,
    harness: str,
    stage: str,
    ticket: str | None = None,
    parent: str | None = None,
    model: str | None = None,
    title: str = "",
) -> str | None:
    args = ["start", "--source", source, "--harness", harness, "--stage", stage]
    for flag, value in (("--ticket", ticket), ("--parent", parent), ("--model", model)):
        if value:
            args += [flag, value]
    if title:
        args += ["--title", title]
    lines = [ln.strip() for ln in _run(args).splitlines() if ln.strip()]
    return lines[-1] if lines else None


def heartbeat(execution_id: str) -> None:
    _run(["heartbeat", execution_id], timeout=3.0)


def finish(execution_id: str, status: str = "succeeded", evidence: str = "") -> None:
    args = ["finish", execution_id, "--status", status]
    if evidence:
        args += ["--evidence", evidence]
    _run(args, timeout=3.0)
