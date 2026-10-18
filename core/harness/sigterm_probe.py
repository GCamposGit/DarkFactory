"""SIGTERM sender probe for the worker's job runner (USR-298, POSIX only).

The Grokbox VM is frozen and restored by its platform; right after a restore processes receive
SIGTERM and the job dies with exit -15 and no verdict. To learn WHO sends the signal, the worker runs
the job's runner under this supervisor::

    python -m core.harness.sigterm_probe -- python core/harness/runner.py --quick --local

The supervisor starts the runner first (so the runner inherits a clean signal mask), then blocks
SIGTERM in itself and waits for it with ``signal.sigwaitinfo`` in a thread. On a SIGTERM it logs the
sender PID/UID/time to stderr (the worker streams it into the job log), forwards SIGTERM to the
runner, waits for it, then dies of SIGTERM itself with the default disposition: the signal is never
swallowed. Where ``sigwaitinfo`` does not exist (Windows) the supervisor just execs the command.
"""

from __future__ import annotations

import datetime as dt
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional, Sequence

PROBE_MARKER = "[SIGTERM_PROBE]"


def probe_supported() -> bool:
    return os.name != "nt" and hasattr(signal, "sigwaitinfo") and hasattr(signal, "pthread_sigmask")


def _sender_cmdline(pid: int) -> str:
    """Best-effort command line of the sender (``/proc``); empty when unavailable or already gone."""

    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()[:200]


def format_probe_line(
    signum: int, sender_pid: int, sender_uid: int, when: dt.datetime, sender_cmdline: str = ""
) -> str:
    """The job-log line describing one received signal."""

    try:
        name = signal.Signals(signum).name
    except ValueError:
        name = str(signum)
    line = (
        f"{PROBE_MARKER} received {name} ({signum}) at {when.isoformat()} "
        f"from pid={sender_pid} uid={sender_uid}"
    )
    if sender_cmdline:
        line += f" cmdline={sender_cmdline!r}"
    return line


def _die_like(signum: int) -> None:
    """Terminate this process by ``signum`` with the default disposition (no swallowing)."""

    signal.signal(signum, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signum})
    os.kill(os.getpid(), signum)


def run_supervised(command: Sequence[str]) -> int:
    """Run ``command`` under the probe; returns its exit code (never returns after a SIGTERM)."""

    if not probe_supported():
        return subprocess.call(list(command))

    child = subprocess.Popen(list(command))  # before blocking: the child must not inherit the mask
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    received = threading.Event()

    def _wait_for_sigterm() -> None:
        while True:
            info = signal.sigwaitinfo({signal.SIGTERM})
            received.set()
            when = dt.datetime.now(dt.timezone.utc)
            print(
                format_probe_line(info.si_signo, info.si_pid, info.si_uid, when, _sender_cmdline(info.si_pid)),
                file=sys.stderr,
                flush=True,
            )
            try:
                child.send_signal(signal.SIGTERM)
            except OSError:
                pass

    threading.Thread(target=_wait_for_sigterm, daemon=True, name="darkfac-sigterm-probe").start()
    returncode = child.wait()
    if received.is_set():
        _die_like(signal.SIGTERM)
    if returncode < 0:
        _die_like(-returncode)  # keep "killed by signal N" visible to the worker as returncode -N
    return returncode


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--" in args:
        args = args[args.index("--") + 1 :]
    if not args:
        print(f"{PROBE_MARKER} usage: python -m core.harness.sigterm_probe -- <command> [args...]", file=sys.stderr)
        return 2
    return run_supervised(args)


if __name__ == "__main__":
    raise SystemExit(main())
