"""Operational evidence report for autonomous coordination (USR-539 T6).

The report is deliberately evidence driven: implementation or test presence is not operational
proof. Each criterion is approved only when a persisted evidence reference is supplied.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from core.paths import state_root

CRITERIA = {
    "priority": "Prioridade explicita e selecao elegivel",
    "harness_entry": "Entrada curta nos quatro harnesses",
    "learning_concurrency": "Aprendizagem concorrente recuperavel",
    "quota_repair": "Reparo de leitura de cota",
    "continuity": "Continuidade sem chat e nova admissao",
}
DEFAULT_EVIDENCE_DIR = state_root() / "reports" / "coordination-proof" / "evidence"


class CoordinationCriterion(BaseModel):
    id: str
    title: str
    verified: bool
    evidence_refs: list[str] = Field(default_factory=list)
    degraded_sources: list[str] = Field(default_factory=list)


class CoordinationProofReport(BaseModel):
    generated_at: str
    criterion_met: bool
    criteria: list[CoordinationCriterion]
    missing: list[str] = Field(default_factory=list)
    degraded_sources: list[str] = Field(default_factory=list)


def evaluate_coordination_proof(
    evidence: dict[str, list[str]], *, now: datetime, degraded_sources: list[str] | None = None,
) -> CoordinationProofReport:
    """Build report from durable references; absent evidence remains pending."""
    degraded = sorted(set(degraded_sources or []))
    criteria: list[CoordinationCriterion] = []
    for criterion_id, title in CRITERIA.items():
        refs = [str(ref) for ref in evidence.get(criterion_id, []) if str(ref).strip()]
        criterion_degraded = [code for code in degraded if code.startswith(f"{criterion_id}:")]
        verified = bool(refs) and not criterion_degraded
        criteria.append(CoordinationCriterion(
            id=criterion_id, title=title, verified=verified,
            evidence_refs=refs, degraded_sources=criterion_degraded,
        ))
    missing = [f"{item.id}: evidencia operacional pendente" for item in criteria if not item.verified]
    return CoordinationProofReport(
        generated_at=now.isoformat(), criterion_met=all(item.verified for item in criteria),
        criteria=criteria, missing=missing, degraded_sources=degraded,
    )


def collect_coordination_proof(*, evidence_dir: Path | None = None, now: datetime | None = None) -> CoordinationProofReport:
    """Read one JSON evidence manifest per criterion; never exposes exception text/secrets."""
    directory = evidence_dir or DEFAULT_EVIDENCE_DIR
    evidence: dict[str, list[str]] = {}
    degraded: list[str] = []
    for criterion_id in CRITERIA:
        path = directory / f"{criterion_id}.json"
        if not path.exists():
            continue
        try:
            payload: Any = json.loads(path.read_text(encoding="utf-8"))
            refs = payload.get("evidence_refs", []) if isinstance(payload, dict) else []
            if not isinstance(refs, list):
                degraded.append(f"{criterion_id}:invalid_evidence_manifest")
                continue
            valid_refs: list[str] = []
            for ref in refs:
                if not isinstance(ref, str) or not ref.strip():
                    continue
                target = Path(ref)
                if not target.is_absolute():
                    target = path.parent / target
                try:
                    resolved = target.resolve(strict=True)
                    if resolved.is_file() and resolved.stat().st_size > 0:
                        valid_refs.append(str(resolved))
                    else:
                        degraded.append(f"{criterion_id}:invalid_evidence_ref")
                except (OSError, RuntimeError):
                    degraded.append(f"{criterion_id}:invalid_evidence_ref")
            evidence[criterion_id] = valid_refs
        except (OSError, json.JSONDecodeError):
            degraded.append(f"{criterion_id}:evidence_read_failed")
    return evaluate_coordination_proof(evidence, now=now or datetime.now(UTC), degraded_sources=degraded)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Prova operacional da coordenacao autonoma")
    parser.add_argument("--evidence-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    report = collect_coordination_proof(evidence_dir=args.evidence_dir)
    print(report.model_dump_json(indent=2))
    return 0 if report.criterion_met else 1


if __name__ == "__main__":
    raise SystemExit(main())
