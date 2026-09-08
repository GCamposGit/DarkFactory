"""Headless CLI for account quotas and external harness model events."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from core.usage.ledger import ModelUsageLedger
from core.usage.models import ModelCallEvent, ModelModality, ModelTier
from core.usage.monitor import AccountUsageMonitor
from core.paths import project_root


PROJECT_ROOT = project_root()
USAGE_DIR = PROJECT_ROOT / ".factory" / "usage"


def _configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DarkFac AI quota and model usage telemetry")
    subparsers = parser.add_subparsers(dest="command", required=True)

    accounts = subparsers.add_parser("accounts", help="Inspect all configured AI accounts")
    accounts.add_argument("--refresh", action="store_true")

    models = subparsers.add_parser("models", help="Show project model-call aggregates")
    models.add_argument("--recent", type=int, default=25)

    record = subparsers.add_parser("record", help="Record one call made by an external harness")
    record.add_argument("--invocation-id", help="Stable retry/idempotency key")
    record.add_argument("--provider", required=True)
    record.add_argument("--model", required=True)
    record.add_argument("--tier", choices=[item.value for item in ModelTier], default=ModelTier.UNKNOWN.value)
    record.add_argument("--harness", required=True)
    record.add_argument("--modality", choices=[item.value for item in ModelModality], default=ModelModality.TEXT.value)
    record.add_argument("--failed", action="store_true")
    record.add_argument("--input-tokens", type=int)
    record.add_argument("--output-tokens", type=int)
    record.add_argument("--cost-usd", type=float)
    record.add_argument("--latency-ms", type=float)
    record.add_argument("--source", default="external_harness")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    _configure_stdout()
    args = _parser().parse_args(argv)
    if args.command == "accounts":
        report = AccountUsageMonitor(USAGE_DIR / "providers").inspect(force=args.refresh)
        print(report.model_dump_json(indent=2))
        return 0
    ledger = ModelUsageLedger(USAGE_DIR)
    if args.command == "models":
        print(ledger.report(recent_limit=max(0, args.recent)).model_dump_json(indent=2))
        return 0
    event = ModelCallEvent(
        **({"invocation_id": args.invocation_id} if args.invocation_id else {}),
        provider=args.provider,
        model=args.model,
        tier=ModelTier(args.tier),
        harness=args.harness,
        modality=ModelModality(args.modality),
        success=not args.failed,
        input_tokens=args.input_tokens,
        output_tokens=args.output_tokens,
        cost_usd=args.cost_usd,
        latency_ms=args.latency_ms,
        source=args.source,
    )
    ledger.record(event)
    print(json.dumps({"recorded": True, "event": event.model_dump(mode="json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
