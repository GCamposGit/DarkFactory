from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.evidence.greenfield_pilot import parse_report, verify_report


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs/evidence/USR-279-M7.md"


def _report_with_timeline(rows: str, *, conclusion: str = "Piloto concluido: nao.") -> str:
    return f"""# Relatorio M7 USR-279

{conclusion}

## Linha do tempo M1-M6

| Marco | Status | Referencia verificavel | Bloqueador |
| --- | --- | --- | --- |
{rows}

## SHAs, PR e CI
## Deploy e smoke
## Prova de independencia
## Intervencoes manuais
## Falhas e lacunas
"""


def test_rejects_unknown_status() -> None:
    report = _report_with_timeline("| M1 | desconhecido | docs/evidence/USR-279-M1.md | — |")
    assert any("status" in problem.lower() for problem in verify_report(report, root=ROOT))


def test_rejects_confirmed_milestone_without_verifiable_reference() -> None:
    report = _report_with_timeline("| M1 | confirmado | — | — |")
    assert any("refer" in problem.lower() for problem in verify_report(report, root=ROOT))


def test_rejects_pending_milestone_without_blocker() -> None:
    report = _report_with_timeline("| M1 | pendente | docs/evidence/USR-279-M1.md | — |")
    assert any("bloque" in problem.lower() for problem in verify_report(report, root=ROOT))


def test_rejects_completed_pilot_with_pending_milestone() -> None:
    report = _report_with_timeline(
        "| M1 | pendente | — | OA-043 |", conclusion="Piloto concluido: sim."
    )
    assert any("conclu" in problem.lower() for problem in verify_report(report, root=ROOT))


def test_rejects_incomplete_and_duplicate_milestone_timeline() -> None:
    report = _report_with_timeline(
        "| M1 | confirmado | docs/evidence/USR-279-M1.md | — |\n"
        "| M1 | confirmado | docs/evidence/USR-279-M1.md | — |"
    )
    problems = verify_report(report, root=ROOT)
    assert any("M1 duplicado" in problem for problem in problems)
    assert any("marco M2 ausente" in problem for problem in problems)


def test_rejects_unverified_sha_as_only_confirmed_reference() -> None:
    report = _report_with_timeline(
        "| M1 | confirmado | commit `deadbeefdeadbeefdeadbeefdeadbeefdeadbeef` | — |"
    )
    assert any("referencia verificavel" in problem for problem in verify_report(report, root=ROOT))


def test_published_report_passes_verifier() -> None:
    assert REPORT.exists()
    assert verify_report(REPORT.read_text(encoding="utf-8"), root=ROOT) == []


def test_m1_m2_rows_repeat_evidence_sha_and_times() -> None:
    parsed = parse_report(REPORT.read_text(encoding="utf-8"))
    rows = {item.milestone: item for item in parsed.timeline}
    for milestone, path, sha, time in (
        ("M1", "docs/evidence/USR-279-M1.md", "23960d95add7595952c8b8ba679af13becc43348", "15:08Z"),
        ("M2", "docs/evidence/USR-279-M2.md", "e5c2ef15ba0641820f05a0687146918b3f9ff95a", "15:33Z"),
    ):
        row = rows[milestone]
        assert path in row.reference
        assert sha in row.reference
        assert time in row.reference


def test_m1_m2_references_match_sha_and_time_in_source_evidence() -> None:
    parsed = parse_report(REPORT.read_text(encoding="utf-8"))
    rows = {item.milestone: item for item in parsed.timeline}
    for milestone in ("M1", "M2"):
        reference = rows[milestone].reference
        match = re.search(r"docs/evidence/USR-279-" + milestone + r"\.md", reference)
        assert match is not None
        source = (ROOT / match.group()).read_text(encoding="utf-8")
        sha_matches = re.findall(r"\b[0-9a-f]{7,40}\b", source, re.I)
        time_matches = re.findall(r"\b\d{2}:\d{2}Z\b", source)
        assert any(sha in reference for sha in sha_matches)
        assert any(time in reference for time in time_matches)
