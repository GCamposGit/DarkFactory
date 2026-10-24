"""Read memory usage and OOM counters from the current container's cgroup."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, ConfigDict

_MIB = 1024 * 1024
_V1_UNLIMITED = 1 << 60
logger = logging.getLogger("dark_factory.infra.container_memory")
MEMORY_ALERT_THRESHOLD_PERCENT = 80.0
MEMORY_ALERT_DEDUPE_SECONDS = 6 * 60 * 60
MEMORY_SAMPLE_INTERVAL_SECONDS = 30.0


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


def _telegram_ops_notifier(message: str) -> bool:
    """Send an operational alert to configured Telegram ops chats, if available."""
    from core.integrations.telegram import TelegramGateway, load_telegram_config

    config = load_telegram_config(role="ops")
    if not config.bot_token:
        logger.warning("container memory alert not sent: Telegram ops bot is not configured")
        return False
    gateway = TelegramGateway(config=config)
    targets = list(config.authorized_chat_ids) or list(config.authorized_user_ids)
    if not targets:
        logger.warning("container memory alert not sent: no Telegram ops recipients are configured")
        return False
    sent = False
    for chat_id in targets:
        sent = bool(gateway.send_message(chat_id=chat_id, text=message)) or sent
    return sent


class ContainerMemorySampler:
    """Track container memory peaks and notify on threshold and OOM counter changes."""

    def __init__(
        self,
        *,
        reader: Callable[[], ContainerMemory] = read_container_memory,
        notifier: Callable[[str], object] = _telegram_ops_notifier,
        clock: Callable[[], float] = time.monotonic,
        dedupe_window_seconds: float = MEMORY_ALERT_DEDUPE_SECONDS,
    ) -> None:
        self.reader = reader
        self.notifier = notifier
        self.clock = clock
        self.dedupe_window_seconds = max(0.0, dedupe_window_seconds)
        self.peak_mb: Optional[float] = None
        self.peak_percent: Optional[float] = None
        self._last_threshold_alert: Optional[float] = None
        self._last_oom_kills: Optional[int] = None
        self._lock = threading.Lock()

    def sample_once(self) -> bool:
        """Read and process one sample; return whether cgroup memory is bounded."""
        try:
            sample = self.reader()
        except Exception as exc:
            logger.warning("container memory sample failed (%s)", type(exc).__name__)
            return False
        if sample.limit_mb is None or sample.limit_mb <= 0:
            return False

        with self._lock:
            if sample.peak_mb is not None:
                self.peak_mb = max(self.peak_mb or 0.0, sample.peak_mb)
            if sample.used_mb is not None:
                self.peak_mb = max(self.peak_mb or 0.0, sample.used_mb)
            if self.peak_mb is not None:
                self.peak_percent = max(self.peak_percent or 0.0, self.peak_mb * 100 / sample.limit_mb)

            now = self.clock()
            observed_percent = max(
                sample.percent_of_limit or 0.0,
                (sample.peak_mb * 100 / sample.limit_mb) if sample.peak_mb is not None else 0.0,
            )
            if (
                observed_percent >= MEMORY_ALERT_THRESHOLD_PERCENT
                and (self._last_threshold_alert is None or now - self._last_threshold_alert >= self.dedupe_window_seconds)
            ):
                self._notify(
                    f"DarkHub memory warning: {observed_percent:.1f}% of "
                    f"{sample.limit_mb:.0f} MB limit (peak {self.peak_mb:.1f} MB)."
                )
                self._last_threshold_alert = now

            if sample.oom_kills is not None:
                previous = self._last_oom_kills
                self._last_oom_kills = sample.oom_kills
                if previous is not None and sample.oom_kills > previous:
                    self._notify(
                        f"DarkHub OOM alert: oom_kill counter increased from {previous} to {sample.oom_kills}."
                    )
        return True

    def _notify(self, message: str) -> None:
        try:
            self.notifier(message)
        except Exception as exc:
            logger.warning("container memory notifier failed (%s)", type(exc).__name__)

    def run(self, stop_event: threading.Event, *, interval_seconds: float = MEMORY_SAMPLE_INTERVAL_SECONDS) -> None:
        """Sample periodically until the supplied event is set."""
        interval = max(0.1, interval_seconds)
        deadline = self.clock()
        while not stop_event.is_set():
            self.sample_once()
            deadline += interval
            stop_event.wait(max(0.0, deadline - self.clock()))


def start_background_memory_sampler(
    *,
    reader: Callable[[], ContainerMemory] = read_container_memory,
    notifier: Callable[[str], object] = _telegram_ops_notifier,
    interval_seconds: float = MEMORY_SAMPLE_INTERVAL_SECONDS,
) -> Optional[tuple[threading.Thread, threading.Event, ContainerMemorySampler]]:
    """Start the daemon sampler only when the initial cgroup read has a known limit."""
    try:
        initial = reader()
    except Exception as exc:
        logger.warning("container memory sampler disabled: initial cgroup read failed (%s)", type(exc).__name__)
        return None
    if initial.limit_mb is None or initial.limit_mb <= 0:
        logger.info("container memory sampler disabled: no readable cgroup memory limit")
        return None

    sampler = ContainerMemorySampler(reader=reader, notifier=notifier)
    # Preserve the initial cgroup peak and OOM baseline without issuing an alert on startup.
    sampler.peak_mb = max(initial.peak_mb or 0.0, initial.used_mb or 0.0)
    sampler.peak_percent = (
        initial.peak_mb * 100 / initial.limit_mb
        if initial.peak_mb is not None and initial.limit_mb
        else initial.percent_of_limit
    )
    sampler._last_oom_kills = initial.oom_kills
    stop_event = threading.Event()
    thread = threading.Thread(
        target=sampler.run,
        kwargs={"stop_event": stop_event, "interval_seconds": interval_seconds},
        name="darkhub-container-memory",
        daemon=True,
    )
    thread.start()
    return thread, stop_event, sampler
