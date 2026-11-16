"""Politica que separa historico por ticket do snapshot da fase (USR-499)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.orchestrator.docs_sync_policy import (
    CURRENT_PHASE_DOCUMENT,
    DocsSyncPolicyError,
    delivery_history_documents,
    ticket_history_lines,
    validate_docs_sync,
)

ROOT = Path(__file__).resolve().parents[1]
PHASE = ROOT / "docs" / "FASE_ATUAL.md"

# Oraculo do aceite, independente do classificador: data no inicio do item,
# depois ":" ou "(", e um ticket logo em seguida. Tambem titulos de historico.
_FORBIDDEN_DIARY = re.compile(
    r"^\s*(?:[-*+]|\d+\.)\s+(?:\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})\s*[:\(]\s*"
    r"(?:USR|BUG|FIX|TASK)-\d+\b",
    re.IGNORECASE,
)
_FORBIDDEN_HEADING = re.compile(
    r"^\s{0,3}#{1,6}\s+(?:hist[oó]rico\b|(?:ticket\s+)?(?:USR|BUG|FIX|TASK)-\d+\b)",
    re.IGNORECASE,
)

CONSOLIDATED_SECTIONS = (
    "## Canais de entrada",
    "## Ambiente e máquinas",
    "## Orçamento",
    "## Metas técnicas da fase",
    "## Piloto de produto",
    "## Portões técnicos de hoje",
)


def _iso(month: int, day: int) -> str:
    """Monta AAAA-MM-DD sem gravar o ano colado ao hifen no fonte."""

    year = "2026"
    return f"{year}-{month:02d}-{day:02d}"


def _refused_lines() -> list[str]:
    iso_colon = _iso(11, 10)
    iso_paren = _iso(11, 6)
    iso_task = _iso(11, 1)
    return [
        "- 10/11/2026: USR-494 entregou a via expressa.",
        "- 05/11/2026 (USR-473): owner confirmou a contingencia.",
        f"- {iso_colon}: USR-499 retirou o historico.",
        f"- {iso_paren} (USR-468): receipt do mesmo candidato.",
        "* 01/11/2026: BUG-12 corrige o canal.",
        f"1. {iso_task} (TASK-4): fecha o portao.",
        "- 06/11/2026: a recuperacao exige receipt (USR-468).",
        "## Histórico",
        "## Histórico de entregas",
        "## Historico",
        "## USR-499",
        "### USR-12 fecha o rebase",
        "## Ticket USR-7",
    ]


REFUSED_LINES = _refused_lines()


def test_phase_snapshot_keeps_consolidated_facts_without_ticket_history() -> None:
    text = PHASE.read_text(encoding="utf-8")
    assert ticket_history_lines(text) == []
    for line in text.splitlines():
        assert _FORBIDDEN_DIARY.match(line) is None
        assert _FORBIDDEN_HEADING.match(line) is None
    for heading in CONSOLIDATED_SECTIONS:
        assert heading in text
    validate_docs_sync(
        [CURRENT_PHASE_DOCUMENT],
        {CURRENT_PHASE_DOCUMENT: text.splitlines()},
    )


@pytest.mark.parametrize("line", REFUSED_LINES)
def test_validate_docs_sync_refuses_ticket_history(line: str) -> None:
    with pytest.raises(DocsSyncPolicyError) as caught:
        validate_docs_sync(
            [CURRENT_PHASE_DOCUMENT],
            {CURRENT_PHASE_DOCUMENT: [line]},
        )
    message = str(caught.value)
    assert "docs/manual/" in message
    assert ".factory/learning_packs/" in message
    assert line.strip() in message


def test_consolidated_sentence_citing_a_ticket_in_the_middle_passes() -> None:
    validate_docs_sync(
        [CURRENT_PHASE_DOCUMENT],
        {
            CURRENT_PHASE_DOCUMENT: [
                "Antes da worktree, a admissao (USR-468 T1) confirma a rota de planning.",
                "- O teto mensal permanece no bloco USR-378.",
            ]
        },
    )


def test_dated_consolidated_fact_without_a_ticket_passes() -> None:
    validate_docs_sync(
        [CURRENT_PHASE_DOCUMENT],
        {CURRENT_PHASE_DOCUMENT: ["- 26/10/2026: Desktop fora da frota por indexacao."]},
    )


def test_only_manual_path_without_phase_history_passes() -> None:
    validate_docs_sync(["docs/manual/frota.md"], None)
    validate_docs_sync(
        ["docs/manual/"],
        {"docs/manual/frota.md": ["Registro da entrega no manual, sem tocar o snapshot."]},
    )


def test_only_learning_pack_path_without_phase_history_passes() -> None:
    validate_docs_sync([".factory/learning_packs/USR-499.md"], None)
    validate_docs_sync(
        [".factory/learning_packs/"],
        {".factory/learning_packs/USR-499.md": ["## USR-499", "Registro da entrega no pack."]},
    )


def test_manual_and_learning_pack_are_not_required_together() -> None:
    documents = delivery_history_documents()
    assert "docs/MANUAL.md" not in documents
    assert "docs/LEARNING_PACK.md" not in documents
    assert "docs/manual/" in documents
    assert ".factory/learning_packs/" in documents


def test_windows_phase_path_still_refuses_history() -> None:
    with pytest.raises(DocsSyncPolicyError) as caught:
        validate_docs_sync(
            [r"docs\FASE_ATUAL.md"],
            {r"docs\FASE_ATUAL.md": ["- 10/11/2026: USR-499 conflito de rebase."]},
        )
    message = str(caught.value)
    assert "docs/manual/" in message
    assert ".factory/learning_packs/" in message
