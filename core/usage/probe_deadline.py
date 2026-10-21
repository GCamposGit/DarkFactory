"""Hard deadline for external quota/wake probes (USR-321).

An adapter probe can spawn CLIs and open HTTP connections whose individual timeouts add up
(or never fire). :func:`run_with_deadline` bounds the whole probe with a monotonic clock; on
overrun it kills the process tree the probe started and raises :class:`ProbeTimeout`, a
structured error callers translate into "quota unknown" (fail-closed).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

logger = logging.getLogger("darkfac.usage.probe_deadline")

ENV_PROBE_DEADLINE = "DARKFAC_PROBE_DEADLINE_SEC"
DEFAULT_PROBE_DEADLINE_SEC = 10.0

T = TypeVar("T")


def probe_deadline_s() -> float:
    """Configured deadline in seconds (env ``DARKFAC_PROBE_DEADLINE_SEC``, default 10)."""
    raw = os.environ.get(ENV_PROBE_DEADLINE, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_PROBE_DEADLINE_SEC
    except ValueError:
        return DEFAULT_PROBE_DEADLINE_SEC
    return value if value > 0 else DEFAULT_PROBE_DEADLINE_SEC


@dataclass
class ProbeTimeout(Exception):
    """Structured error: the probe for ``provider`` exceeded ``deadline_s``."""

    provider: str
    duration_s: float
    deadline_s: float
    error: str = "probe_timeout"

    def __post_init__(self) -> None:
        super().__init__(f"{self.error}: {self.provider} {self.duration_s:.2f}s > {self.deadline_s:.2f}s")

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": self.error,
            "provider": self.provider,
            "duration_s": round(self.duration_s, 3),
            "deadline_s": self.deadline_s,
        }


def _descendant_pids() -> set[int]:
    try:
        import psutil

        return {p.pid for p in psutil.Process().children(recursive=True)}
    except Exception:  # noqa: BLE001 - psutil is best effort
        return set()



def run_with_deadline(
    provider: str, fn: Callable[[], T], deadline_s: float | None = None
) -> T:
    """Run ``fn`` with a hard deadline; raises :class:`ProbeTimeout` on overrun.

    Exceptions raised by ``fn`` propagate unchanged. Every call logs one structured line with the
    monotonic duration.
    """
    limit = probe_deadline_s() if deadline_s is None else deadline_s
    before = _descendant_pids()
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    started = time.monotonic()
    thread = threading.Thread(target=_target, name=f"probe-{provider}", daemon=True)
    thread.start()
    thread.join(limit)
    duration = time.monotonic() - started
    if thread.is_alive():
        # Never kill here: a child that appeared during the window may belong to another thread of
        # this process (e.g. an agent run in a worker). The probe's own CLI/HTTP timeouts end it;
        # we only abandon the daemon thread and report how many new children are still alive.
        lingering = len(_descendant_pids() - before)
        logger.warning(
            "probe provider=%s outcome=probe_timeout duration_s=%.3f deadline_s=%.1f new_children=%d",
            provider, duration, limit, lingering,
        )
        raise ProbeTimeout(provider=provider, duration_s=duration, deadline_s=limit)
    logger.info(
        "probe provider=%s outcome=%s duration_s=%.3f", provider, "error" if "error" in box else "ok", duration
    )
    if "error" in box:
        raise box["error"]
    return box["value"]  # type: ignore[no-any-return]
