"""USR-483 T5: coordination docs follow the lease the code actually stores."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from core.line.coordination import (
    ABSENCE_HEARTBEAT_EXPIRED,
    ABSENCE_NO_COORDINATOR,
    LEASE_TTL_SECONDS,
    ORIGIN_OWNER_DECISION,
)
from core.usage.models import STALE_AFTER_SECONDS


ROOT = Path(__file__).resolve().parents[2]
DOCS = {
    "manual": ROOT / "docs/manual/operacao_interativa.md",
    "handoff": ROOT / "docs/operacao/HANDOFF.md",
    "fase": ROOT / "docs/FASE_ATUAL.md",
    "interop": ROOT / "docs/HARNESS_INTEROP.md",
}


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(character for character in normalized if not unicodedata.combining(character)).lower()


def _current_fase(text: str) -> str:
    for heading in ("## Histórico", "## Historico"):
        head, separator, _history = text.partition(heading)
        if separator:
            return head
    return text


def _required_phrases() -> tuple[str, ...]:
    return (
        "identidade da sessao",
        "sessao ativa",
        "coordenador",
        "scripts/line_briefing.py",
        "assumir --decisao-owner",
        "comportamento esperado",
        "despachante",
        "fila global",
        "politica da frota",
        "handoff de coordenacao",
        "worktree",
        "suite_lock",
        "reservas",
        ABSENCE_NO_COORDINATOR,
        ABSENCE_HEARTBEAT_EXPIRED,
        ORIGIN_OWNER_DECISION,
        "nao elege",
        "a frota segue",
        f"{LEASE_TTL_SECONDS} segundos",
        f"{LEASE_TTL_SECONDS // 60} minutos",
        "sem sinal confiavel",
        "valor antigo",
        "pendencia humana",
        "sem pedido de confirmacao",
    )


def test_four_docs_match_the_coordination_lease() -> None:
    missing: list[str] = []
    for name, path in DOCS.items():
        folded = _fold(path.read_text(encoding="utf-8"))
        for phrase in _required_phrases():
            if _fold(phrase) not in folded:
                missing.append(f"{name}: {phrase}")
        if re.search(r"darkfac_operating_harness\s*=", folded):
            missing.append(f"{name}: instructs persisting DARKFAC_OPERATING_HARNESS")
        if "$env:darkfac_operating_harness" in folded:
            missing.append(f"{name}: assigns DARKFAC_OPERATING_HARNESS in the shell")
    assert missing == []


def test_handoff_and_current_phase_point_at_generated_state() -> None:
    regions = {
        "handoff": _fold(DOCS["handoff"].read_text(encoding="utf-8")),
        "fase": _fold(_current_fase(DOCS["fase"].read_text(encoding="utf-8"))),
    }
    frozen_quota = re.compile(r"\d{2,}[.,]\d+\s*%")
    for name, text in regions.items():
        assert "estado gerado" in text
        assert "scripts/line_briefing.py" in text
        assert "pr #" not in text
        assert frozen_quota.search(text) is None
        assert "operador antigravity" not in text
        assert "interface interativa de operacao" not in text
        assert re.search(r"coordenador:\s*(claude|codex|grok|antigravity)\b", text) is None


def test_antigravity_limit_quota_floor_and_review_stay() -> None:
    manual = _fold(DOCS["manual"].read_text(encoding="utf-8"))
    fase = _fold(_current_fase(DOCS["fase"].read_text(encoding="utf-8")))
    interop = _fold(DOCS["interop"].read_text(encoding="utf-8"))
    for text in (manual, fase, interop):
        assert "antigravity" in text
        assert "headless" in text
        assert "frota" in text
        assert "15%" in text
        assert "outra familia" in text
    assert str(STALE_AFTER_SECONDS) in manual
    assert "headless ou frota" in manual
