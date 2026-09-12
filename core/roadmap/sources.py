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
        evidence_dir: Path | None = None,
        source_id: str = "approved-roadmap",
        label: str = "Roadmap operacional aprovado",
        priority: int = 10,
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
            evidence_files = self._evidence_files()
            content_hash = hashlib.sha256(
                json.dumps(
                    {
                        "manifest": content,
                        "evidence": [
                            {
                                "name": report.name,
                                "hash": hashlib.sha256(
                                    report.read_bytes()
                                ).hexdigest(),
                            }
                            for report in evidence_files
                        ],
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
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
                evidence_refs = list(raw.get("evidence_refs") or [])
                evidence_refs.extend(self._evidence_refs(str(raw.get("id", ""))))
                if evidence_refs:
                    normalized_raw["evidence_refs"] = evidence_refs
                    if normalized_raw.get("delivery_status") == DeliveryStatus.PLANNED.value:
                        normalized_raw["delivery_status"] = DeliveryStatus.COMPLETED.value
                        normalized_raw["state_rationale"] = (
                            "Relatório de implementação vinculado ao ticket e usado como evidência de conclusão."
                        )
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

    def _evidence_files(self) -> list[Path]:
        if self.evidence_dir is None or not self.evidence_dir.exists():
            return []
        return sorted(
            report
            for report in self.evidence_dir.glob("*-report.md")
            if report.is_file()
        )

    def _evidence_refs(self, item_id: str) -> list[RoadmapEvidenceRef]:
        if not item_id or self.evidence_dir is None:
            return []
        slug = item_id.lower()
        reports = list(self.evidence_dir.glob(f"{slug}-*-report.md"))
        if item_id in {f"RM-{number:02d}" for number in range(1, 8)}:
            operational_report = self.evidence_dir / "roadmap-operacional-report.md"
            if operational_report.is_file():
                reports.append(operational_report)
        return [
            RoadmapEvidenceRef(
                evidence_id=f"report:{item_id}:{report.name}",
                evidence_kind="implementation_report",
                label=f"Relatório de implementação {item_id}",
                locator=report.as_posix(),
                verified=True,
            )
            for report in sorted(reports)
            if report.is_file()
        ]


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


class UserDemandsRoadmapSource:
    """Roadmap source adapter for user-submitted demand tickets."""

    def __init__(
        self,
        path: Path,
        *,
        evidence_dir: Path | None = None,
        source_id: str = "user-demands",
        label: str = "Demandas de Usuários",
        priority: int = 15,
    ) -> None:
        self.path = Path(path)
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self.source_id = source_id
        self.label = label
        self.priority = priority

    def read(self, project_id: str) -> RoadmapSourceResult:
        locator = self.path.as_posix()
        if not self.path.exists():
            state = RoadmapSourceState(
                source_id=self.source_id,
                label=self.label,
                source_kind="document",
                locator=locator,
                revision="",
                content_hash="",
            )
            return RoadmapSourceResult(state=state, records=[], content="")

        try:
            content = self.path.read_text(encoding="utf-8")
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            raw_data = json.loads(content) if content.strip() else []
            items_list = raw_data if isinstance(raw_data, list) else raw_data.get("demands", [])

            records: list[RoadmapCandidate] = []
            for raw in items_list:
                if not isinstance(raw, dict):
                    continue
                item_project = raw.get("project_id", "darkfac")
                if item_project != project_id:
                    continue

                item_id = raw.get("id", "")
                title = raw.get("title", "")
                problem = raw.get("problem_statement", "")
                journey = raw.get("core_journey", [])
                non_goals = raw.get("non_goals", [])
                criteria = raw.get("acceptance_criteria", [])
                raw_tags = raw.get("tags", [])
                tags = list(raw_tags) if isinstance(raw_tags, list) else []
                if "user-demand" not in tags:
                    tags.insert(0, "user-demand")

                journey_str = " -> ".join(journey) if isinstance(journey, list) else str(journey)
                non_goals_str = "\n".join(f"- {ng}" for ng in non_goals) if isinstance(non_goals, list) else str(non_goals)
                description = (
                    f"Problema: {problem}\n\n"
                    f"Jornada Principal: {journey_str}\n\n"
                    f"Non-Goals:\n{non_goals_str}"
                ).strip()

                raw_status = raw.get("status", "planned")
                try:
                    status = DeliveryStatus(raw_status)
                except ValueError:
                    status = DeliveryStatus.PLANNED

                raw_type = raw.get("item_type", "feature")
                try:
                    item_type = RoadmapItemType(raw_type)
                except ValueError:
                    item_type = RoadmapItemType.FEATURE

                raw_stage = raw.get("lifecycle_stage", "execution")
                try:
                    stage = LifecycleStage(raw_stage)
                except ValueError:
                    stage = LifecycleStage.EXECUTION

                raw_horizon = raw.get("horizon", "now")
                try:
                    horizon = PlanningHorizon(raw_horizon)
                except ValueError:
                    horizon = PlanningHorizon.NOW

                deps = [
                    RoadmapDependency(
                        item_id=dep_id,
                        type=DependencyType.REQUIRES,
                        label="Dependência da demanda",
                    )
                    for dep_id in raw.get("dependencies", [])
                    if isinstance(dep_id, str) and dep_id.strip()
                ]

                evidence_refs = self._evidence_refs(item_id)
                if evidence_refs and status == DeliveryStatus.PLANNED:
                    status = DeliveryStatus.COMPLETED

                source_ref = RoadmapSourceRef(
                    source_id=self.source_id,
                    source_kind="document",
                    label=f"{self.label} — {item_id}",
                    locator=f"{locator}#{item_id}",
                    revision=content_hash,
                    produced_by="user",
                )

                candidate = RoadmapCandidate(
                    id=item_id,
                    project_id=project_id,
                    title=f"{item_id} — {title}",
                    description=description,
                    state_rationale=f"Demanda de usuário registrada no backlog com status {status.value}.",
                    item_type=item_type,
                    lifecycle_stage=stage,
                    delivery_status=status,
                    horizon=horizon,
                    confidence=ConfidenceLevel.HIGH,
                    dependencies=deps,
                    tags=tags,
                    completion_criteria=criteria if isinstance(criteria, list) else [],
                    source_refs=[source_ref],
                    evidence_refs=evidence_refs,
                    source_revision=content_hash,
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

    def _evidence_refs(self, item_id: str) -> list[RoadmapEvidenceRef]:
        if self.evidence_dir is None:
            return []
        slug = item_id.lower()
        reports = sorted(self.evidence_dir.glob(f"{slug}-*-report.md"))
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


class HybridWorkflowPlanSource:
    """Roadmap source adapter for hybrid autonomy workflow plan (HF tickets)."""

    def __init__(
        self,
        path: Path,
        *,
        evidence_dir: Path | None = None,
        source_id: str = "hybrid-workflow-plan-wave-1",
        label: str = "Plano de Autonomia Híbrida HF",
        priority: int = 30,
    ) -> None:
        self.path = Path(path)
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self.source_id = source_id
        self.label = label
        self.priority = priority

    def read(self, project_id: str) -> RoadmapSourceResult:
        locator = self.path.as_posix()
        if not self.path.exists():
            state = RoadmapSourceState(
                source_id=self.source_id,
                label=self.label,
                source_kind="document",
                locator=locator,
                status="unavailable",
                error="file not found",
            )
            return RoadmapSourceResult(state=state, records=[], content="")

        try:
            content = self.path.read_text(encoding="utf-8")
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

            wave1_rows = self._parse_wave1_rows(content)
            wave2_rows = self._parse_wave2_rows(content)

            records: list[RoadmapCandidate] = []
            for item_id, title, deps_text, reuse, criterion in wave1_rows:
                records.append(
                    self._to_candidate(
                        project_id=project_id,
                        item_id=item_id,
                        title=title,
                        dependencies_text=deps_text,
                        criterion=criterion,
                        wave=1,
                        reuse_text=reuse,
                        content_hash=content_hash,
                    )
                )

            for item_id, title, complement, value_crit in wave2_rows:
                records.append(
                    self._to_candidate(
                        project_id=project_id,
                        item_id=item_id,
                        title=title,
                        dependencies_text="HF-15",
                        criterion=value_crit,
                        wave=2,
                        reuse_text=complement,
                        content_hash=content_hash,
                    )
                )

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
    def _parse_wave1_rows(content: str) -> list[tuple[str, str, str, str, str]]:
        rows: list[tuple[str, str, str, str, str]] = []
        in_wave1 = False
        for line in content.splitlines():
            line_str = line.strip()
            if "### Onda 1" in line_str:
                in_wave1 = True
                continue
            if in_wave1 and line_str.startswith("### "):
                break
            if not in_wave1 or not line_str.startswith("|"):
                continue
            cells = [c.strip() for c in line_str.strip("|").split("|")]
            if len(cells) >= 5 and re.fullmatch(r"HF-\d{2}", cells[0]):
                rows.append((cells[0], cells[1], cells[2], cells[3], cells[4]))
        return rows

    @staticmethod
    def _parse_wave2_rows(content: str) -> list[tuple[str, str, str, str]]:
        rows: list[tuple[str, str, str, str]] = []
        in_wave2 = False
        for line in content.splitlines():
            line_str = line.strip()
            if "### Onda 2" in line_str:
                in_wave2 = True
                continue
            if in_wave2 and line_str.startswith("## ") and "Onda 2" not in line_str:
                break
            if not in_wave2 or not line_str.startswith("|"):
                continue
            cells = [c.strip() for c in line_str.strip("|").split("|")]
            if len(cells) >= 4 and re.fullmatch(r"HF-\d{2}", cells[0]):
                rows.append((cells[0], cells[1], cells[2], cells[3]))
        return rows

    def _to_candidate(
        self,
        *,
        project_id: str,
        item_id: str,
        title: str,
        dependencies_text: str,
        criterion: str,
        wave: int,
        reuse_text: str,
        content_hash: str,
    ) -> RoadmapCandidate:
        evidence_refs = self._evidence_refs(item_id)
        status = (
            DeliveryStatus.COMPLETED
            if evidence_refs or criterion.strip().lower().startswith("concluído")
            else DeliveryStatus.PLANNED
        )
        rationale = (
            "Relatório de implementação vinculado e verificado no repositório."
            if evidence_refs
            else "Ticket planejado no workflow híbrido de autonomia."
        )
        stage = LifecycleStage.EXECUTION if wave == 1 else LifecycleStage.FUTURE
        horizon = PlanningHorizon.NOW if wave == 1 else PlanningHorizon.LATER
        deps = self._parse_dependencies(dependencies_text)
        source_ref = RoadmapSourceRef(
            source_id=self.source_id,
            source_kind="document",
            label=f"{self.label} — {item_id}",
            locator=f"{self.path.as_posix()}#{item_id}",
            revision=content_hash,
            produced_by="project-owner",
        )
        description = (
            f"Entrega: {title}\n"
            f"Critério / Valor: {criterion}\n"
            f"Reúso / Complemento: {reuse_text}"
        )
        clean_title = title.split(";", 1)[0].split(".", 1)[0].strip()
        return RoadmapCandidate(
            id=item_id,
            project_id=project_id,
            title=f"{item_id} — {clean_title}",
            description=description,
            state_rationale=rationale,
            item_type=RoadmapItemType.FEATURE,
            lifecycle_stage=stage,
            delivery_status=status,
            horizon=horizon,
            confidence=ConfidenceLevel.HIGH,
            dependencies=deps,
            tags=["hf-ticket", f"wave-{wave}", "hybrid-autonomy"],
            completion_criteria=[criterion] if criterion else [],
            source_refs=[source_ref],
            evidence_refs=evidence_refs,
            source_revision=content_hash,
            source_id=self.source_id,
            source_priority=self.priority,
        )

    @staticmethod
    def _parse_dependencies(text: str) -> list[RoadmapDependency]:
        cleaned = text.strip()
        if not cleaned or cleaned in {"—", "-"}:
            return []
        found_ids = sorted(set(re.findall(r"(?:HF|DF|INFRA|RM)-\d{2}", cleaned)))
        return [
            RoadmapDependency(
                item_id=dep_id,
                type=DependencyType.REQUIRES,
                label=f"Dependência {dep_id} declarada no plano híbrido",
            )
            for dep_id in found_ids
        ]

    def _evidence_refs(self, item_id: str) -> list[RoadmapEvidenceRef]:
        if self.evidence_dir is None or not self.evidence_dir.exists():
            return []
        slug = item_id.lower()
        reports = sorted(self.evidence_dir.glob(f"{slug}*-report.md"))
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


class InfraRoadmapJsonSource:
    """Roadmap source adapter for infrastructure roadmap (INFRA tickets)."""

    def __init__(
        self,
        path: Path,
        *,
        evidence_dir: Path | None = None,
        source_id: str = "infra-roadmap-json",
        label: str = "Roadmap de Infraestrutura",
        priority: int = 25,
    ) -> None:
        self.path = Path(path)
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self.source_id = source_id
        self.label = label
        self.priority = priority

    def read(self, project_id: str) -> RoadmapSourceResult:
        locator = self.path.as_posix()
        if not self.path.exists():
            state = RoadmapSourceState(
                source_id=self.source_id,
                label=self.label,
                source_kind="document",
                locator=locator,
                status="unavailable",
                error="file not found",
            )
            return RoadmapSourceResult(state=state, records=[], content="")

        try:
            content = self.path.read_text(encoding="utf-8")
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            raw_data = json.loads(content)
            items_list = raw_data.get("items", []) if isinstance(raw_data, dict) else []

            prereqs_map = {
                "INFRA-06": ["INFRA-05"],
                "INFRA-06B": ["INFRA-06"],
                "INFRA-07": ["INFRA-06"],
                "INFRA-08": ["INFRA-07"],
                "INFRA-09": ["INFRA-06"],
                "INFRA-10": ["INFRA-03"],
                "INFRA-11": ["INFRA-01"],
            }

            records: list[RoadmapCandidate] = []
            for raw in items_list:
                if not isinstance(raw, dict):
                    continue
                item_id = str(raw.get("id", "")).strip()
                if not item_id:
                    continue

                title = str(raw.get("title", item_id))
                raw_status = str(raw.get("status", "planned")).strip().lower()
                status = (
                    DeliveryStatus.COMPLETED
                    if raw_status in ("delivered", "completed")
                    else DeliveryStatus.PLANNED
                )
                phase = str(raw.get("phase", "phase-1"))
                stage = (
                    LifecycleStage.FOUNDATIONS
                    if phase in ("phase-1", "phase-2")
                    else LifecycleStage.EXECUTION
                )
                horizon = (
                    PlanningHorizon.NOW
                    if phase in ("phase-1", "phase-2")
                    else PlanningHorizon.NEXT
                )
                raw_tags = raw.get("tags", [])
                tags = list(raw_tags) if isinstance(raw_tags, list) else []
                if "infrastructure" not in tags:
                    tags.insert(0, "infrastructure")

                deps = [
                    RoadmapDependency(
                        item_id=dep_id,
                        type=DependencyType.REQUIRES,
                        label=f"Pré-requisito {dep_id} de infraestrutura",
                    )
                    for dep_id in prereqs_map.get(item_id, [])
                ]

                evidence_refs = self._evidence_refs(item_id)
                source_ref = RoadmapSourceRef(
                    source_id=self.source_id,
                    source_kind="document",
                    label=f"{self.label} — {item_id}",
                    locator=f"{locator}#{item_id}",
                    revision=content_hash,
                    produced_by="infra-architect",
                )

                candidate = RoadmapCandidate(
                    id=item_id,
                    project_id=project_id,
                    title=f"{item_id} — {title}",
                    description=str(raw.get("description", "")),
                    state_rationale=f"Item de infraestrutura ({phase}) com status {status.value}.",
                    item_type=RoadmapItemType.INFRASTRUCTURE,
                    lifecycle_stage=stage,
                    delivery_status=status,
                    horizon=horizon,
                    confidence=ConfidenceLevel.HIGH,
                    dependencies=deps,
                    tags=tags,
                    completion_criteria=[str(raw.get("description", ""))] if raw.get("description") else [],
                    source_refs=[source_ref],
                    evidence_refs=evidence_refs,
                    source_revision=content_hash,
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

    def _evidence_refs(self, item_id: str) -> list[RoadmapEvidenceRef]:
        if self.evidence_dir is None or not self.evidence_dir.exists():
            return []
        slug = item_id.lower()
        reports = sorted(self.evidence_dir.glob(f"{slug}*-report.md"))
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
