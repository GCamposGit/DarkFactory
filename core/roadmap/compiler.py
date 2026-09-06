"""Compilation of canonical roadmap sources into deterministic snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Iterable

from core.roadmap.checker import RoadmapConsistencyChecker
from core.roadmap.models import (
    ConfirmationState,
    RoadmapCandidate,
    RoadmapConsistencyIssue,
    RoadmapItem,
    RoadmapSnapshot,
    RoadmapStats,
    RoadmapSourceState,
    model_sort_key,
    utc_now,
)
from core.roadmap.sources import RoadmapSource


class RoadmapCompiler:
    """Normalize identity, resolve precedence and validate a roadmap graph."""

    def __init__(self, sources: Iterable[RoadmapSource]) -> None:
        self.sources = tuple(sorted(sources, key=lambda source: (source.priority, source.source_id)))
        self.checker = RoadmapConsistencyChecker()

    def compile(self, project_id: str) -> RoadmapSnapshot:
        source_states: list[RoadmapSourceState] = []
        source_records: list[RoadmapCandidate] = []
        for source in self.sources:
            result = source.read(project_id)
            source_states.append(result.state)
            source_records.extend(
                record for record in result.records if record.project_id == project_id
            )

        grouped: dict[str, list[RoadmapCandidate]] = defaultdict(list)
        for record in source_records:
            grouped[self._resolve_identity(record)].append(record)

        items: list[RoadmapItem] = []
        conflict_issues: list[RoadmapConsistencyIssue] = []
        for item_id in sorted(grouped):
            records = sorted(grouped[item_id], key=lambda record: (record.source_priority, record.source_id))
            selected = records[0]
            merged = self._merge_records(item_id, records)
            if len(records) > 1 and self._records_conflict(records):
                conflict_issues.append(RoadmapConsistencyIssue(
                    code="conflicting_state",
                    severity="error",
                    message=(
                        f"Sources disagree about the operational state of {item_id}; "
                        "the highest-precedence value is shown with a conflict flag."
                    ),
                    item_ids=[item_id],
                    source_ids=sorted({record.source_id for record in records}),
                ))
            items.append(merged.model_copy(update={
                "source_revision": selected.source_revision,
                # A record can only come from an available source. Other
                # unavailable sources make the snapshot partial, but must not
                # incorrectly mark this record's own source as unavailable.
                "freshness_status": selected.freshness_status,
            }))

        items.sort(key=model_sort_key)
        issues = conflict_issues + self.checker.check(items)
        items = self.checker.apply_derived_state(items, issues)
        items = [
            item.model_copy(update={
                "confirmation_state": (
                    ConfirmationState.CONFIRMED
                    if item.source_refs and not any(
                        issue.code == "missing_source" and item.id in issue.item_ids
                        for issue in issues
                    )
                    else ConfirmationState.UNCONFIRMED
                )
            })
            for item in items
        ]
        stats = self._stats(items)
        source_fingerprint = self._source_fingerprint(source_states)
        snapshot_hash = self._snapshot_hash(project_id, items, issues, source_fingerprint)
        return RoadmapSnapshot(
            snapshot_id=f"{project_id}-{snapshot_hash[:16]}",
            snapshot_hash=snapshot_hash,
            project_id=project_id,
            observed_at=utc_now(),
            items=items,
            stats=stats,
            sources_consulted=source_states,
            sources_unavailable=sorted(
                state.source_id for state in source_states if state.status == "unavailable"
            ),
            issues=issues,
            derivation_policy=(
                "Explicit source precedence (lower number wins); identity uses source id "
                "or a normalized project-scoped title; conflicts remain visible; causal "
                "relations exclude related_to from blocker and cycle analysis."
            ),
            source_fingerprint=source_fingerprint,
        )

    @staticmethod
    def _resolve_identity(record: RoadmapCandidate) -> str:
        if record.id.strip():
            return record.id.strip()
        slug = re.sub(r"[^a-z0-9]+", "-", record.title.lower()).strip("-") or "item"
        return f"{record.project_id}:{slug}"

    @staticmethod
    def _merge_records(item_id: str, records: list[RoadmapCandidate]) -> RoadmapItem:
        selected = records[0]
        source_refs = []
        evidence_refs = []
        for record in records:
            source_refs.extend(record.source_refs)
            evidence_refs.extend(record.evidence_refs)
        return selected.model_copy(update={
            "id": item_id,
            "source_refs": RoadmapCompiler._dedupe_models(source_refs),
            "evidence_refs": RoadmapCompiler._dedupe_models(evidence_refs),
        })

    @staticmethod
    def _records_conflict(records: list[RoadmapCandidate]) -> bool:
        fields = (
            "title", "description", "state_rationale", "item_type", "lifecycle_stage", "delivery_status",
            "horizon", "confidence", "parent_id", "planned_start", "target_date",
            "dependencies", "completion_criteria",
        )
        first = records[0]
        for record in records[1:]:
            if any(getattr(first, field) != getattr(record, field) for field in fields):
                return True
        return False

    @staticmethod
    def _dedupe_models(values: list) -> list:
        seen: set[str] = set()
        result = []
        for value in values:
            key = json.dumps(value.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                result.append(value)
        return result

    @staticmethod
    def _source_fingerprint(states: list[RoadmapSourceState]) -> str:
        payload = [
            {
                "source_id": state.source_id,
                "status": state.status,
                "revision": state.revision,
                "content_hash": state.content_hash,
                "error": state.error,
            }
            for state in sorted(states, key=lambda state: state.source_id)
        ]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _snapshot_hash(
        project_id: str,
        items: list[RoadmapItem],
        issues: list[RoadmapConsistencyIssue],
        source_fingerprint: str,
    ) -> str:
        payload = {
            "project_id": project_id,
            "items": [
                item.model_dump(mode="json", exclude={"observed_at", "last_verified_at"})
                for item in items
            ],
            "issues": [issue.model_dump(mode="json") for issue in issues],
            "source_fingerprint": source_fingerprint,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _stats(items: list[RoadmapItem]) -> RoadmapStats:
        return RoadmapStats(
            total_items=len(items),
            confirmed_items=sum(item.confirmation_state == ConfirmationState.CONFIRMED for item in items),
            blocked_items=sum(RoadmapCompiler._has_flag(item, "blocked") for item in items),
            conflict_items=sum(RoadmapCompiler._has_flag(item, "conflicting") for item in items),
            stale_items=sum(item.freshness_status.value in {"aging", "stale", "unavailable"} for item in items),
            by_horizon=RoadmapCompiler._count_values(item.horizon.value for item in items),
            by_status=RoadmapCompiler._count_values(item.delivery_status.value for item in items),
        )

    @staticmethod
    def _has_flag(item: RoadmapItem, flag: str) -> bool:
        return any(value.value == flag for value in item.operational_flags)

    @staticmethod
    def _count_values(values: Iterable[str]) -> dict[str, int]:
        result: dict[str, int] = {}
        for value in values:
            result[value] = result.get(value, 0) + 1
        return dict(sorted(result.items()))
