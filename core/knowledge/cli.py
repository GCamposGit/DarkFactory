"""Command-line interface for Dark Factory Knowledge Subsystem (HF-22)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from .ingestor import KnowledgeIngestor
    from .models import IngestionRequest, KnowledgeQuery
    from .segundo_cerebro_client import SegundoCerebroClient
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from core.knowledge.ingestor import KnowledgeIngestor
    from core.knowledge.models import IngestionRequest, KnowledgeQuery
    from core.knowledge.segundo_cerebro_client import SegundoCerebroClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.knowledge.cli",
        description="Dark Factory Segundo Cérebro MCP CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    parser_status = subparsers.add_parser("status", help="Check Segundo Cérebro MCP server health")
    parser_status.add_argument("--json", action="store_true", help="Output JSON")

    # search
    parser_search = subparsers.add_parser("search", help="Execute semantic/lexical knowledge search")
    parser_search.add_argument("query", help="Query string")
    parser_search.add_argument("--project", default="darkfac", help="Project namespace (darkfac, atrium, jarvis, shared)")
    parser_search.add_argument("--min-score", type=float, default=0.55, help="Fail-closed minimum relevance score")
    parser_search.add_argument("-k", type=int, default=5, help="Max citations to return")
    parser_search.add_argument("--json", action="store_true", help="Output JSON")

    # read
    parser_read = subparsers.add_parser("read", help="Read chunk details by ID")
    parser_read.add_argument("chunk_id", help="Chunk identifier")
    parser_read.add_argument("--window", type=int, default=1, help="Context window")
    parser_read.add_argument("--json", action="store_true", help="Output JSON")

    # ingest
    parser_ingest = subparsers.add_parser("ingest", help="Ingest an Office, PDF, or Markdown document")
    parser_ingest.add_argument("file_path", help="Path to file")
    parser_ingest.add_argument("--project", default="darkfac", help="Target project namespace")
    parser_ingest.add_argument("--format", choices=["docx", "xlsx", "pptx", "pdf", "md", "txt", "audio"], default=None)
    parser_ingest.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args(argv)
    client = SegundoCerebroClient()
    ingestor = KnowledgeIngestor()

    if args.command == "status":
        status = client.check_health()
        if getattr(args, "json", False):
            print(status.model_dump_json(indent=2))
        else:
            print(f"Segundo Cérebro MCP Status:")
            print(f"  Available: {status.available}")
            print(f"  Endpoint: {status.mcp_endpoint}")
            print(f"  Tools: {', '.join(status.registered_tools) if status.registered_tools else 'None'}")
            if status.error_message:
                print(f"  Diagnosis: {status.error_message}")
        return 0 if status.available else 1

    elif args.command == "search":
        q = KnowledgeQuery(
            query=args.query,
            project_id=args.project,
            min_score=args.min_score,
            k=args.k,
        )
        res = client.search(q)
        if getattr(args, "json", False):
            print(res.model_dump_json(indent=2))
        else:
            print(f"Query Result [{res.status}] for '{res.query}' in project '{res.project_id}' ({res.execution_time_ms} ms):")
            if not res.citations:
                print("  No citations satisfied the fail-closed threshold. Zero hallucinations generated.")
            for i, c in enumerate(res.citations, 1):
                print(f"\n--- [{i}] Score: {c.score:.3f} | {c.file_path} ({c.locator}) ---")
                print(f"Section: {c.section}")
                print(f"Hash: {c.provenance_hash[:16]}...")
                print(f"Content: {c.content[:200]}...")
        return 0

    elif args.command == "read":
        note = client.read_note(args.chunk_id, janela=args.window)
        if getattr(args, "json", False):
            print(json.dumps(note, indent=2))
        else:
            print(json.dumps(note, indent=2))
        return 0

    elif args.command == "ingest":
        p = Path(args.file_path)
        fmt = args.format
        if not fmt:
            ext = p.suffix.lower().lstrip(".")
            if ext in ["docx", "xlsx", "pptx", "pdf", "md", "txt"]:
                fmt = ext
            elif ext in ["wav", "mp3", "m4a"]:
                fmt = "audio"
            else:
                fmt = "txt"

        req = IngestionRequest(
            file_path=p,
            project_id=args.project,
            format=fmt,
        )
        result = ingestor.ingest(req)
        if getattr(args, "json", False):
            print(result.model_dump_json(indent=2))
        else:
            if result.success:
                print(f"Successfully ingested {result.file_path} into '{result.project_id}'.")
                print(f"  Staged: {result.staged_path}")
                print(f"  SHA-256: {result.file_hash}")
            else:
                print(f"Failed to ingest {result.file_path}: {result.error_message}")
        return 0 if result.success else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
