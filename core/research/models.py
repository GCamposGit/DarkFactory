"""
Domain Models and Data Schemas for the Research Engine.
Strictly typed, decoupled from any UI/presentation layer.
"""

from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone


class ResearchTopicType(str, Enum):
    """Identifies the intent of a research operation."""
    TOPIC_CONCEPT = "topic_concept"  # General topics, best practices, architecture, scientific papers
    CODE_REUSE = "code_reuse"        # Existing codebases, open-source repositories, reusable components


class LicenseType(str, Enum):
    """Categorizes open source licenses for reuse compatibility."""
    PERMISSIVE = "permissive"      # MIT, Apache-2.0, BSD-2/3, ISC (Safe for direct reuse)
    COPYLEFT = "copyleft"          # GPL-2.0/3.0, AGPL (Needs strict isolation or architectural reference only)
    PROPRIETARY = "proprietary"    # Closed or restrictive
    UNKNOWN = "unknown"


class AuthorityTier(str, Enum):
    """Distinguishes between canonical technical evidence and community trend signals."""
    HIGH_CREDIBILITY = "high_credibility"  # Papers, RFCs, official docs, tested repos (For Architecture & Code)
    TREND_SIGNAL = "trend_signal"          # Industry experts, tech influencers, trending releases (For Ideation)


@dataclass
class ResearchSource:
    """Represents an authoritative research source or community trend signal."""
    id: str
    title: str
    url: str
    source_type: str  # "paper", "repository", "engineering_blog", "rfc", "documentation", "expert_trend"
    authors_or_maintainers: List[str] = field(default_factory=list)
    published_date: Optional[str] = None
    summary: str = ""
    license: Optional[str] = None
    license_category: LicenseType = LicenseType.UNKNOWN
    authority_tier: AuthorityTier = AuthorityTier.HIGH_CREDIBILITY
    credibility_score: float = 1.0  # 0.0 to 1.0
    stars: Optional[int] = None      # For code repositories
    has_test_suite: Optional[bool] = None  # True if tests detected in repo
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["license_category"] = self.license_category.value
        data["authority_tier"] = self.authority_tier.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResearchSource":
        d = dict(data)
        if "license_category" in d and isinstance(d["license_category"], str):
            d["license_category"] = LicenseType(d["license_category"])
        if "authority_tier" in d and isinstance(d["authority_tier"], str):
            d["authority_tier"] = AuthorityTier(d["authority_tier"])
        return cls(**d)


@dataclass
class SourceInsight:
    """Specific actionable insight extracted from a single research source."""
    source_id: str
    source_title: str
    key_insight: str
    architectural_implications: str
    future_reference_value: str
    authority_tier: AuthorityTier = AuthorityTier.HIGH_CREDIBILITY
    code_patterns_or_algorithms: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["authority_tier"] = self.authority_tier.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SourceInsight":
        d = dict(data)
        if "authority_tier" in d and isinstance(d["authority_tier"], str):
            d["authority_tier"] = AuthorityTier(d["authority_tier"])
        return cls(**d)



@dataclass
class ResearchLedger:
    """
    Knowledge & Insight Ledger holding all research findings,
    immutable sources, and actionable insights for future codebase versions.
    """
    ledger_id: str
    query: str
    topic_type: ResearchTopicType
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    sources: List[ResearchSource] = field(default_factory=list)
    insights: List[SourceInsight] = field(default_factory=list)
    summary_executive: str = ""

    def add_source(self, source: ResearchSource) -> None:
        # Avoid duplicate source URLs or IDs
        if not any(s.id == source.id or (s.url and s.url == source.url) for s in self.sources):
            self.sources.append(source)

    def add_insight(self, insight: SourceInsight) -> None:
        self.insights.append(insight)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ledger_id": self.ledger_id,
            "query": self.query,
            "topic_type": self.topic_type.value,
            "created_at": self.created_at,
            "summary_executive": self.summary_executive,
            "sources": [s.to_dict() for s in self.sources],
            "insights": [i.to_dict() for i in self.insights],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResearchLedger":
        sources = [ResearchSource.from_dict(s) for s in data.get("sources", [])]
        insights = [SourceInsight.from_dict(i) for i in data.get("insights", [])]
        return cls(
            ledger_id=data["ledger_id"],
            query=data["query"],
            topic_type=ResearchTopicType(data["topic_type"]),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            sources=sources,
            insights=insights,
            summary_executive=data.get("summary_executive", ""),
        )

    def to_markdown(self) -> str:
        """Renders an auditable Markdown report separating canonical evidence from trend signals."""
        topic_label = (
            "Conceito, Arquitetura e Melhores Práticas"
            if self.topic_type == ResearchTopicType.TOPIC_CONCEPT
            else "Mineração de Repositórios & Reúso de Código"
        )

        high_cred_sources = [s for s in self.sources if s.authority_tier == AuthorityTier.HIGH_CREDIBILITY]
        trend_sources = [s for s in self.sources if s.authority_tier == AuthorityTier.TREND_SIGNAL]

        md = [
            f"# Dossiê de Pesquisa & Knowledge Ledger: {self.ledger_id}",
            "",
            f"- **Consulta / Objetivo**: `{self.query}`",
            f"- **Tipo de Pesquisa**: {topic_label} (`{self.topic_type.value}`)",
            f"- **Data de Criação**: {self.created_at}",
            f"- **Total de Fontes Catalogadas**: {len(self.sources)} ({len(high_cred_sources)} alta credibilidade, {len(trend_sources)} tendências/experts)",
            f"- **Total de Insights Extraídos**: {len(self.insights)}",
            "",
            "## 1. Resumo Executivo dos Achados",
            self.summary_executive if self.summary_executive else "Nenhum resumo executivo gerado.",
            "",
            "## 2. Fontes de Alta Credibilidade (Papers, RFCs, Repositórios Testados)",
            "> Fundamentação técnica, integridade matemática e validação de arquitetura.",
            "",
            "| ID | Título | Tipo | Licença / Score | URL / Identificador |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ]

        if not high_cred_sources:
            md.append("| Nenhuma | Nenhuma fonte formal registrada | - | - | - |")
        else:
            for s in high_cred_sources:
                lic = s.license or "N/A"
                score_badge = f"{int(s.credibility_score * 100)}%"
                lic_score = f"{lic} (⭐ {s.stars})" if s.stars is not None else f"{lic} (Score: {score_badge})"
                md.append(f"| `{s.id}` | **{s.title}** | `{s.source_type}` | {lic_score} | [{s.url}]({s.url}) |")

        if trend_sources:
            md.extend([
                "",
                "## 3. Radar de Tendências & Experts da Comunidade (Ideação de Features)",
                "> 💡 Ampliação de escopo e recursos emergentes em alta. A arquitetura e segurança devem ser validadas nas fontes acima.",
                "",
                "| ID | Título / Discussão | Expert / Canal | Repercussão | URL Canônica |",
                "| :--- | :--- | :--- | :--- | :--- |",
            ])
            for s in trend_sources:
                author_str = ", ".join(s.authors_or_maintainers) if s.authors_or_maintainers else "Comunidade Tech"
                rep = s.metadata.get("points")
                rep_str = f"{rep} pts / {s.metadata.get('comments', 0)} comments" if rep else "Trending Signal"
                md.append(f"| `{s.id}` | **{s.title}** | {author_str} | {rep_str} | [{s.url}]({s.url}) |")

        md.extend([
            "",
            "## 4. Síntese de Insights Específicos por Fonte (Para Evolução do Código)",
            ""
        ])

        for i, ins in enumerate(self.insights, start=1):
            is_trend = ins.authority_tier == AuthorityTier.TREND_SIGNAL
            tier_badge = "🔥 [TENDÊNCIA / EXPERT - IDEAÇÃO DE FEATURE]" if is_trend else "🏛️ [ALTA CREDIBILIDADE - VALIDAÇÃO ARQUITETURAL]"
            md.extend([
                f"### {i}. {tier_badge} `{ins.source_id}` — {ins.source_title}",
                "",
                f"**💡 Insight Chave:**",
                ins.key_insight,
                "",
                f"**🏛️ Implicações Arquiteturais / Ideação de Features:**",
                ins.architectural_implications,
                "",
                f"**🔮 Valor de Referência Futura (Evolução de Features):**",
                ins.future_reference_value,
                "",
            ])
            if is_trend:
                md.extend([
                    "> ⚠️ **Diretriz DarkFac**: Ideia de feature extraída de tendência da comunidade. Requisitos técnicos, integridade e segurança devem ser validados nas fontes de alta credibilidade antes de codificar.",
                    ""
                ])
            if ins.code_patterns_or_algorithms:
                md.append("**🧩 Padrões de Código ou Algoritmos Identificados:**")
                for pat in ins.code_patterns_or_algorithms:
                    md.append(f"- `{pat}`")
                md.append("")

        return "\n".join(md)

