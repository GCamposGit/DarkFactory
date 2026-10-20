"""Qualify a candidate model (USR-185 slice 6). Uses a recorded-answers file; no paid call is made by default.

Usage: python C:\\dev\\DarkFac\\scripts\\qualify_model.py <model_id> --recorded answers.json [--max-usd 0] [--max-tokens N]
``answers.json`` maps step id (contract, tools, reference:<task>) to {text, tool_calls, input_tokens, output_tokens, cost_usd}.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.models.qualification import (  # noqa: E402
    Budget,
    ExecResult,
    RecordedExecutor,
    persist_evidence,
    record_in_registry,
    run_battery,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_id")
    parser.add_argument("--recorded", required=True, type=Path)
    parser.add_argument("--max-usd", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--no-record", action="store_true", help="persist evidence only; do not touch the registry")
    args = parser.parse_args(argv)
    raw = json.loads(args.recorded.read_text(encoding="utf-8"))
    recorded = {
        k: ExecResult(
            text=v.get("text", ""),
            tool_calls=tuple(v.get("tool_calls", [])),
            input_tokens=v.get("input_tokens", 0),
            output_tokens=v.get("output_tokens", 0),
            cost_usd=v.get("cost_usd", 0.0),
        )
        for k, v in raw.items()
    }
    result = run_battery(
        args.model_id,
        RecordedExecutor(recorded),
        now=datetime.now(timezone.utc),
        budget=Budget(args.max_usd, args.max_tokens),
    )
    path = persist_evidence(result)
    if not args.no_record:
        record_in_registry(result, path)
    print(f"{args.model_id}: {result.status} (cost_usd={result.cost_usd}) evidence={path}")
    return 0 if result.approved else 1


if __name__ == "__main__":
    sys.exit(main())
