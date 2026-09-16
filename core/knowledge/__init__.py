"""Dark Factory Knowledge Subsystem — Segundo Cérebro MCP Integration (HF-22)."""

from .models import (
    IngestionRequest,
    IngestionResult,
    KnowledgeCitation,
    KnowledgeQuery,
    KnowledgeQueryResult,
    SecondBrainStatus,
)
from .ingestor import KnowledgeIngestor
from .segundo_cerebro_client import SegundoCerebroClient

__all__ = [
    "IngestionRequest",
    "IngestionResult",
    "KnowledgeCitation",
    "KnowledgeIngestor",
    "KnowledgeQuery",
    "KnowledgeQueryResult",
    "SecondBrainStatus",
    "SegundoCerebroClient",
]
