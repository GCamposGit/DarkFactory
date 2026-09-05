"""
DarkFac Research Engine (2026 Edition)
Autonomous dual-mode research pipeline:
1. Topic Deep Research (Scientific papers, academic graphs, architectural concepts)
2. Repo Code Scout (High-quality open-source codebases, test suites, permissive licenses)
Persists findings to an auditable Knowledge & Insight Ledger.
"""

from core.research.models import (
    ResearchTopicType,
    AuthorityTier,
    ResearchSource,
    SourceInsight,
    ResearchLedger,
)
from core.research.classifier import classify_research_intent
from core.research.arxiv_client import ArxivClient
from core.research.github_scout import GitHubScout
from core.research.trends_scout import ExpertTrendsScout
from core.research.ledger import KnowledgeLedgerManager

__all__ = [
    "ResearchTopicType",
    "AuthorityTier",
    "ResearchSource",
    "SourceInsight",
    "ResearchLedger",
    "classify_research_intent",
    "ArxivClient",
    "GitHubScout",
    "ExpertTrendsScout",
    "KnowledgeLedgerManager",
]

