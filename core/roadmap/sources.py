"""Read-only adapters for canonical roadmap sources."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from core.roadmap.models import (
    ConfidenceLevel,
    DeliveryStatus,
    DependencyType,
    LifecycleStage,
    RoadmapCandidate,
    RoadmapDependency,
    RoadmapEvidenceRef,
    RoadmapItemType,
    RoadmapSourceRef,
    RoadmapSourceState,
    PlanningHorizon,
    utc_now,
)


class RoadmapSource(Protocol):
    """Source adapter contract used by library, CLI and HTTP callers."""

    source_id: str
    priority: int

    def read(self, project_id: str) -> "RoadmapSourceResult":
        ...


@dataclass(frozen=True)
class RoadmapSourceResult:
    state: RoadmapSourceState
    records: list[RoadmapCandidate]
    content: str = ""


class JsonRoadmapSource:
    """Load an explicit, versioned roadmap source without executing its text."""

    def __init__(
        self,
        path: Path,
        *,
        source_id: str = "approved-roadmap",
        label: str = "Roadmap operacional aprovado",
        priority: int = 10,
    ) -> None:
        self.path = Path(path)
        self.source_id = source_id
        self.label = label
        self.priority = priority

    def read(self, project_id: str) -> RoadmapSourceResult:
        locator = self.path.as_posix()
        try:
            content = self.path.read_text(encoding="utf-8")
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise ValueError("manifest must be a JSON object")
            manifest_project = payload.get("project_id")
            if manifest_project != project_id:
                raise ValueError(
                    f"source project mismatch: expected {project_id}, got {manifest_project}"
                )
            raw_items = payload.get("items", [])
            if not isinstance(raw_items, list):
                raise ValueError("manifest items must be a list")

            records: list[RoadmapCandidate] = []
            for raw in raw_items:
                if not isinstance(raw, dict):
                    raise ValueError("every roadmap item must be an object")
                source_refs = raw.get("source_refs") or [
                    RoadmapSourceRef(
                        source_id=self.source_id,
                        source_kind="document",
                        label=self.label,
                        locator=locator,
                        revision=content_hash,
                        produced_by="roadmap-manifest",
                    )
                ]
                normalized_raw = dict(raw)
                normalized_raw.update({
                    "source_refs": source_refs,
                    "source_revision": raw.get("source_revision") or content_hash,
                    "observed_at": raw.get("observed_at") or utc_now(),
                    "last_verified_at": raw.get("last_verified_at") or utc_now(),
                })
                candidate = RoadmapCandidate(
                    **normalized_raw,
                    source_id=self.source_id,
                    source_priority=self.priority,
                )
                records.append(candidate)

            state = RoadmapSourceState(
                source_id=self.source_id,
                label=self.label,
                source_kind="document",
                locator=locator,
                revision=content_hash,
                content_hash=content_hash,
            )
            return RoadmapSourceResult(state=state, records=records, content=content)
        except Exception as exc:
            state = RoadmapSourceState(
                source_id=self.source_id,
                label=self.label,
                source_kind="document",
                locator=locator,
                status="unavailable",
                error=str(exc),
            )
            return RoadmapSourceResult(state=state, records=[], content="")


class MarkdownDevelopmentPlanSource:
    """Read the executable DF ticket table from the approved development plan.

    The development plan is intentionally parsed as data. Its rows are
    projected into the same typed contract as the RM manifest, so library,
    CLI, HTTP and Hub consumers see one combined snapshot without duplicating
    the plan in a second hand-maintained JSON file.
    """

    def __init__(
        self,
        path: Path,
        *,
        evidence_dir: Path | None = None,
        source_id: str = "development-plan",
        label: str = "Plano de desenvolvimento DF",
        priority: int = 20,
    ) -> None:
        self.path = Path(path)
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self.source_id = source_id
        self.label = label
        self.priority = priority

    def read(self, project_id: str) -> RoadmapSourceResult:
        locator = self.path.as_posix()
        try:
            content = self.path.read_text(encoding="utf-8")
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            rows = self._parse_rows(content)
            if not rows:
                raise ValueError("development plan contains no DF ticket rows")
            records = [
                self._to_candidate(
                    project_id=project_id,
                    item_id=item_id,
                    scope=scope,
                    dependencies_text=dependencies_text,
                    acceptance=acceptance,
                    content_hash=content_hash,
                )
                for item_id, scope, dependencies_text, acceptance in rows
            ]
            state = RoadmapSourceState(
                source_id=self.source_id,
                label=self.label,
                source_kind="document",
                locator=locator,
                revision=content_hash,
                content_hash=content_hash,
            )
            return RoadmapSourceResult(state=state, records=records, content=content)
        except Exception as exc:
            state = RoadmapSourceState(
                source_id=self.source_id,
                label=self.label,
                source_kind="document",
                locator=locator,
                status="unavailable",
                error=str(exc),
            )
            return RoadmapSourceResult(state=state, records=[], content="")

    @staticmethod
    def _parse_rows(content: str) -> list[tuple[str, str, str, str]]:
        rows: list[tuple[str, str, str, str]] = []
        for line in content.splitlines():
            if not line.strip().startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) != 4 or not re.fullmatch(r"DF-\d{2}", cells[0]):
                continue
            rows.append((cells[0], cells[1], cells[2], cells[3]))
        return rows

    def _to_candidate(
        self,
        *,
        project_id: str,
        item_id: str,
        scope: str,
        dependencies_text: str,
        acceptance: str,
        content_hash: str,
    ) -> RoadmapCandidate:
        criterion = acceptance.strip()
        title = criterion.split(" python ", 1)[0].rstrip(". ")
        evidence_refs = self._evidence_refs(item_id)
        status = DeliveryStatus.COMPLETED if evidence_refs else DeliveryStatus.PLANNED
        rationale = (
            "Relatório de implementação vinculado ao ticket e usado como evidência de conclusão."
            if evidence_refs
            else "O plano registra o ticket, mas não há relatório de implementação vinculado."
        )
        source_ref = RoadmapSourceRef(
            source_id=self.source_id,
            source_kind="document",
            label=f"{self.label} — {item_id}",
            locator=f"{self.path.as_posix()}#{item_id}",
            revision=content_hash,
            produced_by="project-owner",
        )
        return RoadmapCandidate(
            id=item_id,
            project_id=project_id,
            title=f"{item_id} — {title or scope}",
            description=(
                f"Escopo: {scope}\n"
                f"Critério e validação propostos: {criterion}"
            ),
            state_rationale=rationale,
            item_type=RoadmapItemType.FEATURE,
            lifecycle_stage=LifecycleStage.EXECUTION,
            delivery_status=status,
            horizon=PlanningHorizon.NOW,
            confidence=ConfidenceLevel.UNKNOWN,
            dependencies=self._parse_dependencies(dependencies_text),
            tags=["df-ticket", "development-plan"],
            completion_criteria=[criterion],
            source_refs=[source_ref],
            evidence_refs=evidence_refs,
            source_revision=content_hash,
            source_id=self.source_id,
            source_priority=self.priority,
        )

    @staticmethod
    def _parse_dependencies(value: str) -> list[RoadmapDependency]:
        text = value.strip()
        if not text or text in {"—", "-"}:
            return []

        numbers: list[int] = []
        ranges = re.findall(r"(?<!\d)(\d{1,2})\s*[–-]\s*(\d{1,2})(?!\d)", text)
        for start, end in ranges:
            numbers.extend(range(int(start), int(end) + 1))
        remainder = re.sub(r"(?<!\d)\d{1,2}\s*[–-]\s*\d{1,2}(?!\d)", "", text)
        numbers.extend(int(number) for number in re.findall(r"(?<!\d)\d{1,2}(?!\d)", remainder))

        return [
            RoadmapDependency(
                item_id=f"DF-{number:02d}",
                type=DependencyType.REQUIRES,
                label="Dependência declarada no plano de desenvolvimento",
            )
            for number in sorted(set(numbers))
        ]

    def _evidence_refs(self, item_id: str) -> list[RoadmapEvidenceRef]:
        if self.evidence_dir is None:
            return []
        ticket_number = item_id.removeprefix("DF-")
        reports = sorted(self.evidence_dir.glob(f"df-{ticket_number}-*-report.md"))
        return [
            RoadmapEvidenceRef(
                evidence_id=f"report:{item_id}:{report.name}",
                evidence_kind="implementation_report",
                label=f"Relatório de implementação {item_id}",
                locator=report.as_posix(),
                verified=True,
            )
            for report in reports
            if report.is_file()
        ]
