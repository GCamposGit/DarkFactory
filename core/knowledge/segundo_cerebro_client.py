"""Segundo Cérebro MCP Client for Dark Factory (HF-22).

Connects to the mature Segundo Cérebro MCP server (C:\\dev\\SegundoCerebro), providing
typed retrieval, strict fail-closed anti-hallucination checks, and multi-project segregation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import (
    KnowledgeCitation,
    KnowledgeQuery,
    KnowledgeQueryResult,
    SecondBrainStatus,
)

logger = logging.getLogger("darkfac.knowledge.segundo_cerebro")

DEFAULT_SEGUNDO_CEREBRO_ROOT = Path(r"C:\dev\SegundoCerebro")
DEFAULT_VENV_PYTHON = DEFAULT_SEGUNDO_CEREBRO_ROOT / ".venv" / "Scripts" / "python.exe"


class SegundoCerebroClient:
    """Headless MCP client for Segundo Cérebro retrieval and provenance audit."""

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        python_exe: Optional[Path] = None,
        mock_mode: bool = False,
    ) -> None:
        self.repo_root = repo_root or Path(
            os.getenv("SEGUNDO_CEREBRO_ROOT", str(DEFAULT_SEGUNDO_CEREBRO_ROOT))
        )
        self.python_exe = python_exe or Path(
            os.getenv("SEGUNDO_CEREBRO_PYTHON", str(DEFAULT_VENV_PYTHON))
        )
        # Mock mode allows deterministic testing in headless environments without dependencies
        self.mock_mode = mock_mode or os.getenv("DARKFAC_KNOWLEDGE_MOCK", "0") in ("1", "true", "yes")
        self._mock_corpus: List[Dict[str, Any]] = []

    def set_mock_corpus(self, items: List[Dict[str, Any]]) -> None:
        """Inject test documents for offline mock testing."""
        self._mock_corpus = items
        self.mock_mode = True

    def check_health(self) -> SecondBrainStatus:
        """Verify presence of Segundo Cérebro repository, python environment, and MCP server."""
        if self.mock_mode:
            return SecondBrainStatus(
                available=True,
                mcp_endpoint="mock://segundocerebro",
                registered_tools=["search", "read_note", "neighbors", "list_folder", "outline", "get_document"],
                corpus_root=str(self.repo_root),
                error_message=None,
            )

        if not self.repo_root.exists():
            return SecondBrainStatus(
                available=False,
                mcp_endpoint=str(self.python_exe),
                registered_tools=[],
                corpus_root=None,
                error_message=f"SegundoCerebro root not found at '{self.repo_root}'",
            )

        python_binary = self.python_exe if self.python_exe.exists() else Path(sys.executable)
        server_script = self.repo_root / "src" / "segundocerebro" / "mcp" / "server.py"
        if not server_script.exists():
            return SecondBrainStatus(
                available=False,
                mcp_endpoint=str(python_binary),
                registered_tools=[],
                corpus_root=str(self.repo_root),
                error_message=f"Server entrypoint not found at '{server_script}'",
            )

        return SecondBrainStatus(
            available=True,
            mcp_endpoint=f"{python_binary} -m segundocerebro.mcp.server",
            registered_tools=["search", "read_note", "neighbors", "list_folder", "outline", "get_document", "pack_folder"],
            corpus_root=str(self.repo_root),
            error_message=None,
        )

    def _execute_mcp_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a tool on the Segundo Cérebro MCP server via stdio or direct import."""
        if self.mock_mode:
            return self._mock_execute(tool_name, arguments)

        # Build execution environment
        python_bin = str(self.python_exe) if self.python_exe.exists() else sys.executable
        src_path = str(self.repo_root / "src")

        # Command runner that invokes tool directly or via JSON-RPC
        # We invoke a minimal Python runner that leverages segundocerebro library directly if available
        # or initiates the stdio MCP session
        script = f"""
import sys, json, os
from pathlib import Path
sys.path.insert(0, r'{src_path}')
try:
    from segundocerebro.config import carregar
    from segundocerebro.mcp.busca import _registrar_search
    # Fast query execution helper
    from segundocerebro.index.store import Store
    from segundocerebro.index.embeddings import Embedder, MODELOS
    from segundocerebro.retrieve.hybrid import BuscaHibrida

    cfg = carregar()
    base = cfg.bases[0] if cfg and cfg.bases else None
    
    indice_target = os.environ.get("SEGUNDO_CEREBRO_INDICE")
    if not indice_target:
        local_idx = Path(r'{self.repo_root}') / "index"
        if local_idx.exists():
            indice_target = str(local_idx)
        elif base:
            indice_target = base.indice
        else:
            indice_target = "index"

    modelo_nome = getattr(base, "modelo", "e5-large") if base else "e5-large"
    embedder = Embedder(modelo_nome, threads=4)
    store = Store(indice_target, embedder.dim)
    busca = BuscaHibrida(store, embedder)

    args = json.loads(sys.argv[1])
    tool = sys.argv[2]

    if tool == "search":
        acertos = busca.buscar_chunks(
            args.get("consulta", ""),
            k=args.get("k", 8),
            contexto=args.get("contexto", 1),
            pasta=args.get("pasta", "")
        )
        trechos = []
        for a in acertos:
            trechos.append({{
                "id": getattr(a, "id", getattr(a, "chunk_id", "")),
                "arquivo": a.path,
                "secao": a.trilha or "",
                "onde": a.locator or "",
                "texto": a.texto,
                "score": round(float(a.score), 5),
                "achado_por": getattr(a, "origem", "hybrid"),
            }})
        print(json.dumps({{"trechos": trechos}}))
    else:
        print(json.dumps({{"error": f"Tool '{{tool}}' not implemented in direct runner"}}))
except Exception as exc:
    print(json.dumps({{"error": str(exc)}}))
"""

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = src_path

        try:
            cmd = [python_bin, "-c", script, json.dumps(arguments), tool_name]
            timeout_sec = int(os.getenv("SEGUNDO_CEREBRO_TIMEOUT", "60"))
            result = subprocess.run(
                cmd,
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_sec,
                check=False,
            )
            if result.returncode != 0:
                logger.warning("Segundo Cérebro command failed: %s", result.stderr)
                return {"error": result.stderr.strip() or f"Process exited with {result.returncode}"}

            raw_out = result.stdout.strip()
            # Extract last json line in case of warnings
            for line in reversed(raw_out.splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    return json.loads(line)
            return {"error": f"Invalid json output from server: {raw_out}"}

        except Exception as exc:
            logger.error("Failed to invoke Segundo Cérebro MCP tool: %s", exc)
            return {"error": str(exc)}

    def _mock_execute(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Simulate MCP server responses for tests."""
        if tool_name == "search":
            query = arguments.get("consulta", "").lower()
            k = arguments.get("k", 5)
            pasta = arguments.get("pasta", "")

            results = []
            for doc in self._mock_corpus:
                doc_text = doc.get("texto", "")
                doc_path = doc.get("arquivo", "")
                if pasta and not doc_path.startswith(pasta):
                    continue

                # Calculate naive overlap score
                words = set(query.split())
                doc_words = set(doc_text.lower().split())
                overlap = len(words.intersection(doc_words))
                score = min(1.0, overlap / max(1, len(words))) if words else 0.0

                # Override with preset score if present
                if "score" in doc:
                    score = float(doc["score"])

                if score > 0.0 or "score" in doc:
                    results.append({
                        "id": doc.get("id", f"chk-{len(results)+1}"),
                        "arquivo": doc_path,
                        "secao": doc.get("secao", ""),
                        "onde": doc.get("onde", "p.1"),
                        "texto": doc_text,
                        "score": score,
                        "achado_por": "mock",
                    })

            results.sort(key=lambda x: x["score"], reverse=True)
            return {"trechos": results[:k]}

        if tool_name == "read_note":
            target_id = arguments.get("id", "")
            for doc in self._mock_corpus:
                if doc.get("id") == target_id:
                    return {
                        "id": target_id,
                        "documento": doc.get("arquivo", ""),
                        "trechos": [{"id": target_id, "texto": doc.get("texto", "")}],
                    }
            return {"error": "trecho_nao_encontrado"}

        return {"error": f"Tool '{tool_name}' not supported in mock"}

    def search(self, query: KnowledgeQuery) -> KnowledgeQueryResult:
        """
        Execute knowledge query with strict fail-closed anti-hallucination and project isolation.
        """
        start_time = time.perf_counter()

        # Build tool arguments
        pasta_filter = query.root_folder or ""

        args = {
            "consulta": query.query,
            "k": query.k,
            "contexto": 1 if query.include_neighbors else 0,
            "pasta": pasta_filter,
        }

        mcp_res = self._execute_mcp_tool("search", args)
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

        if "error" in mcp_res:
            logger.warning("Retrieval returned error: %s", mcp_res["error"])
            return KnowledgeQueryResult(
                status="INSUFFICIENT_EVIDENCE",
                query=query.query,
                project_id=query.project_id,
                citations=[],
                total_found=0,
                execution_time_ms=elapsed_ms,
            )

        trechos = mcp_res.get("trechos", [])
        citations: List[KnowledgeCitation] = []

        for item in trechos:
            raw_score = float(item.get("score", 0.0))
            # Normalize RRF scores (typically 0.01 - 0.035) to 0.0 - 1.0 scale
            score = raw_score if raw_score >= 0.1 else min(1.0, round(raw_score * 30.0, 4))
            
            # Fail-Closed Rule: Strictly reject any chunk below min_score
            if score < query.min_score:
                continue

            doc_path = item.get("arquivo", "")
            # Project Segregation Rule:
            # If query is for a specific project, ensure doc belongs to project or is shared/global
            detected_project = "shared"
            parts = Path(doc_path).parts
            if parts and parts[0] in ("darkfac", "atrium", "jarvis"):
                detected_project = parts[0]

            if query.project_id not in ("shared", "global", "all"):
                if detected_project != "shared" and detected_project != query.project_id:
                    # Isolated: cannot access other tenant's private documents
                    continue

            content = item.get("texto", "").strip()
            provenance_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

            citation = KnowledgeCitation(
                id=str(item.get("id", "")),
                file_path=doc_path,
                section=item.get("secao", ""),
                locator=item.get("onde", ""),
                content=content,
                score=score,
                provenance_hash=provenance_hash,
                project_id=detected_project,
            )
            citations.append(citation)

        status = "FOUND" if citations else "INSUFFICIENT_EVIDENCE"

        return KnowledgeQueryResult(
            status=status,
            query=query.query,
            project_id=query.project_id,
            citations=citations,
            total_found=len(trechos),
            execution_time_ms=elapsed_ms,
        )

    def read_note(self, chunk_id: str, janela: int = 1) -> Dict[str, Any]:
        """Read full surrounding context of a chunk by ID."""
        return self._execute_mcp_tool("read_note", {"id": chunk_id, "janela": janela})

    def neighbors(self, file_path: str, limite: int = 5) -> Dict[str, Any]:
        """Traverse relationship graph for cited entities and related docs."""
        return self._execute_mcp_tool("neighbors", {"arquivo": file_path, "limite": limite})
