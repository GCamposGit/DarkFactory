"""Read memory usage and OOM counters from the current container's cgroup."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict

_MIB = 1024 * 1024
_V1_UNLIMITED = 1 << 60


class ContainerMemory(BaseModel):
    """Container memory metrics; ``None`` fields mean the cgroup is unknown."""

    model_config = ConfigDict(frozen=True)

    used_mb: Optional[float] = None
    peak_mb: Optional[float] = None
    limit_mb: Optional[float] = None
    percent_of_limit: Optional[float] = None
    oom_kills: Optional[int] = None


def _unknown() -> ContainerMemory:
    return ContainerMemory()


def _integer(path: Path) -> int:
    return int(path.read_text(encoding="utf-8").strip())


def _as_mb(value: int) -> float:
    return round(value / _MIB, 2)


def _read_v2(root: Path) -> ContainerMemory:
    current = _integer(root / "memory.current")
    peak = _integer(root / "memory.peak")
    raw_limit = (root / "memory.max").read_text(encoding="utf-8").strip()
    if raw_limit == "max":
        return _unknown()
    limit = int(raw_limit)
    if limit <= 0:
        return _unknown()
    event_values: dict[str, int] = {}
    for line in (root / "memory.events").read_text(encoding="utf-8").splitlines():
        key, value = line.split()
        event_values[key] = int(value)
    if "oom_kill" not in event_values:
        return _unknown()
    return ContainerMemory(
        used_mb=_as_mb(current),
        peak_mb=_as_mb(peak),
        limit_mb=_as_mb(limit),
        percent_of_limit=current * 100 / limit,
        oom_kills=event_values["oom_kill"],
    )


def _read_v1(root: Path) -> ContainerMemory:
    current = _integer(root / "memory.usage_in_bytes")
    peak = _integer(root / "memory.max_usage_in_bytes")
    limit = _integer(root / "memory.limit_in_bytes")
    if limit <= 0 or limit >= _V1_UNLIMITED:
        return _unknown()
    oom_kills = _integer(root / "memory.failcnt")
    return ContainerMemory(
        used_mb=_as_mb(current),
        peak_mb=_as_mb(peak),
        limit_mb=_as_mb(limit),
        percent_of_limit=current * 100 / limit,
        oom_kills=oom_kills,
    )


def read_container_memory(cgroup_root: Optional[Path] = None) -> ContainerMemory:
    """Return cgroup v2 or v1 metrics, or an all-unknown model on any read error.

    ``cgroup_root`` points to the memory controller directory, which makes the
    reader straightforward to exercise against temporary directory trees.
    """
    try:
        root = Path(cgroup_root) if cgroup_root is not None else Path("/sys/fs/cgroup")
        if (root / "memory.current").is_file():
            return _read_v2(root)
        v1_root = root / "memory" if (root / "memory").is_dir() else root
        if (v1_root / "memory.usage_in_bytes").is_file():
            return _read_v1(v1_root)
    except Exception:
        return _unknown()
    return _unknown()
