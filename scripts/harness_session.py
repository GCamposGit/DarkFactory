#!/usr/bin/env python3
"""Wrapper that registers a harness session in the execution registry (USR-209).

    python C:\\dev\\DarkFac\\scripts\\harness_session.py codex -- codex
    python C:\\dev\\DarkFac\\scripts\\harness_session.py grok -- grok
    python C:\\dev\\DarkFac\\scripts\\harness_session.py antigravity -- agy

Registers start, beats every 30 s while the command runs and finishes with its exit status. The command's exit
code is returned unchanged; registry failures are swallowed and never block the session.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import exec_cli  # noqa: E402

HARNESSES = ("codex", "grok", "antigravity", "claude")
HEARTBEAT_ENV = "DARKFAC_EXEC_HOOK_HEARTBEAT_S"


def _beat_loop(eid: str, stop: threading.Event, interval: float) -> None:
    while not stop.wait(interval):
        exec_cli.heartbeat(eid)


def run(harness: str, command: list[str], ticket: str | None = None) -> int:
    if not command:
        print("harness_session: nenhum comando informado apos '--'", file=sys.stderr)
        return 2
    eid = None
    try:
        eid = exec_cli.start(
            f"{harness}_session",
            harness,
            "interactive_session",
            ticket or exec_cli.detect_ticket(),
            title=" ".join(command)[:120],
        )
    except Exception:  # noqa: BLE001
        eid = None
    stop = threading.Event()
    if eid:
        try:
            interval = float(os.environ.get(HEARTBEAT_ENV, "30"))
        except ValueError:
            interval = 30.0
        threading.Thread(target=_beat_loop, args=(eid, stop, interval), daemon=True).start()
    code = 1
    try:
        code = subprocess.call(command)  # noqa: S603 - the canonical launch command, inherits the terminal
    except FileNotFoundError:
        print(f"harness_session: comando nao encontrado: {command[0]}", file=sys.stderr)
        code = 127
    except KeyboardInterrupt:
        code = 130
    finally:
        stop.set()
        if eid:
            status = "succeeded" if code == 0 else ("cancelled" if code == 130 else "failed")
            exec_cli.finish(eid, status, f"exit {code}")
    return code


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in argv:
        idx = argv.index("--")
        head, command = argv[:idx], argv[idx + 1 :]
    else:
        head, command = argv, []
    parser = argparse.ArgumentParser(prog="harness_session.py")
    parser.add_argument("harness", choices=HARNESSES)
    parser.add_argument("--ticket", default=None)
    args = parser.parse_args(head)
    return run(args.harness, command, args.ticket)


if __name__ == "__main__":
    raise SystemExit(main())
