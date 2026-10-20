#!/usr/bin/env python3
"""Claude Code hook (USR-209): registers/closes sessions and subagents in the execution registry.

Events: SessionStart, SessionEnd, SubagentStart, SubagentStop (JSON on stdin). The foreground call only spawns a
detached worker (or drops a stop marker) and exits 0 immediately; the worker owns the execution: start, heartbeat
every 30 s and finish when the matching stop marker appears. Never raises, always exit 0.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import exec_cli  # noqa: E402

STATE_ENV = "DARKFAC_EXEC_HOOK_STATE_DIR"
HEARTBEAT_ENV = "DARKFAC_EXEC_HOOK_HEARTBEAT_S"
MAX_LIFETIME_S = 8 * 3600
START_EVENTS = {"SessionStart": "claude_session", "SubagentStart": "subagent"}
STOP_EVENTS = {"SessionEnd": "claude_session", "SubagentStop": "subagent"}


def state_dir() -> Path:
    override = os.environ.get(STATE_ENV, "").strip()
    return Path(override) if override else Path(tempfile.gettempdir()) / "darkfac_exec_hooks"


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:100] or "unknown"


def key_for(event: str, payload: dict) -> str:
    session = str(payload.get("session_id") or "nosession")
    if event.startswith("Subagent"):
        return f"sub-{_safe(session)}-{_safe(str(payload.get('agent_id') or 'noagent'))}"
    return f"ses-{_safe(session)}"


def _worker(event: str, key: str, payload: dict) -> int:
    sdir = state_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    source = START_EVENTS[event]
    parent = None
    if source == "subagent":
        parent_file = sdir / f"ses-{_safe(str(payload.get('session_id') or 'nosession'))}.id"
        if parent_file.is_file():
            parent = parent_file.read_text(encoding="utf-8").strip() or None
    stage = "subagent" if source == "subagent" else "interactive_session"
    title = str(payload.get("agent_type") or payload.get("cwd") or "")[:120]
    eid = exec_cli.start(
        source, "claude", stage, exec_cli.detect_ticket(payload.get("cwd")), parent, model=payload.get("model"), title=title
    )
    if not eid:
        return 0
    (sdir / f"{key}.id").write_text(eid, encoding="utf-8")
    stop_file = sdir / f"{key}.stop"
    try:
        interval = float(os.environ.get(HEARTBEAT_ENV, "30"))
    except ValueError:
        interval = 30.0
    deadline = time.monotonic() + MAX_LIFETIME_S
    next_beat = time.monotonic() + interval
    while time.monotonic() < deadline:
        if stop_file.exists():
            status = stop_file.read_text(encoding="utf-8").strip() or "succeeded"
            exec_cli.finish(eid, status)
            break
        if time.monotonic() >= next_beat:
            exec_cli.heartbeat(eid)
            next_beat = time.monotonic() + interval
        time.sleep(min(0.2, interval))
    else:
        exec_cli.finish(eid, "cancelled", "hook worker lifetime exceeded")
    for suffix in (".stop", ".id"):
        try:
            (sdir / f"{key}{suffix}").unlink()
        except OSError:
            pass
    return 0


def _spawn_worker(event: str, key: str, payload: dict) -> None:
    kwargs: dict = {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, str(Path(__file__).resolve()), "--worker", event, key], **kwargs
    )
    if proc.stdin is not None:
        proc.stdin.write(json.dumps(payload).encode("utf-8"))
        proc.stdin.close()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv[:1] == ["--worker"]:
            payload = json.loads(sys.stdin.read() or "{}")
            return _worker(argv[1], argv[2], payload)
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        event = str(payload.get("hook_event_name") or (argv[0] if argv else ""))
        key = key_for(event, payload)
        if event in START_EVENTS:
            _spawn_worker(event, key, payload)
        elif event in STOP_EVENTS:
            sdir = state_dir()
            sdir.mkdir(parents=True, exist_ok=True)
            reason = str(payload.get("reason") or "")
            (sdir / f"{key}.stop").write_text(
                "cancelled" if reason in {"logout", "other"} else "succeeded", encoding="utf-8"
            )
    except Exception:  # noqa: BLE001 - a hook never fails the session
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
