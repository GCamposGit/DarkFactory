"""Sample public Hub health once per second and write observations as JSONL.

Example (PowerShell):
    python C:\\dev\\DarkFac\\scripts\\hub_downtime_probe.py --url https://darkhub.ggcampos.com/health --seconds 600 --out C:\\Users\\guigc\\hub_probe.jsonl --expect-switch
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.infra.downtime_probe import DEFAULT_URL, DEFAULT_TIMEOUT_SECONDS, ProbeResult, run_probe  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help="Public Hub /health URL")
    parser.add_argument("--seconds", type=int, default=600, help="Number of one-second samples")
    parser.add_argument("--out", type=Path, default=Path("hub_downtime.jsonl"), help="JSONL output path")
    parser.add_argument("--expect-switch", action="store_true", help="Require at least two distinct SHAs")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    prober: Callable[[str, float], ProbeResult] | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.seconds < 1:
        print("seconds must be at least 1")
        return 1
    options = {}
    if prober is not None:
        options["prober"] = prober
    if clock is not None:
        options["clock"] = clock
    if sleep is not None:
        options["sleep"] = sleep
    try:
        summary = run_probe(args.url, args.seconds, args.out, **options)
    except (OSError, ValueError) as exc:
        print(f"probe failed: {exc}")
        return 1
    shas = summary["distinct_shas"]
    print(
        "Hub downtime probe: "
        f"total={summary['total']} non200={summary['non200']} "
        f"max_failure_streak={summary['max_failure_streak']} distinct_shas={len(shas)} "
        f"shas={','.join(shas)}"
    )
    if summary["non200"] != 0 or (args.expect_switch and len(shas) < 2):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
