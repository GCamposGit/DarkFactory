"""Durable, atomic model-call ledger used by every DarkFac harness."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from core.usage.models import (
    ModelCallEvent,
    ModelUsageAggregate,
    ModelUsageReport,
)
from core.usage.store import AtomicUsageStore

class ModelUsageLedger:
    """Persist bounded raw events plus recomputable aggregate counters."""

    schema_version = 2

    def __init__(self, storage_dir: Path, max_events: int = 500) -> None:
        self.storage_dir = Path(storage_dir)
        self.ledger_path = self.storage_dir / "model_usage.json"
        self.max_events = max(10, max_events)
        self._store = AtomicUsageStore(self.ledger_path)


    @staticmethod
    def _aggregate_key(event: ModelCallEvent) -> str:
        values = (
            event.provider.lower(),
            event.model,
            event.tier.value,
            event.harness.lower(),
            event.modality.value,
        )
        return "\u241f".join(values)

    def record(self, event: ModelCallEvent) -> None:
        """Record one completed or failed model attempt."""
        with self._store.transaction() as data:
            event_data = event.model_dump(mode="json")
            events: List[Dict[str, Any]] = list(data.get("events", []))
            invocations: Dict[str, Dict[str, Any]] = dict(data.get("invocations", {}))
            if event.invocation_id in invocations:
                return
            events.append(event_data)
            data["events"] = events[-self.max_events :]
            invocations[event.invocation_id] = event_data
            data["invocations"] = invocations

            aggregates: Dict[str, Dict[str, Any]] = dict(data.get("aggregates", {}))
            key = self._aggregate_key(event)
            row = aggregates.get(
                key,
                {
                    "provider": event.provider,
                    "model": event.model,
                    "tier": event.tier.value,
                    "harness": event.harness,
                    "modality": event.modality.value,
                    "call_count": 0,
                    "success_count": 0,
                    "failure_count": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
                    "latency_ms": 0.0,
                    "first_seen_at": event.timestamp,
                    "last_seen_at": event.timestamp,
                },
            )
            row["call_count"] += 1
            row["success_count" if event.success else "failure_count"] += 1
            row["input_tokens"] += event.input_tokens or 0
            row["output_tokens"] += event.output_tokens or 0
            row["cost_usd"] = round(float(row["cost_usd"]) + (event.cost_usd or 0.0), 8)
            row["latency_ms"] = round(float(row["latency_ms"]) + (event.latency_ms or 0.0), 3)
            row["last_seen_at"] = event.timestamp
            aggregates[key] = row
            data["aggregates"] = aggregates

        # Mirror event to SQLite telemetry store
        try:
            from core.telemetry.models import ExecutionMode, TelemetryRecordCreate
            from core.telemetry.store import TelemetryStore

            db_path = self.storage_dir.parent / "telemetry.db"
            store = TelemetryStore(db_path)
            mode = ExecutionMode.HEADLESS
            if event.execution_mode:
                try:
                    mode = ExecutionMode(event.execution_mode.lower())
                except ValueError:
                    mode = ExecutionMode.HEADLESS

            store.record(
                TelemetryRecordCreate(
                    id=event.invocation_id,
                    timestamp=event.timestamp,
                    ticket_id=event.ticket_id,
                    provider=event.provider,
                    model=event.model,
                    tier=event.tier.value if hasattr(event.tier, "value") else str(event.tier),
                    harness=event.harness,
                    execution_mode=mode,
                    input_tokens=event.input_tokens or 0,
                    processing_tokens=event.processing_tokens or 0,
                    output_tokens=event.output_tokens or 0,
                    latency_ms=event.latency_ms or 0.0,
                    cost_usd=event.cost_usd or 0.0,
                    success=event.success,
                )
            )
        except Exception:
            pass

    def recompute_aggregates(self) -> int:
        """Rebuild lifetime aggregates from the durable invocation evidence."""
        with self._store.transaction() as data:
            invocations = dict(data.get("invocations", {}))
            aggregates: Dict[str, Dict[str, Any]] = {}
            for event_data in invocations.values():
                event = ModelCallEvent.model_validate(event_data)
                key = self._aggregate_key(event)
                row = aggregates.get(
                    key,
                    {
                        "provider": event.provider,
                        "model": event.model,
                        "tier": event.tier.value,
                        "harness": event.harness,
                        "modality": event.modality.value,
                        "call_count": 0,
                        "success_count": 0,
                        "failure_count": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_usd": 0.0,
                        "latency_ms": 0.0,
                        "first_seen_at": event.timestamp,
                        "last_seen_at": event.timestamp,
                    },
                )
                row["call_count"] += 1
                row["success_count" if event.success else "failure_count"] += 1
                row["input_tokens"] += event.input_tokens or 0
                row["output_tokens"] += event.output_tokens or 0
                row["cost_usd"] = round(
                    float(row["cost_usd"]) + (event.cost_usd or 0.0),
                    8,
                )
                row["latency_ms"] = round(
                    float(row["latency_ms"]) + (event.latency_ms or 0.0),
                    3,
                )
                row["last_seen_at"] = event.timestamp
                aggregates[key] = row
            data["aggregates"] = aggregates
            return len(invocations)
    def report(self, recent_limit: int = 25, project_id: Optional[str] = None) -> ModelUsageReport:
        """Return stable aggregates sorted by call count and recency."""
        data = self._store.read()

        if project_id:
            invocations = dict(data.get("invocations", {}))
            filtered_aggregates: Dict[str, Dict[str, Any]] = {}
            for event_data in invocations.values():
                event_proj = event_data.get("project_id", "darkfac")
                if event_proj != project_id:
                    continue
                event = ModelCallEvent.model_validate(event_data)
                key = self._aggregate_key(event)
                row = filtered_aggregates.get(
                    key,
                    {
                        "provider": event.provider,
                        "model": event.model,
                        "tier": event.tier.value,
                        "harness": event.harness,
                        "modality": event.modality.value,
                        "call_count": 0,
                        "success_count": 0,
                        "failure_count": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_usd": 0.0,
                        "latency_ms": 0.0,
                        "first_seen_at": event.timestamp,
                        "last_seen_at": event.timestamp,
                    },
                )
                row["call_count"] += 1
                row["success_count" if event.success else "failure_count"] += 1
                row["input_tokens"] += event.input_tokens or 0
                row["output_tokens"] += event.output_tokens or 0
                row["cost_usd"] = round(float(row["cost_usd"]) + (event.cost_usd or 0.0), 8)
                row["latency_ms"] = round(float(row["latency_ms"]) + (event.latency_ms or 0.0), 3)
                row["last_seen_at"] = event.timestamp
                filtered_aggregates[key] = row

            aggregates = [ModelUsageAggregate.model_validate(row) for row in filtered_aggregates.values()]
            raw_events = [e for e in data.get("events", []) if e.get("project_id", "darkfac") == project_id][-max(0, recent_limit):]
        else:
            aggregates = [ModelUsageAggregate.model_validate(row) for row in data.get("aggregates", {}).values()]
            raw_events = list(data.get("events", []))[-max(0, recent_limit):]

        aggregates.sort(key=lambda row: (-row.call_count, row.provider.lower(), row.model.lower()))
        recent_events = [ModelCallEvent.model_validate(row) for row in reversed(raw_events)]
        total_calls = sum(row.call_count for row in aggregates)
        successful = sum(row.success_count for row in aggregates)
        failed = sum(row.failure_count for row in aggregates)
        tokens = sum(row.input_tokens + row.output_tokens for row in aggregates)
        cost = round(sum(row.cost_usd for row in aggregates), 8)
        return ModelUsageReport(
            total_calls=total_calls,
            successful_calls=successful,
            failed_calls=failed,
            total_tokens=tokens,
            total_cost_usd=cost,
            aggregates=aggregates,
            recent_events=recent_events,
        )


def infer_model_tier(provider: str, model: str) -> str:
    """Infer a conservative tier for call sites that do not declare one."""
    provider_lower = provider.lower()
    model_lower = model.lower()
    if provider_lower in {"ollama", "local", "local_procedural"}:
        return "procedural" if "procedural" in provider_lower or "vector" in model_lower else "local"
    if any(token in model_lower for token in ("astra", "pro", "opus", "grok-4", "r1")):
        return "frontier"
    if provider_lower in {"openrouter", "openai", "google", "xai", "anthropic", "siliconflow"}:
        return "tier_2"
    return "unknown"
