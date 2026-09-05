"""
Persistence layer for Session Learning Packs.
Stores packs as JSON, Markdown, HTML, and Anki TSV in `.factory/learning_packs/`.
Maintains an index for instant querying by DarkHub and CLI.
"""

import os
import json
from pathlib import Path
from typing import List, Dict, Any, Optional

from core.learning_pack.models import SessionLearningPack
from core.learning_pack.renderer import LearningPackRenderer

_ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_PACKS_DIR = _ROOT_DIR / ".factory" / "learning_packs"
INDEX_FILE_NAME = "packs_index.json"


class LearningPackStore:
    """Manages reading and writing of session learning packs."""

    def __init__(self, storage_dir: Optional[Path] = None):
        self.storage_dir = storage_dir or DEFAULT_PACKS_DIR
        self.index_file = self.storage_dir / INDEX_FILE_NAME
        self._ensure_storage_exists()

    def _ensure_storage_exists(self) -> None:
        os.makedirs(self.storage_dir, exist_ok=True)
        if not self.index_file.exists():
            self._write_index([])

    def _read_index(self) -> List[Dict[str, Any]]:
        if not self.index_file.exists():
            return []
        try:
            with open(self.index_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _write_index(self, index_data: List[Dict[str, Any]]) -> None:
        with open(self.index_file, "w", encoding="utf-8") as f:
            json.dump(index_data, f, indent=2, ensure_ascii=False)

    def save_pack(self, pack: SessionLearningPack) -> Dict[str, str]:
        """
        Saves the learning pack as JSON, Markdown, standalone HTML, and Anki TSV.
        Updates the index file atomically.
        """
        self._ensure_storage_exists()
        base_name = f"{pack.pack_id}"

        json_path = self.storage_dir / f"{base_name}.json"
        md_path = self.storage_dir / f"{base_name}.md"
        html_path = self.storage_dir / f"{base_name}.html"
        anki_path = self.storage_dir / f"{base_name}_anki.tsv"

        # 1. Save JSON
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(pack.to_dict(), f, indent=2, ensure_ascii=False)

        # 2. Save Markdown
        md_content = LearningPackRenderer.render_markdown(pack)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)

        # 3. Save HTML
        html_content = LearningPackRenderer.render_html(pack)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        # 4. Save Anki TSV
        anki_content = LearningPackRenderer.render_anki_tsv(pack)
        with open(anki_path, "w", encoding="utf-8") as f:
            f.write(anki_content)

        # 5. Update index
        index = self._read_index()
        # Remove previous entry for same pack_id if exists
        index = [item for item in index if item.get("pack_id") != pack.pack_id]

        index_entry = {
            "pack_id": pack.pack_id,
            "session_id": pack.session_id,
            "timestamp": pack.timestamp,
            "title": pack.title,
            "executive_summary": pack.executive_summary[:180] + ("..." if len(pack.executive_summary) > 180 else ""),
            "concepts_count": len(pack.concepts),
            "flashcards_count": len(pack.flashcards),
            "concept_names": [c.name for c in pack.concepts],
            "file_json": json_path.name,
            "file_md": md_path.name,
            "file_html": html_path.name,
            "file_anki": anki_path.name,
        }
        # Prepend to keep latest first
        index.insert(0, index_entry)
        self._write_index(index)

        return {
            "json": str(json_path),
            "md": str(md_path),
            "html": str(html_path),
            "anki": str(anki_path),
        }

    def load_pack(self, pack_id: str) -> Optional[SessionLearningPack]:
        """Loads a SessionLearningPack by its pack_id or 'latest'."""
        if pack_id == "latest":
            return self.get_latest_pack()

        json_path = self.storage_dir / f"{pack_id}.json"
        if not json_path.exists():
            return None

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return SessionLearningPack.from_dict(data)
        except Exception:
            return None

    def get_latest_pack(self) -> Optional[SessionLearningPack]:
        """Returns the most recent learning pack."""
        index = self._read_index()
        if not index:
            return None
        latest_id = index[0]["pack_id"]
        return self.load_pack(latest_id)

    def list_packs(self) -> List[Dict[str, Any]]:
        """Returns the metadata index of all saved learning packs."""
        return self._read_index()
