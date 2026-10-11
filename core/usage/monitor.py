"""Concurrent, failure-isolated account quota monitor."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.usage.adapters import AccountUsageAdapter, build_default_adapters
from core.usage.models import AccountConnectionStatus, AccountUsageReport, ProviderAccountUsage

from datetime import datetime, timezone
import inspect as py_inspect

logger = logging.getLogger(__name__)

# A provider id names a file (`<dir>/<provider_id>.json`) and arrives from outside the process (the
# sync endpoint), so it is a slug: no separators, no dots, no leading dash, at most 64 characters.
PROVIDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def is_valid_provider_id(provider_id: Any) -> bool:
    """True only for a string that is a safe provider slug (see `PROVIDER_ID_PATTERN`)."""
    return isinstance(provider_id, str) and PROVIDER_ID_PATTERN.fullmatch(provider_id) is not None


def snapshot_path(directory: Path, provider_id: Any) -> Path:
    """`<directory>/<provider_id>.json`, or `ValueError` if the id is unsafe or the path escapes `directory`."""
    if not is_valid_provider_id(provider_id):
        raise ValueError(f"Invalid provider id: {repr(provider_id)[:40]}")
    base = Path(directory)
    target = base / f"{provider_id}.json"
    if target.resolve().parent != base.resolve():
        raise ValueError(f"Provider id escapes the snapshot directory: {repr(provider_id)[:40]}")
    return target


class AccountUsageMonitor:
    """Inspect every provider without allowing one failed account to fail the Hub."""

    def __init__(
        self,
        snapshot_dir: Path,
        adapters: Optional[Iterable[AccountUsageAdapter]] = None,
        cache_ttl_sec: float = 30.0,
    ) -> None:
        self.snapshot_dir = Path(snapshot_dir)
        self.adapters = list(adapters) if adapters is not None else list(build_default_adapters(self.snapshot_dir))
        self.cache_ttl_sec = max(0.0, cache_ttl_sec)
        self._cache: Optional[AccountUsageReport] = None
        self._cached_at = 0.0
        self._lock = threading.RLock()

    def save_snapshot(self, provider_id: str, payload: Dict[str, Any]) -> None:
        """Persist or update an account quota snapshot JSON file and invalidate cache."""
        file_target = snapshot_path(self.snapshot_dir, provider_id)  # refuse an unsafe id before touching disk
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        existing: Dict[str, Any] = {}
        if file_target.is_file():
            try:
                existing = json.loads(file_target.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Failed to read existing snapshot %s: %s", file_target, exc)
        existing.update(payload)
        existing.setdefault("checked_at", datetime.now(timezone.utc).isoformat())
        # Write-then-replace: a reader (the Hub inspecting every request) must never see a half-written
        # snapshot and report the provider as "no synchronized data" until the next sync.
        serialized = json.dumps(existing, indent=2, ensure_ascii=False)
        tmp_target = file_target.with_name(f".{file_target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp_target.write_text(serialized, encoding="utf-8")
            for attempt in range(3):
                try:
                    os.replace(tmp_target, file_target)
                    break
                except PermissionError:
                    # Windows refuses to replace a file another process is reading right now.
                    if attempt == 2:
                        file_target.write_text(serialized, encoding="utf-8")
                        break
                    time.sleep(0.05)
        finally:
            tmp_target.unlink(missing_ok=True)
        with self._lock:
            self._cache = None
            self._cached_at = 0.0

    @staticmethod
    def _run_adapter_inspect(adapter: AccountUsageAdapter, force: bool) -> ProviderAccountUsage:
        try:
            sig = py_inspect.signature(adapter.inspect)
            if "force" in sig.parameters:
                return adapter.inspect(force=force)
            return adapter.inspect()
        except TypeError:
            return adapter.inspect()

    def inspect(self, force: bool = False) -> AccountUsageReport:
        with self._lock:
            if not force and self._cache is not None and time.monotonic() - self._cached_at < self.cache_ttl_sec:
                return self._cache

        accounts: List[ProviderAccountUsage] = []
        workers = min(6, max(1, len(self.adapters)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="quota-probe") as executor:
            futures = {executor.submit(self._run_adapter_inspect, adapter, force): adapter for adapter in self.adapters}
            for future in as_completed(futures):
                adapter = futures[future]
                try:
                    accounts.append(future.result())
                except Exception as exc:
                    logger.warning("Provider adapter %s failed: %s", adapter.spec.provider_id, exc)
                    accounts.append(adapter.degraded("Falha isolada ao consultar esta plataforma.", type(adapter).__name__))

        order = {adapter.spec.provider_id: index for index, adapter in enumerate(self.adapters)}
        accounts.sort(key=lambda item: order.get(item.provider_id, len(order)))
        report = AccountUsageReport(
            accounts=accounts,
            connected_count=sum(item.status == AccountConnectionStatus.CONNECTED for item in accounts),
            limited_count=sum(item.status == AccountConnectionStatus.LIMITED for item in accounts),
            disconnected_count=sum(item.status == AccountConnectionStatus.DISCONNECTED for item in accounts),
        )
        with self._lock:
            self._cache = report
            self._cached_at = time.monotonic()
        return report
