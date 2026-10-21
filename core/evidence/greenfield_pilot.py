"""Parse and verify the auditable M7 report for the USR-279 pilot."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MilestoneEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    milestone: str
    status: str
    reference: str = ""
    blocker: str = ""


class CriterionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion: int
    status: str
    reference: str = ""
    blocker: str = ""


class PilotReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    declared_complete: bool
    timeline: list[MilestoneEvidence] = Field(default_factory=list)
    criteria: list[CriterionEvidence] = Field(default_factory=list)
    raw: str


def _table_rows(text: str, heading: str) -> list[list[str]]:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == heading), -1)
    if start < 0:
        return []
    rows: list[list[str]] = []
    in_table = False
    for line in lines[start + 1 :]:
        if not line.strip():
            if in_table:
                break
            continue
        if not line.startswith("|"):
            if in_table:
                break
            continue
        in_table = True
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", c.replace(" ", "")) for c in cells):
            continue
        rows.append(cells)
    return rows[1:] if rows else []


def parse_report(report: str) -> PilotReport:
    """Parse timeline and criterion tables from an M7 Markdown report."""
    completion = re.search(r"(?im)^Piloto conclu[ií]do\s*:\s*(sim|n[aã]o)\.?\s*$", report)
    timeline_rows = _table_rows(report, "## Linha do tempo M1-M6")
    criteria_rows = _table_rows(report, "## Criterios 1, 2 e 3 do USR-279")
    timeline = [
        MilestoneEvidence(
            milestone=row[0], status=row[1], reference=row[2] if len(row) > 2 else "",
            blocker=row[3] if len(row) > 3 else "",
        )
        for row in timeline_rows if len(row) >= 2
    ]
    criteria: list[CriterionEvidence] = []
    for row in criteria_rows:
        number = re.search(r"\d+", row[0]) if row else None
        if len(row) >= 2 and number:
            criteria.append(CriterionEvidence(
                criterion=int(number.group()), status=row[1],
                reference=row[2] if len(row) > 2 else "",
                blocker=row[3] if len(row) > 3 else "",
            ))
    return PilotReport(
        declared_complete=bool(completion and completion.group(1).lower() in {"sim", "não", "nao"} and completion.group(1).lower() == "sim"),
        timeline=timeline,
        criteria=criteria,
        raw=report,
    )


def verify_report(
    report: str | PilotReport,
    *,
    root: Path | None = None,
    demand: dict[str, Any] | None = None,
) -> list[str]:
    """Return all structural, evidence, completion, and ticket consistency problems."""
    parsed = parse_report(report) if isinstance(report, str) else report
    repo = root or Path.cwd()
    problems: list[str] = []
    if not re.search(r"(?im)^Piloto conclu[ií]do\s*:\s*(sim|n[aã]o)\.?\s*$", parsed.raw):
        problems.append("declaracao de conclusao do piloto ausente ou invalida")
    required_headings = (
        "## Linha do tempo M1-M6", "## SHAs, PR e CI", "## Deploy e smoke",
        "## Prova de independencia", "## Intervencoes manuais", "## Falhas e lacunas",
        "## Criterios 1, 2 e 3 do USR-279",
    )
    for heading in required_headings:
        if heading not in parsed.raw:
            problems.append(f"secao obrigatoria ausente: {heading}")
    milestones: dict[str, MilestoneEvidence] = {}
    for item in parsed.timeline:
        if item.milestone in milestones:
            problems.append(f"marco {item.milestone} duplicado na linha do tempo")
        milestones[item.milestone] = item
    for milestone in (f"M{i}" for i in range(1, 7)):
        item = milestones.get(milestone)
        if item is None:
            problems.append(f"marco {milestone} ausente da linha do tempo")
            continue
        if item.status not in {"confirmado", "pendente"}:
            problems.append(f"status desconhecido para {milestone}: {item.status}")
        elif item.status == "confirmado":
            ref = item.reference.strip()
            paths = re.findall(r"(?:docs|core)/[A-Za-z0-9_./-]+", ref)
            valid_path = any((repo / path.rstrip(".,;)")).is_file() for path in paths)
            has_url = bool(re.search(r"https://\S+", ref))
            if not ref or ref in {"—", "-", "n/a"} or not (valid_path or has_url):
                problems.append(f"marco confirmado {milestone} sem referencia verificavel")
        elif not item.blocker.strip() or item.blocker.strip() in {"—", "-"}:
            problems.append(f"marco pendente {milestone} sem bloqueador")
    if parsed.declared_complete and any(item.status == "pendente" for item in parsed.timeline):
        problems.append("piloto declarado concluido com marco pendente")
    criteria: dict[int, CriterionEvidence] = {}
    for item in parsed.criteria:
        if item.criterion in criteria:
            problems.append(f"criterio {item.criterion} duplicado no relatorio")
        criteria[item.criterion] = item
    for number in (1, 2, 3):
        item = criteria.get(number)
        if item is None:
            problems.append(f"criterio {number} ausente")
            continue
        if item.status not in {"confirmado", "pendente"}:
            problems.append(f"status desconhecido para criterio {number}: {item.status}")
        if item.status == "confirmado" and not item.reference.strip():
            problems.append(f"criterio {number} confirmado sem referencia")
        if item.status == "confirmado":
            paths = re.findall(r"(?:docs|core)/[A-Za-z0-9_./-]+", item.reference)
            if not any((repo / path.rstrip(".,;)")).is_file() for path in paths):
                problems.append(f"criterio {number} confirmado sem referencia verificavel")
        if item.status == "pendente" and not item.blocker.strip():
            problems.append(f"criterio {number} pendente sem bloqueador")
    if demand is not None:
        evidence = str(demand.get("delivery_evidence") or "").lower()
        for number in (1, 2, 3):
            item = criteria.get(number)
            if item is None:
                continue
            claimed_confirmed = bool(re.search(rf"criterio\s*{number}\s*:\s*confirmado", evidence))
            claimed_pending = bool(re.search(rf"criterio\s*{number}\s*:\s*pendente", evidence))
            if not claimed_confirmed and not claimed_pending:
                problems.append(f"criterio {number} sem status explicito no ticket")
            if item.status == "pendente" and claimed_confirmed:
                problems.append(f"criterio {number} marcado confirmado no ticket sem confirmacao no relatorio")
            if item.status == "confirmado" and not claimed_confirmed:
                problems.append(f"criterio {number} confirmado no relatorio mas nao no ticket")
            if item.status == "pendente" and not claimed_pending:
                problems.append(f"criterio {number} pendente no relatorio mas nao no ticket")
            if item.status == "confirmado" and claimed_pending:
                problems.append(f"criterio {number} pendente no ticket apesar de confirmado no relatorio")
    return problems
