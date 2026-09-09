"""Concurrent, failure-isolated account quota monitor."""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.usage.adapters import AccountUsageAdapter, build_default_adapters
from core.usage.models import AccountConnectionStatus, AccountUsageReport, ProviderAccountUsage

logger = logging.getLogger(__name__)


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
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        file_target = self.snapshot_dir / f"{provider_id}.json"
        existing: Dict[str, Any] = {}
        if file_target.is_file():
            try:
                existing = json.loads(file_target.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Failed to read existing snapshot %s: %s", file_target, exc)
        existing.update(payload)
        file_target.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
        with self._lock:
            self._cache = None
            self._cached_at = 0.0

    def inspect(self, force: bool = False) -> AccountUsageReport:
        with self._lock:
            if not force and self._cache is not None and time.monotonic() - self._cached_at < self.cache_ttl_sec:
                return self._cache

        accounts: List[ProviderAccountUsage] = []
        workers = min(6, max(1, len(self.adapters)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="quota-probe") as executor:
            futures = {executor.submit(adapter.inspect): adapter for adapter in self.adapters}
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
