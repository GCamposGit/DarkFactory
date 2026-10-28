"""
Knowledge & Insight Ledger Manager.
Persists and retrieves auditable research records, source citations,
and actionable insights into the repository (.factory/research/<ledger-id>/).
"""

import os
import re
import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from core.research.models import ResearchLedger, ResearchTopicType, ResearchSource, SourceInsight
from core.research.compat import normalize_ledger
from core.paths import project_root

DEFAULT_RESEARCH_DIR = project_root() / ".factory" / "research"
logger = logging.getLogger(__name__)


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
            return normalize_ledger(data, requested_ledger_id=ledger_id)

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
                            ledger = normalize_ledger(data, requested_ledger_id=entry.name)
                            results.append({
                                "ledger_id": ledger.ledger_id,
                                "query": ledger.query,
                                "topic_type": ledger.topic_type.value,
                                "created_at": ledger.created_at,
                                "sources_count": len(ledger.sources),
                                "insights_count": len(ledger.insights),
                                "path": str(entry)
                            })
                    except Exception as exc:  # noqa: BLE001 - isolate malformed historical entries
                        field = getattr(exc, "field", "$")
                        reason = getattr(exc, "reason", "invalid ledger record")
                        logger.warning(
                            "invalid research ledger skipped",
                            extra={"ledger_id": entry.name, "field": field, "reason": reason},
                        )
                        continue

        results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return results

    def migrate_ledgers(self, ledger_id: Optional[str] = None, apply: bool = False) -> Dict[str, Any]:
        """Report or migrate ledgers that normalize to a different canonical schema.

        The default is read-only. Invalid entries are isolated so one malformed
        ledger cannot prevent valid ledgers from being migrated.
        """
        errors: List[Dict[str, str]] = []
        if ledger_id is not None:
            selected = self.base_dir / ledger_id
            if selected.resolve().parent != self.base_dir.resolve():
                return {
                    "applied": apply,
                    "ledgers": [],
                    "errors": [{
                        "ledger_id": ledger_id,
                        "field": "ledger_id",
                        "reason": "ledger ID must name a direct child of the research directory",
                    }],
                }
            directories = [selected]
        elif self.base_dir.exists():
            directories = sorted(
                (entry for entry in self.base_dir.iterdir() if entry.is_dir()),
                key=lambda entry: entry.name,
            )
        else:
            directories = []

        historical: List[Dict[str, Any]] = []
        for directory in directories:
            json_path = directory / "ledger.json"
            if not json_path.is_file():
                continue
            try:
                with json_path.open("r", encoding="utf-8") as source_file:
                    raw = json.load(source_file)
                if not isinstance(raw, dict):
                    raise ValueError("ledger root must be an object")
                ledger = normalize_ledger(raw, requested_ledger_id=directory.name)
            except Exception as exc:  # noqa: BLE001 - migration isolates invalid records
                field = getattr(exc, "field", "$")
                reason = getattr(exc, "reason", None)
                if not reason:
                    reason = (
                        "invalid JSON"
                        if isinstance(exc, json.JSONDecodeError)
                        else "ledger could not be normalized"
                    )
                errors.append({"ledger_id": directory.name, "field": str(field), "reason": str(reason)})
                continue

            canonical = ledger.to_dict()
            if raw == canonical:
                continue

            row: Dict[str, Any] = {
                "ledger_id": ledger.ledger_id,
                "status": "historical",
                "normalization": {
                    "changed_fields": _changed_fields(raw, canonical),
                    "sources": len(ledger.sources),
                    "insights": len(ledger.insights),
                },
            }
            if apply:
                self.save_ledger(ledger)
                row["status"] = "migrated"
            historical.append(row)

        return {"applied": apply, "ledgers": historical, "errors": errors}

    @staticmethod
    def generate_code_header_reference(ledger_id: str) -> str:
        """Returns header comments to link newly written code to its foundational research."""
        return (
            f"# [RESEARCH PROVENANCE & INSIGHTS]\n"
            f"# Ledger ID: {ledger_id}\n"
            f"# Audit Doc: .factory/research/{ledger_id}/INSIGHTS.md\n"
            f"# Canonical Sources: .factory/research/{ledger_id}/ledger.json\n"
        )


def _changed_fields(before: Any, after: Any, prefix: str = "") -> List[str]:
    """Return paths changed by normalization without including potentially secret values."""
    if isinstance(before, dict) and isinstance(after, dict):
        paths: List[str] = []
        for key in sorted(set(before) | set(after)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                paths.append(path)
            else:
                paths.extend(_changed_fields(before[key], after[key], path))
        return paths
    if isinstance(before, list) and isinstance(after, list):
        paths = []
        for index in range(max(len(before), len(after))):
            path = f"{prefix}[{index}]"
            if index >= len(before) or index >= len(after):
                paths.append(path)
            else:
                paths.extend(_changed_fields(before[index], after[index], path))
        return paths
    return [] if before == after else [prefix]
