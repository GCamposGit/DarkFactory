"""Process identity that survives wall-clock and boot-time drift (USR-317).

``psutil.Process.create_time()`` is ``boot_time + starttime`` on Linux and
``boot_time`` is recomputed after every VM freeze/restore, so the same live
process reports a creation time that moves by minutes. Comparing it with a
recorded value declared live processes dead. On Linux the start time in clock
ticks since boot (``/proc/<pid>/stat`` field 22) is immutable for the life of a
process and independent of ``boot_time``; it is recorded at launch and compared
exactly. Windows and macOS keep the ``create_time`` comparison with tolerance.

Stdlib only; shared by ``remote_worker`` and ``suite_lock``.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

#: Tolerance (seconds) of the ``create_time`` fallback used when no ticks were recorded.
CREATE_TIME_TOLERANCE_SEC = 2.0


def parse_proc_stat_start_ticks(stat_text: str) -> Optional[int]:
    """Field 22 (``starttime``) of a ``/proc/<pid>/stat`` line, or ``None``.

    The command name (field 2) is wrapped in parentheses and may contain spaces
    and ``)``, so the fields are split after the LAST ``)``.
    """

    close = stat_text.rfind(")")
    if close < 0:
        return None
    fields = stat_text[close + 1 :].split()
    # fields[0] is field 3 (state), so field 22 is fields[19].
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def proc_start_ticks(pid: Optional[int] = None) -> Optional[int]:
    """Start time of ``pid`` (default: this process) in clock ticks; Linux only, else ``None``."""

    if not sys.platform.startswith("linux"):
        return None
    try:
        target = os.getpid() if pid is None else int(pid)
        with open(f"/proc/{target}/stat", encoding="utf-8", errors="replace") as handle:
            return parse_proc_stat_start_ticks(handle.read())
    except (OSError, ValueError):
        return None


def same_process(
    proc: Any,
    started_at: Optional[float],
    start_ticks: Optional[int] = None,
    *,
    tolerance_sec: float = CREATE_TIME_TOLERANCE_SEC,
    only_younger: bool = False,
) -> bool:
    """True when the live psutil ``proc`` is the process recorded at launch.

    With ``start_ticks`` recorded the comparison is exact and ignores
    ``create_time`` (a pid recycled by another process has another starttime;
    an unreadable ``/proc`` entry means the process is gone). Without it
    (Windows/macOS, or a record written before USR-317) ``create_time`` is
    compared with ``tolerance_sec``; ``only_younger`` rejects only a process
    created AFTER the record (the suite lock's historic rule).
    """

    if start_ticks is not None:
        return proc_start_ticks(proc.pid) == int(start_ticks)
    if started_at is None:
        return True
    delta = float(proc.create_time()) - float(started_at)
    return delta <= tolerance_sec if only_younger else abs(delta) <= tolerance_sec
