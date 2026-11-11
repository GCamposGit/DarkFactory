"""Persistent waste brake for new BUILD admissions.

The brake tracks completed quota consumptions that did not produce their own
merge. Once the configured threshold is reached, only *new* BUILD admissions
are paused. Work already in progress, corrections and reviews remain eligible.

State changes are persisted atomically and consumption events are idempotent by
event id. The Sentinel is notified once for each pause episode.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


_STATE_VERSION = 1
_NEW_BUILD_STAGES = frozenset({"BUILD"})
_RESUMABLE_STAGES = frozenset(
    {
        "CORRECTION",
        "CORRECAO",
        "FIX",
        "RESUME",
        "RESUMPTION",
        "REVIEW",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ConsumptionCompleted:
    """Idempotent result of one completed quota consumption."""

    event_id: str
    work_id: str
    stage: str
    provider: str = ""
    model: str = ""
    produced_own_merge: bool = False
    completed_at: str = field(default_factory=_utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("event_id must not be empty")
        if not self.work_id.strip():
            raise ValueError("work_id must not be empty")
        if not self.stage.strip():
            raise ValueError("stage must not be empty")


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    reason: str
    paused: bool
    no_merge_sequence: int
    pause_episode: int | None = None


@dataclass(frozen=True)
class ConsumptionUpdate:
    duplicate: bool
    paused: bool
    pause_started: bool
    pause_released: bool
    sentinel_triggered: bool
    no_merge_sequence: int
    pause_episode: int | None


class Sentinel(Protocol):
    def trigger(self, context: Mapping[str, Any]) -> Any:
        """Receive context about a newly started waste-pause episode."""


SentinelCallback = Callable[[Mapping[str, Any]], Any]


@dataclass
class _BrakeState:
    version: int = _STATE_VERSION
    no_merge_sequence: int = 0
    sequence_event_ids: list[str] = field(default_factory=list)
    processed_event_ids: list[str] = field(default_factory=list)
    paused: bool = False
    pause_episode: int = 0
    sentinel_episode: int = 0
    paused_at: str | None = None
    released_at: str | None = None
    release_reason: str | None = None
    audit: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "_BrakeState":
        return cls(
            version=int(value.get("version", _STATE_VERSION)),
            no_merge_sequence=int(value.get("no_merge_sequence", 0)),
            sequence_event_ids=list(value.get("sequence_event_ids", [])),
            processed_event_ids=list(value.get("processed_event_ids", [])),
            paused=bool(value.get("paused", False)),
            pause_episode=int(value.get("pause_episode", 0)),
            sentinel_episode=int(value.get("sentinel_episode", 0)),
            paused_at=value.get("paused_at"),
            released_at=value.get("released_at"),
            release_reason=value.get("release_reason"),
            audit=list(value.get("audit", [])),
        )


class NoMergeBuildPause:
    """Persisted policy that pauses wasteful new BUILD admissions.

    ``threshold`` is the number of consecutive completed consumptions without
    an own merge that starts a pause. A value must be at least one.

    ``sentinel`` may be a callable or an object exposing ``trigger``,
    ``notify`` or ``alert``. Notification failures do not stop resumptions;
    they are recorded in the audit trail.
    """

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        threshold: int,
        sentinel: Sentinel | SentinelCallback | None = None,
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold must be at least 1")

        self.state_path = Path(state_path)
        self.threshold = threshold
        self.sentinel = sentinel
        self._lock = threading.RLock()
        self._state = self._load()

    @classmethod
    def from_environment(
        cls,
        state_path: str | os.PathLike[str],
        sentinel: Sentinel | SentinelCallback | None = None,
        *,
        variable: str = "ORCHESTRATOR_NO_MERGE_BUILD_PAUSE_THRESHOLD",
        default: int = 3,
    ) -> "NoMergeBuildPause":
        raw_threshold = os.environ.get(variable)
        threshold = default if raw_threshold is None else int(raw_threshold)
        return cls(state_path, threshold, sentinel)

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._state.paused

    @property
    def no_merge_sequence(self) -> int:
        with self._lock:
            return self._state.no_merge_sequence

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result = asdict(self._state)
            result["threshold"] = self.threshold
            return result

    def admission_decision(
        self,
        stage: str,
        *,
        already_started: bool = False,
    ) -> AdmissionDecision:
        """Decide admission without blocking resumptions or downstream work."""

        normalized_stage = stage.strip().upper()
        with self._lock:
            state = self._state
            is_new_build = (
                normalized_stage in _NEW_BUILD_STAGES and not already_started
            )
            if state.paused and is_new_build:
                return AdmissionDecision(
                    admitted=False,
                    reason="new_build_paused_after_no_merge_sequence",
                    paused=True,
                    no_merge_sequence=state.no_merge_sequence,
                    pause_episode=state.pause_episode,
                )

            if state.paused and (
                already_started or normalized_stage in _RESUMABLE_STAGES
            ):
                reason = "existing_work_continues_during_build_pause"
            else:
                reason = "admitted"

            return AdmissionDecision(
                admitted=True,
                reason=reason,
                paused=state.paused,
                no_merge_sequence=state.no_merge_sequence,
                pause_episode=state.pause_episode if state.paused else None,
            )

    def record_consumption(
        self,
        event: ConsumptionCompleted,
    ) -> ConsumptionUpdate:
        """Apply a completed-consumption event exactly once."""

        sentinel_context: dict[str, Any] | None = None

        with self._lock:
            self._reload()
            state = self._state

            if event.event_id in state.processed_event_ids:
                return ConsumptionUpdate(
                    duplicate=True,
                    paused=state.paused,
                    pause_started=False,
                    pause_released=False,
                    sentinel_triggered=False,
                    no_merge_sequence=state.no_merge_sequence,
                    pause_episode=state.pause_episode if state.paused else None,
                )

            state.processed_event_ids.append(event.event_id)
            pause_started = False
            pause_released = False

            if event.produced_own_merge:
                previous_sequence = state.no_merge_sequence
                was_paused = state.paused
                state.no_merge_sequence = 0
                state.sequence_event_ids = []
                state.paused = False
                state.paused_at = None
                state.released_at = event.completed_at
                state.release_reason = "own_merge"
                pause_released = was_paused
                state.audit.append(
                    {
                        "type": "own_merge_sequence_reset",
                        "at": event.completed_at,
                        "event_id": event.event_id,
                        "work_id": event.work_id,
                        "previous_sequence": previous_sequence,
                        "released_pause": was_paused,
                        "episode": state.pause_episode if was_paused else None,
                    }
                )
            else:
                state.no_merge_sequence += 1
                state.sequence_event_ids.append(event.event_id)
                state.audit.append(
                    {
                        "type": "consumption_without_own_merge",
                        "at": event.completed_at,
                        "event_id": event.event_id,
                        "work_id": event.work_id,
                        "stage": event.stage,
                        "provider": event.provider,
                        "model": event.model,
                        "sequence": state.no_merge_sequence,
                    }
                )

                if (
                    not state.paused
                    and state.no_merge_sequence >= self.threshold
                ):
                    state.paused = True
                    state.pause_episode += 1
                    state.paused_at = event.completed_at
                    state.released_at = None
                    state.release_reason = None
                    pause_started = True

                    sentinel_context = {
                        "reason": "consecutive_consumptions_without_own_merge",
                        "pause_episode": state.pause_episode,
                        "sequence": state.no_merge_sequence,
                        "threshold": self.threshold,
                        "sequence_event_ids": list(state.sequence_event_ids),
                        "trigger_event_id": event.event_id,
                        "work_id": event.work_id,
                        "stage": event.stage,
                        "provider": event.provider,
                        "model": event.model,
                        "paused_at": event.completed_at,
                    }

                    # Persist the notification claim before invoking an
                    # external integration. Reprocessing cannot notify twice.
                    state.sentinel_episode = state.pause_episode
                    state.audit.append(
                        {
                            "type": "new_build_pause_started",
                            "at": event.completed_at,
                            **sentinel_context,
                        }
                    )

            self._persist()

        sentinel_triggered = False
        if sentinel_context is not None:
            sentinel_triggered = self._notify_sentinel(sentinel_context)

        return ConsumptionUpdate(
            duplicate=False,
            paused=self.paused,
            pause_started=pause_started,
            pause_released=pause_released,
            sentinel_triggered=sentinel_triggered,
            no_merge_sequence=self.no_merge_sequence,
            pause_episode=(
                self._state.pause_episode if self._state.paused else None
            ),
        )

    def release(
        self,
        reason: str,
        *,
        actor: str = "orchestrator",
        released_at: str | None = None,
    ) -> bool:
        """Explicitly end the current episode and reset its sequence.

        Returns ``True`` only when an active pause was released.
        """

        if not reason.strip():
            raise ValueError("release reason must not be empty")

        with self._lock:
            self._reload()
            state = self._state
            if not state.paused:
                return False

            timestamp = released_at or _utc_now()
            previous_sequence = state.no_merge_sequence
            episode = state.pause_episode
            state.paused = False
            state.no_merge_sequence = 0
            state.sequence_event_ids = []
            state.paused_at = None
            state.released_at = timestamp
            state.release_reason = reason
            state.audit.append(
                {
                    "type": "new_build_pause_released",
                    "at": timestamp,
                    "episode": episode,
                    "actor": actor,
                    "reason": reason,
                    "previous_sequence": previous_sequence,
                }
            )
            self._persist()
            return True

    def _load(self) -> _BrakeState:
        if not self.state_path.exists():
            return _BrakeState()

        with self.state_path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError(
                f"invalid no-merge pause state in {self.state_path}"
            )
        return _BrakeState.from_mapping(value)

    def _reload(self) -> None:
        if self.state_path.exists():
            self._state = self._load()

    def _persist(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.state_path.with_name(
            f".{self.state_path.name}.{os.getpid()}.tmp"
        )
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump(
                asdict(self._state),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                default=str,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, self.state_path)

    def _notify_sentinel(self, context: Mapping[str, Any]) -> bool:
        if self.sentinel is None:
            return False

        try:
            if callable(self.sentinel):
                self.sentinel(context)
            elif hasattr(self.sentinel, "trigger"):
                self.sentinel.trigger(context)
            elif hasattr(self.sentinel, "notify"):
                self.sentinel.notify(context)
            elif hasattr(self.sentinel, "alert"):
                self.sentinel.alert(context)
            else:
                raise TypeError(
                    "sentinel must be callable or expose trigger/notify/alert"
                )
            return True
        except Exception as error:  # Sentinel must not block resumptions.
            with self._lock:
                self._reload()
                self._state.audit.append(
                    {
                        "type": "sentinel_notification_failed",
                        "at": _utc_now(),
                        "episode": context.get("pause_episode"),
                        "error": repr(error),
                    }
                )
                self._persist()
            return False


# Explicit aliases used by orchestration integrations with policy-oriented
# naming.
BuildWasteBrake = NoMergeBuildPause
NoMergePausePolicy = NoMergeBuildPause
