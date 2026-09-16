"""Document and Office Ingestion Dispatcher for Segundo Cérebro (HF-22).

Handles validation, staging, and dispatch of Office files (.docx, .xlsx, .pptx),
documents (.pdf, .md, .txt), and audio transcripts into project corpus partitions.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from .models import IngestionRequest, IngestionResult

logger = logging.getLogger("darkfac.knowledge.ingestor")

SUPPORTED_EXTENSIONS = {
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".pptx": "pptx",
    ".pdf": "pdf",
    ".md": "md",
    ".txt": "txt",
    ".json": "txt",
    ".wav": "audio",
    ".mp3": "audio",
    ".m4a": "audio",
}


class KnowledgeIngestor:
    """Manages document validation, hash computation, and staging into project corpora."""

    def __init__(self, corpus_root: Optional[Path] = None) -> None:
        default_root = Path(os.getenv("SEGUNDO_CEREBRO_CORPUS", r"C:\dev\SegundoCerebro\corpus"))
        self.corpus_root = corpus_root or default_root

    def _compute_hash(self, file_path: Path) -> str:
        """Compute SHA-256 digest of file contents."""
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()

    def ingest(self, request: IngestionRequest) -> IngestionResult:
        """Validate, stage, and register document in the appropriate project namespace."""
        path = Path(request.file_path).resolve()
        if not path.exists():
            return IngestionResult(
                success=False,
                file_path=str(path),
                project_id=request.project_id,
                error_message=f"File '{path}' does not exist.",
            )

        if not path.is_file():
            return IngestionResult(
                success=False,
                file_path=str(path),
                project_id=request.project_id,
                error_message=f"Target '{path}' is not a regular file.",
            )

        ext = path.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            return IngestionResult(
                success=False,
                file_path=str(path),
                project_id=request.project_id,
                error_message=(
                    f"Unsupported extension '{ext}'. Supported formats: "
                    f"{', '.join(sorted(SUPPORTED_EXTENSIONS.keys()))}"
                ),
            )

        file_size = path.stat().st_size
        if file_size == 0:
            return IngestionResult(
                success=False,
                file_path=str(path),
                project_id=request.project_id,
                error_message="Cannot ingest empty file (0 bytes).",
            )

        try:
            file_hash = self._compute_hash(path)

            # Destination staged directory per project
            dest_dir = self.corpus_root / request.project_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_file = dest_dir / path.name

            # Copy file to project corpus partition if source and dest are different
            if path != dest_file.resolve():
                shutil.copy2(path, dest_file)

            logger.info(
                "Ingested %s (%s, %d bytes) into corpus partition '%s'",
                path.name,
                request.format,
                file_size,
                request.project_id,
            )

            return IngestionResult(
                success=True,
                file_path=str(path),
                project_id=request.project_id,
                staged_path=str(dest_file),
                file_hash=file_hash,
                error_message=None,
            )

        except Exception as exc:
            logger.error("Failed to ingest %s: %s", path, exc)
            return IngestionResult(
                success=False,
                file_path=str(path),
                project_id=request.project_id,
                error_message=str(exc),
            )
