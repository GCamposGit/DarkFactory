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


def is_report_for_ticket(report_name: str, ticket_id: str) -> bool:
    """Deterministic check if a report file belongs to a ticket, preventing parent-child false positives.

    For example, 'hf-05-02-runtime-report.md' belongs to 'HF-05-02', and MUST NEVER match parent 'HF-05'.
    """
    slug = ticket_id.lower().strip()
    name = report_name.lower().strip()
    if not (name.endswith("-report.md") or name.endswith(".md")):
        return False
    if name == f"{slug}-report.md":
        return True
    prefix = f"{slug}-"
    if not name.startswith(prefix):
        return False
    remainder = name[len(prefix):]
    # If the remainder immediately starts with digits followed by '-', '_', or '.',
    # it belongs to a child sub-ticket (e.g. hf-05-02 belongs to HF-05-02, not HF-05).
    if re.match(r"^\d{1,3}(?:-|_|\.|$)", remainder):
        return False
    return True


class JsonRoadmapSource:
    """Load an explicit, versioned roadmap source without executing its text."""

    def __init__(
        self,
        path: Path,
        *,
        evidence_dir: Path | None = None,
        plan_path: Path | None = None,
        receipts: list[Any] | None = None,
        receipts_dir: Path | None = None,
        source_id: str = "approved-roadmap",
        label: str = "Roadmap operacional aprovado",
        priority: int = 10,
    ) -> None:
        self.path = Path(path)
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self.plan_path = Path(plan_path) if plan_path else None
        self.receipts = list(receipts) if receipts is not None else []
        self.receipts_dir = Path(receipts_dir) if receipts_dir else None
        self.source_id = source_id
        self.label = label
        self.priority = priority

    def read(self, project_id: str) -> RoadmapSourceResult:
        locator = self.path.as_posix()
        try:
            content = self.path.read_text(encoding="utf-8")
            evidence_files = self._evidence_files()
            plan_hash = ""
            plan_units: dict[str, dict[str, Any]] = {}
            if self.plan_path and self.plan_path.exists():
                try:
                    plan_content = self.plan_path.read_text(encoding="utf-8")
                    plan_hash = hashlib.sha256(plan_content.encode("utf-8")).hexdigest()
                    plan_data = json.loads(plan_content)
                    if isinstance(plan_data, dict):
                        for u in plan_data.get("units", []):
                            if isinstance(u, dict) and "ticket_id" in u:
                                plan_units[str(u["ticket_id"]).strip()] = u
                except Exception:
                    pass

            content_hash = hashlib.sha256(
                json.dumps(
                    {
                        "manifest": content,
                        "plan_hash": plan_hash,
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
                item_id = str(raw.get("id", "")).strip()

                if item_id in plan_units:
                    unit = plan_units[item_id]
                    if unit.get("planning_status"):
                        normalized_raw["planning_status"] = str(unit["planning_status"])
                    if unit.get("implementation_status"):
                        normalized_raw["implementation_status"] = str(unit["implementation_status"])
                    if unit.get("operational_status"):
                        normalized_raw["operational_status"] = str(unit["operational_status"])

                evidence_refs = [
                    RoadmapEvidenceRef.model_validate(ref) if isinstance(ref, dict) else ref
                    for ref in raw.get("evidence_refs") or []
                ]
                evidence_refs.extend(self._evidence_refs(item_id))
                if evidence_refs:
                    normalized_raw["evidence_refs"] = evidence_refs
                    has_verified = any(ref.verified for ref in evidence_refs)
                    if normalized_raw.get("delivery_status") == DeliveryStatus.PLANNED.value and has_verified:
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
        if not item_id:
            return []
        refs: list[RoadmapEvidenceRef] = []
        for r in self.receipts:
            if hasattr(r, "subject") and r.subject == item_id:
                res = getattr(r, "result", None)
                is_passed = str(res) in ("passed", "EvidenceResult.PASSED") or (hasattr(res, "value") and res.value == "passed")
                is_verified = getattr(r, "verified", True) and is_passed
                receipt_id = getattr(r, "receipt_id", f"receipt-{item_id}")
                refs.append(
                    RoadmapEvidenceRef(
                        evidence_id=f"receipt:{item_id}:{receipt_id}",
                        evidence_kind="verified_receipt",
                        label=f"Recibo verificado {item_id}",
                        locator=getattr(r, "locator", f"receipt://{receipt_id}"),
                        observed_at=getattr(r, "observed_at", None),
                        verified=is_verified,
                    )
                )

        if self.evidence_dir is None or not self.evidence_dir.exists():
            return refs

        reports: list[Path] = [
            report for report in self._evidence_files()
            if is_report_for_ticket(report.name, item_id)
        ]
        if item_id in {f"RM-{number:02d}" for number in range(1, 8)}:
            operational_report = self.evidence_dir / "roadmap-operacional-report.md"
            if operational_report.is_file() and operational_report not in reports:
                reports.append(operational_report)

        is_hf = item_id.startswith("HF-")
        for report in sorted(reports):
            if is_hf:
                refs.append(
                    RoadmapEvidenceRef(
                        evidence_id=f"doc:{item_id}:{report.name}",
                        evidence_kind="documentary_unverified",
                        label=f"Relatório documental não verificado {item_id}",
                        locator=report.as_posix(),
                        verified=False,
                    )
                )
            else:
                refs.append(
                    RoadmapEvidenceRef(
                        evidence_id=f"report:{item_id}:{report.name}",
                        evidence_kind="implementation_report",
                        label=f"Relatório de implementação {item_id}",
                        locator=report.as_posix(),
                        verified=True,
                    )
                )
        return refs


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


class ContinuousAutonomyPlanSource:
    """Roadmap source adapter for continuous autonomy plan (plan.json).

    - Chaveamento por ID completo (ex: HF-13-01 é tratado por seu ID completo e não truncado ou confundido com pai HF-13).
    - Relatórios markdown globbed em .factory/reports/ (ex: slug-*-report.md) DEVEM ser rotulados como documentais
      não verificados (evidence_kind="documentary_unverified" e verified=False), NUNCA conferindo status completed.
    - Status de conclusão (completed) requer obrigatoriamente um recibo vinculado e confiável (EvidenceReceipt / verified=True).
    - Proteção contra falso positivo pai-filho: evidência de HF-05-02 JAMAIS conclui HF-05.
    - Fonte offline ou com erro é marcada como stale ou unavailable sem quebrar nem truncar o grafo do DAG.
    """

    def __init__(
        self,
        path: Path,
        *,
        evidence_dir: Path | None = None,
        receipts: list[Any] | None = None,
        receipts_dir: Path | None = None,
        source_id: str = "continuous-autonomy-plan",
        label: str = "Plano de Autonomia Contínua",
        priority: int = 15,
    ) -> None:
        self.path = Path(path)
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self.receipts = list(receipts) if receipts is not None else []
        self.receipts_dir = Path(receipts_dir) if receipts_dir else None
        self.source_id = source_id
        self.label = label
        self.priority = priority
        self._cached_records: list[RoadmapCandidate] | None = None
        self._cached_hash: str | None = None

    def read(self, project_id: str) -> RoadmapSourceResult:
        locator = self.path.as_posix()
        if not self.path.exists():
            status = "stale" if self._cached_records is not None else "unavailable"
            records = self._cached_records or []
            state = RoadmapSourceState(
                source_id=self.source_id,
                label=self.label,
                source_kind="document",
                locator=locator,
                status=status,
                revision=self._cached_hash,
                content_hash=self._cached_hash,
                error=f"file not found: {locator}",
            )
            return RoadmapSourceResult(state=state, records=records, content="")

        try:
            content = self.path.read_text(encoding="utf-8")
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise ValueError("plan manifest must be a JSON object")

            raw_units = payload.get("units", [])
            if not isinstance(raw_units, list):
                raise ValueError("plan units must be a list")

            evidence_files = self._evidence_files()
            content_hash = hashlib.sha256(
                json.dumps(
                    {
                        "manifest": content,
                        "evidence": [
                            {
                                "name": r.name,
                                "hash": hashlib.sha256(r.read_bytes()).hexdigest(),
                            }
                            for r in evidence_files
                        ],
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()

            records: list[RoadmapCandidate] = []
            for unit in raw_units:
                if not isinstance(unit, dict):
                    continue
                ticket_id = str(unit.get("ticket_id", "")).strip()
                if not ticket_id:
                    continue

                parent_id = str(unit.get("parent_id", "")).strip() or None
                title = str(unit.get("title", ticket_id)).strip()
                oracle = str(unit.get("oracle", "")).strip()
                api_desc = str(unit.get("api", "")).strip()
                criteria = [oracle] if oracle else ([api_desc] if api_desc else [])

                planning_status = unit.get("planning_status")
                implementation_status = unit.get("implementation_status")
                operational_status = unit.get("operational_status")

                dependencies = [
                    RoadmapDependency(
                        item_id=str(dep_id).strip(),
                        type=DependencyType.REQUIRES,
                        label="Dependência declarada no plano de autonomia contínua",
                    )
                    for dep_id in unit.get("depends_on", [])
                    if str(dep_id).strip()
                ]

                evidence_refs = self._evidence_refs(ticket_id)
                has_verified_receipt = any(ref.verified for ref in evidence_refs)

                if has_verified_receipt:
                    status = DeliveryStatus.COMPLETED
                    rationale = f"Concluído com recibo de evidência vinculado e confiável ({ticket_id})."
                elif evidence_refs:
                    status = DeliveryStatus.PLANNED
                    rationale = f"Ticket {ticket_id} possui relatório documental não verificado; conclusão requer recibo verificado."
                else:
                    status = DeliveryStatus.PLANNED
                    rationale = f"Planejado no plano de autonomia contínua com planning_status={planning_status}."

                source_ref = RoadmapSourceRef(
                    source_id=self.source_id,
                    source_kind="document",
                    label=f"{self.label} — {ticket_id}",
                    locator=f"{locator}#{ticket_id}",
                    revision=content_hash,
                    produced_by="continuous-autonomy-planner",
                )

                candidate = RoadmapCandidate(
                    id=ticket_id,
                    project_id=project_id,
                    title=f"{ticket_id} — {title}",
                    description=f"API: {api_desc}\nOracle: {oracle}".strip(),
                    state_rationale=rationale,
                    item_type=RoadmapItemType.FEATURE,
                    lifecycle_stage=LifecycleStage.EXECUTION,
                    delivery_status=status,
                    horizon=PlanningHorizon.NOW,
                    confidence=ConfidenceLevel.HIGH,
                    dependencies=dependencies,
                    parent_id=parent_id,
                    planning_status=str(planning_status) if planning_status else None,
                    implementation_status=str(implementation_status) if implementation_status else None,
                    operational_status=str(operational_status) if operational_status else None,
                    tags=["continuous-autonomy"] + ([f"parent:{parent_id}"] if parent_id else []),
                    completion_criteria=criteria,
                    source_refs=[source_ref],
                    evidence_refs=evidence_refs,
                    source_revision=content_hash,
                    source_id=self.source_id,
                    source_priority=self.priority,
                )
                records.append(candidate)

            self._cached_records = records
            self._cached_hash = content_hash

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
            status = "stale" if self._cached_records is not None else "unavailable"
            records = self._cached_records or []
            state = RoadmapSourceState(
                source_id=self.source_id,
                label=self.label,
                source_kind="document",
                locator=locator,
                status=status,
                revision=self._cached_hash,
                content_hash=self._cached_hash,
                error=str(exc),
            )
            return RoadmapSourceResult(state=state, records=records, content="")

    def _evidence_files(self) -> list[Path]:
        if self.evidence_dir is None or not self.evidence_dir.exists():
            return []
        return sorted(
            report
            for report in self.evidence_dir.glob("*-report.md")
            if report.is_file()
        )

    def _evidence_refs(self, item_id: str) -> list[RoadmapEvidenceRef]:
        refs: list[RoadmapEvidenceRef] = []
        refs.extend(self._find_receipts_for_item(item_id))

        if self.evidence_dir is not None and self.evidence_dir.exists():
            for report in self._evidence_files():
                if is_report_for_ticket(report.name, item_id):
                    refs.append(
                        RoadmapEvidenceRef(
                            evidence_id=f"doc:{item_id}:{report.name}",
                            evidence_kind="documentary_unverified",
                            label=f"Relatório documental não verificado {item_id}",
                            locator=report.as_posix(),
                            verified=False,
                        )
                    )
        return refs

    def _find_receipts_for_item(self, item_id: str) -> list[RoadmapEvidenceRef]:
        receipt_refs: list[RoadmapEvidenceRef] = []
        for r in self.receipts:
            if isinstance(r, RoadmapEvidenceRef):
                if (hasattr(r, "subject") and r.subject == item_id) or r.evidence_id.startswith(f"receipt:{item_id}:"):
                    receipt_refs.append(r)
            elif isinstance(r, dict):
                if r.get("subject") == item_id:
                    res = r.get("result", "passed")
                    is_passed = str(res) in ("passed", "EvidenceResult.PASSED") or getattr(res, "value", None) == "passed"
                    is_verified = r.get("verified", True) and is_passed
                    receipt_refs.append(
                        RoadmapEvidenceRef(
                            evidence_id=r.get("receipt_id") or f"receipt:{item_id}:{len(receipt_refs)}",
                            evidence_kind="verified_receipt",
                            label=r.get("label") or f"Recibo verificado {item_id}",
                            locator=r.get("locator") or f"receipt://{r.get('receipt_id', 'receipt')}",
                            observed_at=r.get("observed_at"),
                            verified=is_verified,
                        )
                    )
            elif hasattr(r, "subject") and r.subject == item_id:
                res = getattr(r, "result", None)
                is_passed = str(res) in ("passed", "EvidenceResult.PASSED") or (hasattr(res, "value") and res.value == "passed")
                is_verified = getattr(r, "verified", True) and is_passed
                receipt_id = getattr(r, "receipt_id", f"receipt-{item_id}")
                receipt_refs.append(
                    RoadmapEvidenceRef(
                        evidence_id=f"receipt:{item_id}:{receipt_id}",
                        evidence_kind="verified_receipt",
                        label=f"Recibo verificado {item_id}",
                        locator=getattr(r, "locator", f"receipt://{receipt_id}"),
                        observed_at=getattr(r, "observed_at", None),
                        verified=is_verified,
                    )
                )

        if self.receipts_dir is not None and self.receipts_dir.exists():
            for f in sorted(self.receipts_dir.glob("*.json")):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and data.get("subject") == item_id:
                        res = data.get("result", "passed")
                        is_passed = str(res) in ("passed", "EvidenceResult.PASSED") or getattr(res, "value", None) == "passed"
                        is_verified = data.get("verified", True) and is_passed
                        receipt_refs.append(
                            RoadmapEvidenceRef(
                                evidence_id=data.get("receipt_id") or f"receipt:{item_id}:{f.name}",
                                evidence_kind="verified_receipt",
                                label=data.get("label") or f"Recibo verificado {item_id}",
                                locator=f.as_posix(),
                                observed_at=data.get("observed_at"),
                                verified=is_verified,
                            )
                        )
                except Exception:
                    continue

        return receipt_refs
