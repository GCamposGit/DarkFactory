"""Best-effort cgroup memory and OOM counters for the current container."""

from __future__ import annotations

from pathlib import Path
from typing import Any

CGROUP_V2_ROOT = Path("/sys/fs/cgroup")
CGROUP_V1_ROOT = Path("/sys/fs/cgroup/memory")


def _read_int(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="ascii").strip()
        return int(value) if value else None
    except (OSError, UnicodeError, ValueError):
        return None


def _read_key(path: Path, key: str) -> int | None:
    try:
        for line in path.read_text(encoding="ascii").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == key:
                return int(parts[1])
    except (OSError, UnicodeError, ValueError):
        pass
    return None


def _unlimited(value: int | str | None) -> int | None:
    if value is None or value == "max":
        return None
    try:
        number = int(value)
        # cgroup v1 uses a very large sentinel for an unlimited limit.
        return None if number >= (1 << 60) else number
    except (TypeError, ValueError):
        return None


def read_container_memory(
    *, cgroup_v2: Path = CGROUP_V2_ROOT, cgroup_v1: Path = CGROUP_V1_ROOT
) -> dict[str, Any]:
    """Return memory usage, limit and OOM kill count; unavailable data is null."""
    try:
        current = _read_int(cgroup_v2 / "memory.current")
        if current is not None:
            try:
                limit_raw: int | str | None = (cgroup_v2 / "memory.max").read_text(encoding="ascii").strip()
            except OSError:
                limit_raw = None
            return {
                "available": True,
                "version": 2,
                "current_bytes": current,
                "limit_bytes": _unlimited(limit_raw),
                "oom_kill_count": _read_key(cgroup_v2 / "memory.events", "oom_kill"),
            }

        current = _read_int(cgroup_v1 / "memory.usage_in_bytes")
        if current is not None:
            return {
                "available": True,
                "version": 1,
                "current_bytes": current,
                "limit_bytes": _unlimited(_read_int(cgroup_v1 / "memory.limit_in_bytes")),
                "oom_kill_count": _read_key(cgroup_v1 / "memory.oom_control", "oom_kill"),
            }
    except Exception:  # noqa: BLE001 - diagnostics must never affect Hub liveness
        pass
    return {
        "available": False,
        "version": None,
        "current_bytes": None,
        "limit_bytes": None,
        "oom_kill_count": None,
    }
