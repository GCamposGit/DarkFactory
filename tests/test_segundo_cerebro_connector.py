"""Tests for Dark Factory Knowledge Subsystem — Segundo Cérebro MCP Integration (HF-22)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import pytest
from pydantic import ValidationError

from core.knowledge import (
    IngestionRequest,
    IngestionResult,
    KnowledgeCitation,
    KnowledgeIngestor,
    KnowledgeQuery,
    KnowledgeQueryResult,
    SecondBrainStatus,
    SegundoCerebroClient,
)
from core.knowledge.cli import main as cli_main


# 1. Pydantic v2 Contracts
def test_knowledge_contracts_validation():
    query = KnowledgeQuery(
        query="Como funciona a governança de branches?",
        project_id="darkfac",
        k=3,
        min_score=0.7,
    )
    assert query.query == "Como funciona a governança de branches?"
    assert query.project_id == "darkfac"
    assert query.k == 3
    assert query.min_score == 0.7

    with pytest.raises(ValidationError):
        # min_score cannot exceed 1.0
        KnowledgeQuery(query="test", min_score=1.5)

    with pytest.raises(ValidationError):
        # empty query rejected
        KnowledgeQuery(query="")


def test_knowledge_citation_hash_and_model():
    content = "A governança de branches na Dark Factory exige worktree isolada."
    expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    citation = KnowledgeCitation(
        id="chk-001",
        file_path="darkfac/FACTORY_RULES.md",
        section="Governança de Branches",
        locator="p.2",
        content=content,
        score=0.92,
        provenance_hash=expected_hash,
        project_id="darkfac",
    )
    assert citation.provenance_hash == expected_hash
    assert citation.project_id == "darkfac"
    assert citation.score == 0.92


# 2. Strict Fail-Closed (Anti-Hallucination) Retrieval
def test_fail_closed_threshold_enforcement():
    client = SegundoCerebroClient(mock_mode=True)
    client.set_mock_corpus([
        {
            "id": "doc-01",
            "arquivo": "shared/normas.md",
            "texto": "Diretriz de segurança e arquitetura resiliente.",
            "score": 0.50,  # Below threshold
        },
        {
            "id": "doc-02",
            "arquivo": "darkfac/ARCHITECTURE.md",
            "texto": "Monólito modular Python e FastAPI com supervisão durável.",
            "score": 0.85,  # Above threshold
        },
    ])

    # Query with min_score=0.60
    res = client.search(
        KnowledgeQuery(
            query="arquitetura",
            project_id="darkfac",
            min_score=0.60,
        )
    )

    assert res.status == "FOUND"
    assert len(res.citations) == 1
    assert res.citations[0].id == "doc-02"
    assert res.citations[0].score == 0.85

    # Query with high min_score=0.90 -> strict fail-closed
    res_strict = client.search(
        KnowledgeQuery(
            query="arquitetura",
            project_id="darkfac",
            min_score=0.90,
        )
    )

    assert res_strict.status == "INSUFFICIENT_EVIDENCE"
    assert len(res_strict.citations) == 0
    assert res_strict.total_found == 2  # Discovered, but zero passed criteria


# 3. Multi-Project Segregation
def test_multi_project_segregation():
    client = SegundoCerebroClient(mock_mode=True)
    client.set_mock_corpus([
        {
            "id": "chk-df",
            "arquivo": "darkfac/secrets.md",
            "texto": "Token de infraestrutura DarkFac.",
            "score": 0.90,
        },
        {
            "id": "chk-jv",
            "arquivo": "jarvis/notes.md",
            "texto": "Configuração do assistente Jarvis de áudio.",
            "score": 0.88,
        },
        {
            "id": "chk-sh",
            "arquivo": "shared/glossario.md",
            "texto": "Glossário técnico compartilhado da fábrica.",
            "score": 0.80,
        },
    ])

    # Jarvis querying: cannot see DarkFac private documents, can see Jarvis and Shared
    res_jarvis = client.search(
        KnowledgeQuery(
            query="configuração",
            project_id="jarvis",
            min_score=0.50,
        )
    )

    citation_ids = [c.id for c in res_jarvis.citations]
    assert "chk-jv" in citation_ids
    assert "chk-sh" in citation_ids
    assert "chk-df" not in citation_ids  # Segregated!

    # Atrium querying: cannot see DarkFac or Jarvis private documents
    res_atrium = client.search(
        KnowledgeQuery(
            query="token",
            project_id="atrium",
            min_score=0.50,
        )
    )
    citation_ids_atrium = [c.id for c in res_atrium.citations]
    assert "chk-df" not in citation_ids_atrium
    assert "chk-jv" not in citation_ids_atrium


# 4. Office & Document Ingestion
def test_office_and_document_ingestion(tmp_path: Path):
    corpus_dir = tmp_path / "corpus"
    ingestor = KnowledgeIngestor(corpus_root=corpus_dir)

    # 4.1 Ingest valid .docx and .xlsx and .pptx
    docx_file = tmp_path / "Briefing.docx"
    docx_file.write_bytes(b"PK\x03\x04 fake docx binary data for test")

    res_docx = ingestor.ingest(
        IngestionRequest(
            file_path=docx_file,
            project_id="atrium",
            format="docx",
        )
    )
    assert res_docx.success is True
    assert res_docx.project_id == "atrium"
    assert res_docx.file_hash is not None
    assert (corpus_dir / "atrium" / "Briefing.docx").exists()

    # 4.2 Ingest valid .xlsx
    xlsx_file = tmp_path / "Orcamento.xlsx"
    xlsx_file.write_bytes(b"PK\x03\x04 fake xlsx spreadsheet content")

    res_xlsx = ingestor.ingest(
        IngestionRequest(
            file_path=xlsx_file,
            project_id="jarvis",
            format="xlsx",
        )
    )
    assert res_xlsx.success is True
    assert (corpus_dir / "jarvis" / "Orcamento.xlsx").exists()

    # 4.3 Ingest valid .pptx
    pptx_file = tmp_path / "Pitch.pptx"
    pptx_file.write_bytes(b"PK\x03\x04 fake pptx slide deck")

    res_pptx = ingestor.ingest(
        IngestionRequest(
            file_path=pptx_file,
            project_id="darkfac",
            format="pptx",
        )
    )
    assert res_pptx.success is True
    assert (corpus_dir / "darkfac" / "Pitch.pptx").exists()

    # 4.4 Reject empty file
    empty_file = tmp_path / "vazio.pdf"
    empty_file.touch()

    res_empty = ingestor.ingest(
        IngestionRequest(
            file_path=empty_file,
            project_id="darkfac",
            format="pdf",
        )
    )
    assert res_empty.success is False
    assert "empty file" in res_empty.error_message.lower()

    # 4.5 Reject unsupported file format (.exe)
    bad_file = tmp_path / "malware.exe"
    bad_file.write_bytes(b"MZ binary")

    with pytest.raises(ValidationError):
        # Pydantic validates format Literal
        IngestionRequest(
            file_path=bad_file,
            project_id="darkfac",
            format="exe",  # type: ignore
        )


# 5. Live Server Detection & Health Check
def test_segundo_cerebro_health_check():
    client = SegundoCerebroClient()
    status = client.check_health()
    assert isinstance(status, SecondBrainStatus)
    # The actual C:\dev\SegundoCerebro exists on this machine
    if Path(r"C:\dev\SegundoCerebro").exists():
        assert status.available is True
        assert "search" in status.registered_tools
        assert "read_note" in status.registered_tools
        assert "neighbors" in status.registered_tools


# 6. Headless CLI Reachability
def test_cli_headless_status(capsys):
    ret = cli_main(["status", "--json"])
    assert ret in (0, 1)
    captured = capsys.readouterr().out
    data = json.loads(captured)
    assert "available" in data
    assert "registered_tools" in data


def test_cli_headless_search(capsys, monkeypatch):
    # Use mock environment for deterministic fast CLI test
    monkeypatch.setenv("DARKFAC_KNOWLEDGE_MOCK", "1")
    ret = cli_main(["search", "governança", "--project", "darkfac", "--json"])
    assert ret == 0
    captured = capsys.readouterr().out
    data = json.loads(captured)
    assert "status" in data
    assert "citations" in data
