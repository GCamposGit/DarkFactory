"""Warm in-memory cache for immutable roadmap snapshots."""

from __future__ import annotations

from threading import RLock

from core.roadmap.compiler import RoadmapCompiler
from core.roadmap.models import FreshnessStatus, RoadmapSnapshot


class RoadmapUnavailableError(RuntimeError):
    """Raised when no trustworthy snapshot exists for an unavailable source."""


class RoadmapSnapshotStore:
    """Cache snapshots by project and source fingerprint.

    The compiler is still consulted on each request to detect source changes.
    Repeated requests with the same fingerprint return the previous immutable
    snapshot object, keeping the warmed HTTP path cheap and deterministic.
    """

    def __init__(self) -> None:
        self._entries: dict[str, RoadmapSnapshot] = {}
        self._lock = RLock()

    def get_or_compile(self, project_id: str, compiler: RoadmapCompiler) -> RoadmapSnapshot:
        candidate = compiler.compile(project_id)
        with self._lock:
            cached = self._entries.get(project_id)
            if cached and cached.source_fingerprint == candidate.source_fingerprint:
                if candidate.sources_unavailable:
                    return self._stale_snapshot(cached, candidate)
                return cached
            if candidate.sources_unavailable and cached:
                return self._stale_snapshot(cached, candidate)
            if candidate.sources_unavailable and not candidate.items:
                raise RoadmapUnavailableError(
                    "No previous roadmap snapshot is available while a canonical source is unavailable."
                )
            self._entries[project_id] = candidate
            return candidate

    def clear(self, project_id: str | None = None) -> None:
        with self._lock:
            if project_id is None:
                self._entries.clear()
            else:
                self._entries.pop(project_id, None)

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
