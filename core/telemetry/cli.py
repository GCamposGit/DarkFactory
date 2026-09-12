"""
Headless CLI for querying, summarizing, and recording AI model execution telemetry.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.telemetry.models import (
    ExecutionMode,
    TelemetryFilters,
    TelemetryRecordCreate,
)
from core.telemetry.store import TelemetryStore


def _configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DarkFac AI Model Telemetry CLI - Real-time observation of AI invocations"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Query
    query = subparsers.add_parser("query", help="Query paginated model runs")
    query.add_argument("--ticket", help="Filter by ticket ID (e.g. HF-15)")
    query.add_argument("--model", help="Filter by model name")
    query.add_argument("--provider", help="Filter by provider")
    query.add_argument("--mode", choices=["ui", "headless"], help="Filter by execution mode")
    query.add_argument("--limit", type=int, default=25, help="Number of records to return")
    query.add_argument("--offset", type=int, default=0, help="Offset for pagination")

    # Stats
    stats = subparsers.add_parser("stats", help="Get aggregated telemetry statistics")
    stats.add_argument("--ticket", help="Filter statistics by ticket ID")
    stats.add_argument("--model", help="Filter statistics by model name")
    stats.add_argument("--provider", help="Filter statistics by provider")
    stats.add_argument("--mode", choices=["ui", "headless"], help="Filter by execution mode")

    # Tickets
    subparsers.add_parser("tickets", help="List all distinct tickets with recorded runs")

    # Record
    record = subparsers.add_parser("record", help="Record an AI model run")
    record.add_argument("--id", help="Optional run ID (UUID generated if omitted)")
    record.add_argument("--ticket", help="Ticket ID being worked on")
    record.add_argument("--provider", required=True, help="Provider name (e.g. ollama, openrouter)")
    record.add_argument("--model", required=True, help="Model name")
    record.add_argument("--tier", default="local", help="Model tier")
    record.add_argument("--harness", default="cli", help="Harness name")
    record.add_argument("--mode", choices=["ui", "headless"], default="headless", help="Execution mode")
    record.add_argument("--input-tokens", type=int, default=0, help="Input/prompt tokens")
    record.add_argument("--proc-tokens", type=int, default=0, help="Processing/reasoning tokens")
    record.add_argument("--output-tokens", type=int, default=0, help="Output/completion tokens")
    record.add_argument("--latency-ms", type=float, default=0.0, help="Latency in milliseconds")
    record.add_argument("--cost-usd", type=float, default=0.0, help="Cost in USD")
    record.add_argument("--failed", action="store_true", help="Flag if the execution failed")
    record.add_argument("--error", help="Optional error message")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    _configure_stdout()
    args = _parser().parse_args(argv)
    store = TelemetryStore()

    if args.command == "query":
        filters = TelemetryFilters(
            ticket_id=args.ticket,
            model=args.model,
            provider=args.provider,
            execution_mode=ExecutionMode(args.mode) if args.mode else None,
            limit=args.limit,
            offset=args.offset,
        )
        result = store.query_runs(filters)
        print(result.model_dump_json(indent=2))
        return 0

    if args.command == "stats":
        filters = TelemetryFilters(
            ticket_id=args.ticket,
            model=args.model,
            provider=args.provider,
            execution_mode=ExecutionMode(args.mode) if args.mode else None,
        )
        stats = store.get_stats(filters)
        print(stats.model_dump_json(indent=2))
        return 0

    if args.command == "tickets":
        tickets = store.list_tickets()
        print(json.dumps({"tickets": tickets}, indent=2, ensure_ascii=False))
        return 0

    if args.command == "record":
        record = TelemetryRecordCreate(
            id=args.id,
            ticket_id=args.ticket,
            provider=args.provider,
            model=args.model,
            tier=args.tier,
            harness=args.harness,
            execution_mode=ExecutionMode(args.mode),
            input_tokens=args.input_tokens,
            processing_tokens=args.proc_tokens,
            output_tokens=args.output_tokens,
            latency_ms=args.latency_ms,
            cost_usd=args.cost_usd,
            success=not args.failed,
            error_message=args.error,
        )
        saved = store.record(record)
        print(json.dumps({"recorded": True, "run": saved.model_dump(mode="json")}, indent=2, ensure_ascii=False))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
