"""Consistency checks for operational roadmap graphs."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from core.roadmap.models import (
    DeliveryStatus,
    DependencyType,
    RoadmapConsistencyIssue,
    RoadmapFlag,
    RoadmapItem,
)


CAUSAL_RELATIONS = {
    DependencyType.REQUIRES,
    DependencyType.BLOCKS,
    DependencyType.UNLOCKS,
    DependencyType.PARENT_OF,
}


class RoadmapConsistencyChecker:
    """Detect inconsistencies and never repair them silently."""

    def check(self, items: Iterable[RoadmapItem]) -> list[RoadmapConsistencyIssue]:
        item_list = list(items)
        known_ids = {item.id for item in item_list}
        issues: list[RoadmapConsistencyIssue] = []

        for item in item_list:
            if not item.source_refs:
                issues.append(RoadmapConsistencyIssue(
                    code="missing_source",
                    severity="error",
                    message=f"Item {item.id} has no navigable source reference.",
                    item_ids=[item.id],
                ))
            if item.delivery_status == DeliveryStatus.COMPLETED:
                if not item.completion_criteria:
                    issues.append(RoadmapConsistencyIssue(
                        code="missing_completion_criteria",
                        severity="error",
                        message=f"Completed item {item.id} has no completion criteria.",
                        item_ids=[item.id],
                    ))
                if not item.evidence_refs:
                    issues.append(RoadmapConsistencyIssue(
                        code="missing_evidence",
                        severity="error",
                        message=f"Completed item {item.id} has no completion evidence.",
                        item_ids=[item.id],
                    ))

            for dependency in item.dependencies:
                if dependency.item_id not in known_ids:
                    issues.append(RoadmapConsistencyIssue(
                        code="orphan_dependency",
                        severity="error",
                        message=(
                            f"Item {item.id} references missing dependency "
                            f"{dependency.item_id}."
                        ),
                        item_ids=[item.id, dependency.item_id],
                    ))

        graph = self._causal_graph(item_list, known_ids)
        for cycle in self._find_cycles(graph):
            issues.append(RoadmapConsistencyIssue(
                code="causal_cycle",
                severity="error",
                message="Causal dependency cycle detected: " + " -> ".join(cycle),
                item_ids=cycle,
            ))
        return issues

    def apply_derived_state(
        self,
        items: Iterable[RoadmapItem],
        issues: Iterable[RoadmapConsistencyIssue],
    ) -> list[RoadmapItem]:
        """Attach non-destructive conflict/blocking indicators to items."""

        item_list = list(items)
        issue_list = list(issues)
        by_id = {item.id: item for item in item_list}
        issue_ids: dict[str, set[str]] = defaultdict(set)
        for issue in issue_list:
            for item_id in issue.item_ids:
                issue_ids[item_id].add(issue.code)

        downstream: dict[str, list[str]] = defaultdict(list)
        for item in item_list:
            for dependency in item.dependencies:
                if dependency.item_id not in by_id:
                    continue
                if dependency.type == DependencyType.RELATED_TO:
                    continue
                downstream[dependency.item_id].append(item.id)

        result: list[RoadmapItem] = []
        for item in item_list:
            flags = list(item.operational_flags)
            codes = issue_ids.get(item.id, set())
            if "conflicting_state" in codes or "causal_cycle" in codes:
                if RoadmapFlag.CONFLICTING not in flags:
                    flags.append(RoadmapFlag.CONFLICTING)
            if "missing_evidence" in codes or "missing_completion_criteria" in codes:
                if RoadmapFlag.AT_RISK not in flags:
                    flags.append(RoadmapFlag.AT_RISK)

            blockers = list(item.blocking_dependency_ids)
            if RoadmapFlag.BLOCKED in flags:
                blockers.extend(
                    dependency.item_id
                    for dependency in item.dependencies
                    if dependency.type == DependencyType.REQUIRES
                    and dependency.item_id in by_id
                    and by_id[dependency.item_id].delivery_status
                    != DeliveryStatus.COMPLETED
                )
            result.append(item.model_copy(update={
                "operational_flags": sorted(set(flags), key=lambda flag: flag.value),
                "blocking_dependency_ids": sorted(set(blockers)),
                "downstream_item_ids": sorted(set(downstream.get(item.id, []))),
            }))
        return result

    @staticmethod
    def _causal_graph(items: list[RoadmapItem], known_ids: set[str]) -> dict[str, set[str]]:
        graph: dict[str, set[str]] = {item.id: set() for item in items}
        for item in items:
            for dependency in item.dependencies:
                if dependency.item_id not in known_ids or dependency.type not in CAUSAL_RELATIONS:
                    continue
                if dependency.type == DependencyType.REQUIRES:
                    graph[dependency.item_id].add(item.id)
                else:
                    graph[item.id].add(dependency.item_id)
        return graph

    @staticmethod
    def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
        cycles: list[list[str]] = []
        visiting: list[str] = []
        active: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in active:
                start = visiting.index(node)
                cycle = visiting[start:] + [node]
                normalized = cycle[:-1]
                if normalized and normalized not in cycles:
                    cycles.append(normalized)
                return
            if node in visited:
                return
            active.add(node)
            visiting.append(node)
            for child in sorted(graph.get(node, set())):
                visit(child)
            visiting.pop()
            active.remove(node)
            visited.add(node)

        for node in sorted(graph):
            visit(node)
        return cycles
