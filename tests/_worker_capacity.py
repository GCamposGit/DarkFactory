"""Worker capacity and memory-safe process budgeting for pytest-xdist (USR-95).

Prevents Windows fatal exceptions (code 0x8007000e / E_OUTOFMEMORY) during
parallel test runs when available memory is constrained (e.g. notebook with
browsers and IDEs open). Mitigates WMI queries during worker startup and
dynamically caps xdist workers based on psutil available memory.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

# Memory constants (bytes)
BYTES_PER_GIB = 1024 * 1024 * 1024
DEFAULT_MIN_WORKER_MEMORY_BYTES = int(2.0 * BYTES_PER_GIB)  # 2.0 GiB per worker
DEFAULT_SYSTEM_RESERVE_BYTES = int(2.0 * BYTES_PER_GIB)     # 2.0 GiB for OS + controller
DEFAULT_HARD_CAP_WORKERS = 8                                 # Upper limit on standard hosts


def get_available_memory_bytes() -> int:
    """Return available system memory in bytes, or a conservative fallback."""
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except Exception:
        # Fallback if psutil fails: assume 4 GiB conservative
        return 4 * BYTES_PER_GIB


def get_physical_cpu_count() -> int:
    """Return physical CPU count, or logical count, or fallback to 1."""
    try:
        import psutil
        count = psutil.cpu_count(logical=False) or psutil.cpu_count(logical=True)
        if count and count > 0:
            return int(count)
    except Exception:
        pass

    try:
        count = os.cpu_count()
        if count and count > 0:
            return int(count)
    except Exception:
        pass

    return 1


def calculate_safe_worker_cap(
    available_bytes: int | None = None,
    cpu_count: int | None = None,
    min_bytes_per_worker: int = DEFAULT_MIN_WORKER_MEMORY_BYTES,
    reserve_bytes: int = DEFAULT_SYSTEM_RESERVE_BYTES,
    hard_cap: int = DEFAULT_HARD_CAP_WORKERS,
) -> int:
    """Calculate the maximum safe number of pytest-xdist workers for available RAM.

    Logic:
    1. Check DARKFAC_MAX_WORKERS environment override.
    2. Check PYTEST_XDIST_AUTO_NUM_WORKERS environment override.
    3. Calculate workers supported by available memory: (available - reserve) // per_worker.
    4. Bound by physical CPU count.
    5. Bound by hard_cap (default 8).
    6. Ensure at least 1 worker.
    """
    # 1. Direct explicit environment overrides
    env_max = os.environ.get("DARKFAC_MAX_WORKERS")
    if env_max:
        try:
            val = int(env_max)
            if val > 0:
                return val
        except ValueError:
            logger.warning("Invalid DARKFAC_MAX_WORKERS: %r; ignoring", env_max)

    env_xdist = os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS")
    if env_xdist:
        try:
            val = int(env_xdist)
            if val > 0:
                return val
        except ValueError:
            pass

    # 2. Hardware metrics
    if available_bytes is None:
        available_bytes = get_available_memory_bytes()
    if cpu_count is None:
        cpu_count = get_physical_cpu_count()

    usable_bytes = max(0, available_bytes - reserve_bytes)
    workers_by_memory = int(usable_bytes // min_bytes_per_worker)

    # At least 1 worker if memory is severely constrained
    if workers_by_memory < 1:
        workers_by_memory = 1

    # Safe cap is bounded by memory, physical CPUs, and the hard cap
    safe_cap = min(cpu_count, workers_by_memory, hard_cap)
    return max(1, safe_cap)


def mitigate_windows_wmi_startup() -> None:
    """Pre-warm and safeguard platform info to prevent non-continuable 0x8007000e in WMI queries.

    On Windows, platform.uname() and platform.win32_ver() invoke C extension _wmi.exec_query().
    Under low-memory or high commit-charge conditions, _wmi raises a non-continuable SEH exception
    (0x8007000e / E_OUTOFMEMORY), which causes PYTHONFAULTHANDLER to crash the worker process.
    Bypassing _wmi_query allows platform to fall back immediately to native Win32 APIs
    (sys.getwindowsversion, winreg), which are fast, reliable, and consume zero COM/WMI RPC resources.
    """
    if sys.platform != "win32":
        return

    import platform

    # Pre-populate uname cache so future calls return instantly
    try:
        platform.uname()
    except Exception:
        pass

    # Safeguard _wmi_query if present
    if hasattr(platform, "_wmi_query"):
        def _safe_wmi_query(*args: Any, **kwargs: Any) -> Any:
            raise OSError("WMI queries bypassed in DarkFac test suite for worker stability (USR-95)")

        platform._wmi_query = _safe_wmi_query  # type: ignore[attr-defined]
