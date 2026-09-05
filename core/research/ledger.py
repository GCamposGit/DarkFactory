"""
Knowledge & Insight Ledger Manager.
Persists and retrieves auditable research records, source citations,
and actionable insights into the repository (.factory/research/<ledger-id>/).
"""

import os
import re
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from core.research.models import ResearchLedger, ResearchTopicType, ResearchSource, SourceInsight

DEFAULT_RESEARCH_DIR = Path(__file__).resolve().parent.parent.parent / ".factory" / "research"


def sanitize_slug(text: str) -> str:
    """Converts a query or title into a clean directory slug."""
    clean = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[-\s]+", "-", clean)[:60]


class KnowledgeLedgerManager:
    """Handles persistence and retrieval of research dossiers."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or DEFAULT_RESEARCH_DIR

    def create_ledger(
        self,
        query: str,
        topic_type: ResearchTopicType,
        ledger_id: Optional[str] = None
    ) -> ResearchLedger:
        """Initializes a new ResearchLedger instance."""
        if not ledger_id:
            now_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            slug = sanitize_slug(query) or "research"
            ledger_id = f"{now_str}_{slug}"

        return ResearchLedger(
            ledger_id=ledger_id,
            query=query,
            topic_type=topic_type,
            created_at=datetime.now(timezone.utc).isoformat()
        )

    def save_ledger(self, ledger: ResearchLedger) -> Path:
        """
        Saves the ledger both as a structured JSON and as an auditable INSIGHTS.md.
        Returns the directory path where it was saved.
        """
        target_dir = self.base_dir / ledger.ledger_id
        target_dir.mkdir(parents=True, exist_ok=True)

        json_path = target_dir / "ledger.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(ledger.to_dict(), f, indent=2, ensure_ascii=False)

        md_path = target_dir / "INSIGHTS.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(ledger.to_markdown())

        return target_dir

    def load_ledger(self, ledger_id: str) -> Optional[ResearchLedger]:
        """Loads a ResearchLedger from disk by its ID."""
        json_path = self.base_dir / ledger_id / "ledger.json"
        if not json_path.exists():
            return None

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return ResearchLedger.from_dict(data)

    def list_ledgers(self) -> List[Dict[str, Any]]:
        """Lists all existing research dossiers in the repository."""
        if not self.base_dir.exists():
            return []

        results = []
        for entry in self.base_dir.iterdir():
            if entry.is_dir():
                json_path = entry / "ledger.json"
                if json_path.exists():
                    try:
                        with open(json_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            results.append({
                                "ledger_id": data.get("ledger_id", entry.name),
                                "query": data.get("query", ""),
                                "topic_type": data.get("topic_type", ""),
                                "created_at": data.get("created_at", ""),
                                "sources_count": len(data.get("sources", [])),
                                "insights_count": len(data.get("insights", [])),
                                "path": str(entry)
                            })
                    except Exception:
                        continue

        results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return results

    @staticmethod
    def generate_code_header_reference(ledger_id: str) -> str:
        """Returns header comments to link newly written code to its foundational research."""
        return (
            f"# [RESEARCH PROVENANCE & INSIGHTS]\n"
            f"# Ledger ID: {ledger_id}\n"
            f"# Audit Doc: .factory/research/{ledger_id}/INSIGHTS.md\n"
            f"# Canonical Sources: .factory/research/{ledger_id}/ledger.json\n"
        )
