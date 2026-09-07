"""Warm in-memory cache and telemetry for immutable roadmap snapshots."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from threading import RLock
from typing import Any

from core.roadmap.compiler import RoadmapCompiler
from core.roadmap.models import FreshnessStatus, RoadmapSnapshot


class RoadmapUnavailableError(RuntimeError):
    """Raised when no trustworthy snapshot exists for an unavailable source."""


@dataclass
class StoreTelemetry:
    """Observability and scale telemetry for roadmap cache and compilation."""

    requests_total: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    compilations_count: int = 0
    last_compile_latency_ms: float = 0.0
    total_compile_latency_ms: float = 0.0
    average_compile_latency_ms: float = 0.0
    items_count: int = 0
    relations_count: int = 0
    source_changes_detected: int = 0

    @property
    def hit_ratio(self) -> float:
        if self.requests_total == 0:
            return 0.0
        return round(self.cache_hits / self.requests_total, 4)


class RoadmapSnapshotStore:
    """Cache snapshots by project and source fingerprint with telemetry tracking.

    The compiler is consulted to detect source changes. Repeated requests with the
    same fingerprint return the previous immutable snapshot object, keeping the
    warmed HTTP path cheap (< 5ms) and deterministic.
    """

    def __init__(self) -> None:
        self._entries: dict[str, RoadmapSnapshot] = {}
        self._telemetry: dict[str, StoreTelemetry] = {}
        self._last_source_fingerprints: dict[str, str] = {}
        self._lock = RLock()

    def _get_telemetry_entry(self, project_id: str) -> StoreTelemetry:
        if project_id not in self._telemetry:
            self._telemetry[project_id] = StoreTelemetry()
        return self._telemetry[project_id]

    def get_or_compile(self, project_id: str, compiler: RoadmapCompiler) -> RoadmapSnapshot:
        with self._lock:
            cached = self._entries.get(project_id)
        start_time = time.perf_counter()
        candidate = compiler.compile(project_id, previous_snapshot=cached)
        compile_duration = time.perf_counter() - start_time
        compile_latency_ms = round(compile_duration * 1000, 3)

        with self._lock:
            telemetry = self._get_telemetry_entry(project_id)
            telemetry.requests_total += 1

            cached = self._entries.get(project_id)
            previous_fp = self._last_source_fingerprints.get(project_id)

            # Check if source fingerprint changed from previous observation
            if previous_fp is not None and previous_fp != candidate.source_fingerprint:
                telemetry.source_changes_detected += 1
            self._last_source_fingerprints[project_id] = candidate.source_fingerprint

            if cached and cached.source_fingerprint == candidate.source_fingerprint:
                telemetry.cache_hits += 1
                if candidate.sources_unavailable:
                    return self._stale_snapshot(cached, candidate)
                return cached

            # Cache miss / first compilation
            telemetry.cache_misses += 1
            telemetry.compilations_count += 1
            telemetry.last_compile_latency_ms = compile_latency_ms
            telemetry.total_compile_latency_ms += compile_latency_ms
            telemetry.average_compile_latency_ms = round(
                telemetry.total_compile_latency_ms / telemetry.compilations_count, 3
            )
            telemetry.items_count = len(candidate.items)
            telemetry.relations_count = sum(len(it.dependencies) for it in candidate.items)

            if candidate.sources_unavailable and cached:
                return self._stale_snapshot(cached, candidate)
            if candidate.sources_unavailable and not candidate.items:
                raise RoadmapUnavailableError(
                    "No previous roadmap snapshot is available while a canonical source is unavailable."
                )
            self._entries[project_id] = candidate
            return candidate

    def get_telemetry(self, project_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            if project_id:
                entry = self._telemetry.get(project_id, StoreTelemetry())
                res = asdict(entry)
                res["hit_ratio"] = entry.hit_ratio
                return res

            # Aggregated telemetry across all projects
            total = StoreTelemetry()
            for entry in self._telemetry.values():
                total.requests_total += entry.requests_total
                total.cache_hits += entry.cache_hits
                total.cache_misses += entry.cache_misses
                total.compilations_count += entry.compilations_count
                total.total_compile_latency_ms += entry.total_compile_latency_ms
                total.source_changes_detected += entry.source_changes_detected
                total.items_count = max(total.items_count, entry.items_count)
                total.relations_count = max(total.relations_count, entry.relations_count)
            if total.compilations_count > 0:
                total.average_compile_latency_ms = round(
                    total.total_compile_latency_ms / total.compilations_count, 3
                )
            res = asdict(total)
            res["hit_ratio"] = total.hit_ratio
            return res

    def clear(self, project_id: str | None = None) -> None:
        with self._lock:
            if project_id is None:
                self._entries.clear()
                self._last_source_fingerprints.clear()
            else:
                self._entries.pop(project_id, None)
                self._last_source_fingerprints.pop(project_id, None)

    @staticmethod
    def _stale_snapshot(cached: RoadmapSnapshot, failed: RoadmapSnapshot) -> RoadmapSnapshot:
        stale_items = [
            item.model_copy(update={"freshness_status": FreshnessStatus.STALE})
            for item in cached.items
        ]
        stale_states = [
            state.model_copy(update={"status": "stale"})
            if state.source_id in failed.sources_unavailable else state
            for state in cached.sources_consulted
        ]
        return cached.model_copy(update={
            "items": stale_items,
            "sources_consulted": stale_states,
            "sources_unavailable": failed.sources_unavailable,
            "issues": cached.issues + failed.issues,
        })
