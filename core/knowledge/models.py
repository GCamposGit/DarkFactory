"""Pydantic v2 data contracts for Dark Factory Knowledge Subsystem (HF-22)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, field_validator


class KnowledgeQuery(BaseModel):
    """Specification of a semantic/lexical knowledge retrieval query."""

    query: str = Field(..., min_length=1, description="Natural language question or search phrase.")
    project_id: str = Field("darkfac", description="Project namespace filter (e.g. darkfac, atrium, jarvis, shared).")
    k: int = Field(5, ge=1, le=50, description="Maximum number of passages to return.")
    min_score: float = Field(
        0.55,
        ge=0.0,
        le=1.0,
        description="Minimum relevance score threshold. Results below this are strictly discarded (Fail-Closed).",
    )
    include_neighbors: bool = Field(True, description="Whether to fetch adjacent context chunks for continuity.")
    root_folder: Optional[str] = Field(None, description="Optional subfolder constraint within the project corpus.")


class KnowledgeCitation(BaseModel):
    """Verifiable passage citation with full provenance details (Anti-Hallucination)."""

    id: str = Field(..., description="Stable chunk identifier.")
    file_path: str = Field(..., description="Canonical path of the source document.")
    section: str = Field("", description="Heading trail or logical section in the document.")
    locator: str = Field("", description="Specific locator (e.g. page, slide, sheet, or row).")
    content: str = Field(..., description="Raw text content extracted from the source.")
    score: float = Field(..., description="Normalized hybrid retrieval/similarity score.")
    provenance_hash: str = Field(..., description="SHA-256 hash of the content verifying data integrity.")
    project_id: str = Field("shared", description="Project namespace the source document belongs to.")
    audio_timestamp_sec: Optional[float] = Field(None, description="Start timestamp in seconds if source is audio.")
    line_start: Optional[int] = Field(None, description="Starting line in source text if applicable.")
    line_end: Optional[int] = Field(None, description="Ending line in source text if applicable.")


class KnowledgeQueryResult(BaseModel):
    """Outcome of a knowledge query with strict fail-closed guarantees."""

    status: Literal["FOUND", "INSUFFICIENT_EVIDENCE"] = Field(
        ...,
        description="FOUND if one or more citations passed min_score; INSUFFICIENT_EVIDENCE otherwise.",
    )
    query: str = Field(..., description="Original search query.")
    project_id: str = Field(..., description="Target project scope requested.")
    citations: List[KnowledgeCitation] = Field(
        default_factory=list,
        description="List of verified citations passing min_score.",
    )
    total_found: int = Field(0, description="Count of passages discovered before filtering.")
    execution_time_ms: float = Field(0.0, description="Retrieval latency in milliseconds.")
    engine: str = Field("segundocerebro_mcp", description="Name of the underlying retrieval engine.")


class IngestionRequest(BaseModel):
    """Request to ingest a new document into a project's knowledge base."""

    file_path: Path = Field(..., description="Absolute or relative path to the file to be ingested.")
    project_id: str = Field("darkfac", description="Target project namespace (darkfac, atrium, jarvis, shared).")
    format: Literal["docx", "xlsx", "pptx", "pdf", "md", "txt", "audio"] = Field(
        ...,
        description="Declared file format type.",
    )
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary user or system metadata.")

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, v: Path) -> Path:
        return Path(v)


class IngestionResult(BaseModel):
    """Result of an ingestion operation."""

    success: bool = Field(..., description="True if document was accepted and staged/indexed.")
    file_path: str = Field(..., description="Path of the processed document.")
    project_id: str = Field(..., description="Assigned project namespace.")
    staged_path: Optional[str] = Field(None, description="Destination path in Segundo Cérebro corpus.")
    file_hash: Optional[str] = Field(None, description="SHA-256 hash of the ingested file.")
    error_message: Optional[str] = Field(None, description="Error explanation if success is False.")


class SecondBrainStatus(BaseModel):
    """Health and connectivity status of the Segundo Cérebro MCP server."""

    available: bool = Field(..., description="Whether the MCP server is responsive.")
    mcp_endpoint: str = Field(..., description="Executable command or connection string.")
    registered_tools: List[str] = Field(default_factory=list, description="List of tools exposed by MCP server.")
    corpus_root: Optional[str] = Field(None, description="Root directory of the indexed corpus.")
    error_message: Optional[str] = Field(None, description="Diagnosis if unavailable.")
