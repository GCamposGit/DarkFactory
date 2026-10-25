"""Politica de pesquisa condicional com ledger (USR-376, Frente 1).

Regras fechadas (avaliadas em ordem, a primeira que casa decide):

R1. tag ou titulo de correcao/bug/chore/docs/teste        -> NAO pesquisar
R2. item_type documentation/quality/operations            -> NAO pesquisar
R3. complexidade small/low sem sinal de arquitetura/integracao -> NAO pesquisar
R4. texto cita arquitetura (arquitetura, contrato, protocolo...) -> pesquisar
R5. texto cita integracao externa (api, oauth, webhook...)       -> pesquisar
R6. item_type feature/infrastructure/security com complexidade >= medium -> pesquisar
R7. demais                                                -> NAO pesquisar

O ledger e consultado antes de qualquer busca; tema ja pesquisado e reutilizado.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, Field, field_validator

from core.research.ledger import KnowledgeLedgerManager
from core.research.models import (
    AuthorityTier,
    ResearchLedger,
    ResearchSource,
    ResearchTopicType,
    SourceInsight,
)

logger = logging.getLogger(__name__)

SKIP_TAGS = frozenset({"bug", "bugfix", "fix", "chore", "docs", "documentation", "test", "tests", "hotfix"})
SKIP_TITLE = re.compile(r"^\s*(fix|bug|chore|docs?|test|corrig\w*|ajust\w*|typo)\b", re.I)
SKIP_ITEM_TYPES = frozenset({"documentation", "quality", "operations"})
ARCH_WORDS = ("arquitetura", "architecture", "design de", "contrato", "motor de", "orquestra", "protocolo")
EXTERNAL_WORDS = ("api ", "oauth", "webhook", "integra", "sdk", "mcp", "provider", "provedor", "telegram", "github", "dokploy", "stripe")
RESEARCH_ITEM_TYPES = frozenset({"feature", "infrastructure", "security"})
SMALL = frozenset({"small", "low"})
_STOP = frozenset("para como com uma uns das dos que por não nao pelo pela sobre entre sem mais the and for with from this that".split())
REUSED_PREFIX = "Como influenciou o plano: "


class ResearchDecision(BaseModel):
    research: bool
    rule: str
    reason: str


class ResearchSourceRecord(BaseModel):
    title: str = Field(min_length=1)
    url: str
    accessed_at: str = Field(min_length=10)
    insight: str = Field(min_length=1)
    influence: str = Field(min_length=1, description="Como influenciou o plano")

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not re.match(r"^https?://\S+$", v or ""):
            raise ValueError("fonte exige URL http(s)")
        return v


class ResearchArtifact(BaseModel):
    kind: Literal["research_skipped", "research_done", "research_reused"]
    ticket_id: str
    reason: str = ""
    ledger_id: str | None = None
    sources: list[ResearchSourceRecord] = Field(default_factory=list)

    def model_post_init(self, _ctx: Any) -> None:
        if self.kind == "research_skipped" and not self.reason:
            raise ValueError("research_skipped exige motivo")
        if self.kind != "research_skipped" and not self.sources:
            raise ValueError(f"{self.kind} exige ao menos uma fonte")


def decide(ticket: Mapping[str, Any]) -> ResearchDecision:
    """Decide (funcao pura) se o ticket exige pesquisa."""
    tags = {str(t).lower() for t in ticket.get("tags") or []}
    item_type = str(ticket.get("item_type") or "").lower()
    cx = str(ticket.get("estimated_complexity") or "").lower()
    title = str(ticket.get("title") or "")
    text = f"{title} {ticket.get('problem_statement') or ''}".lower()
    arch = any(w in text for w in ARCH_WORDS)
    ext = any(w in text for w in EXTERNAL_WORDS)

    if tags & SKIP_TAGS or SKIP_TITLE.match(title):
        return ResearchDecision(research=False, rule="R1", reason="correcao/bug/chore/docs/teste dispensa pesquisa")
    if item_type in SKIP_ITEM_TYPES:
        return ResearchDecision(research=False, rule="R2", reason=f"item_type {item_type} dispensa pesquisa")
    if cx in SMALL and not (arch or ext):
        return ResearchDecision(research=False, rule="R3", reason="mudanca pequena sem arquitetura nem integracao externa")
    if arch:
        return ResearchDecision(research=True, rule="R4", reason="decisao de arquitetura")
    if ext:
        return ResearchDecision(research=True, rule="R5", reason="integracao externa")
    if item_type in RESEARCH_ITEM_TYPES and cx not in SMALL:
        return ResearchDecision(research=True, rule="R6", reason=f"{item_type} de complexidade {cx or 'indefinida'}")
    return ResearchDecision(research=False, rule="R7", reason="sem criterio de pesquisa atendido")


def topic_terms(ticket: Mapping[str, Any]) -> list[str]:
    words = re.findall(r"[a-zà-ú0-9]{4,}", str(ticket.get("title") or "").lower())
    return sorted({w for w in words if w not in _STOP})


def lookup(terms: list[str], ledger_root: Path, min_overlap: float = 0.6) -> list[ResearchLedger]:
    """Ledgers existentes cujo texto cobre >= min_overlap dos termos do tema."""
    if not terms:
        return []
    mgr = KnowledgeLedgerManager(Path(ledger_root))
    found: list[ResearchLedger] = []
    for row in mgr.list_ledgers():
        try:
            ledger = mgr.load_ledger(row["ledger_id"])
        except Exception as exc:  # noqa: BLE001 - one unreadable ledger must not fail the stage
            logger.warning("ledger de pesquisa ilegivel ignorado (%s): %s", row.get("ledger_id"), exc)
            continue
        if ledger is None or not ledger.sources:
            continue
        hay = (ledger.query + " " + ledger.summary_executive).lower()
        if sum(t in hay for t in terms) / len(terms) >= min_overlap:
            found.append(ledger)
    return found


def _records(ledger: ResearchLedger) -> list[ResearchSourceRecord]:
    by_id = {i.source_id: i for i in ledger.insights}
    out = []
    for s in ledger.sources:
        ins = by_id.get(s.id)
        influence = ins.architectural_implications.removeprefix(REUSED_PREFIX) if ins else ""
        out.append(ResearchSourceRecord(
            title=s.title,
            url=s.url,
            accessed_at=str(s.metadata.get("accessed_at", ledger.created_at[:10])),
            insight=ins.key_insight if ins else (s.summary or s.title),
            influence=influence or "reutilizado do ledger",
        ))
    return out


def _write(ledger_root: Path, art: ResearchArtifact) -> ResearchArtifact:
    d = Path(ledger_root) / "_artifacts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{art.ticket_id}.json").write_text(art.model_dump_json(indent=2), encoding="utf-8")
    return art


def run_research_stage(
    ticket: Mapping[str, Any],
    ledger_root: Path,
    web_search: Callable[[str], list[Mapping[str, Any]]],
) -> ResearchArtifact:
    """Politica -> ledger -> busca injetada. Devolve artefato validado e o grava."""
    tid = str(ticket["id"])
    root = Path(ledger_root)
    decision = decide(ticket)
    if not decision.research:
        logger.info("research skipped %s: %s", tid, decision.reason)
        return _write(root, ResearchArtifact(
            kind="research_skipped", ticket_id=tid, reason=f"{decision.rule}: {decision.reason}"))

    mgr = KnowledgeLedgerManager(root)
    hits = lookup(topic_terms(ticket), root)
    if hits:
        ledger = hits[0]
        if tid not in ledger.related_tickets:
            ledger.related_tickets.append(tid)
            mgr.save_ledger(ledger)
        return _write(root, ResearchArtifact(
            kind="research_reused", ticket_id=tid, reason=decision.reason,
            ledger_id=ledger.ledger_id, sources=_records(ledger)))

    query = str(ticket.get("title") or tid)
    now = datetime.now(timezone.utc).date().isoformat()
    records = [
        ResearchSourceRecord(
            title=str(r.get("title", "")), url=str(r.get("url", "")), accessed_at=now,
            insight=str(r.get("insight", "")), influence=str(r.get("influence", "")))
        for r in web_search(query)
    ]
    art = ResearchArtifact(kind="research_done", ticket_id=tid, reason=decision.reason, sources=records)
    ledger = mgr.create_ledger(query, ResearchTopicType.TOPIC_CONCEPT)
    ledger.related_tickets.append(tid)
    ledger.summary_executive = "; ".join(r.insight for r in records)
    for n, r in enumerate(records, 1):
        sid = f"S{n}"
        ledger.add_source(ResearchSource(
            id=sid, title=r.title, url=r.url, source_type="documentation",
            summary=r.insight, metadata={"accessed_at": r.accessed_at}))
        ledger.add_insight(SourceInsight(
            source_id=sid, source_title=r.title, key_insight=r.insight,
            architectural_implications=f"{REUSED_PREFIX}{r.influence}",
            future_reference_value=r.insight, authority_tier=AuthorityTier.HIGH_CREDIBILITY))
    mgr.save_ledger(ledger)
    art.ledger_id = ledger.ledger_id
    return _write(root, art)
