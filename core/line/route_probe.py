"""CLI probe for quota-parked production-line jobs (USR-346)."""

from __future__ import annotations

import argparse
from collections.abc import Iterable

from core.line import routing

ROUTE_PROBE_HOST_CAPS = (
    "harness:claude",
    "harness:codex",
    "harness:grok",
    "harness:antigravity",
)
ROUTE_PROBE_STAGES = frozenset({"development", "review", "planning", "grill"})


def route_available(stage: str, *, host_caps: Iterable[str] = ROUTE_PROBE_HOST_CAPS) -> bool:
    """Check whether normal production routing can currently elect a stage route."""
    if stage not in ROUTE_PROBE_STAGES:
        raise ValueError(f"Unsupported route-probe stage: {stage}")
    return routing.pick(stage, host_caps, mode=routing.stage_mode(stage)) is not None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check whether a production-line stage has a route")
    parser.add_argument("stage", choices=sorted(ROUTE_PROBE_STAGES))
    args = parser.parse_args(argv)
    return 0 if route_available(args.stage) else 1


if __name__ == "__main__":
    raise SystemExit(main())
