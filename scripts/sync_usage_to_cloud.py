"""CLI for the snapshot-only usage publisher; scheduling is independent of OA exports."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.usage.sync_task import (
    collect_local_usage_payload, configure_sync_logging, load_telemetry_key,
    publish_once,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-url")
    parser.add_argument("--node-id")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    configure_sync_logging(REPO_ROOT)
    while True:
        if args.dry_run:
            print(json.dumps(collect_local_usage_payload(REPO_ROOT, args.node_id or "workstation", force_probe=False)))
            return 0
        result = publish_once(REPO_ROOT, target_url=args.target_url, node_id=args.node_id)
        logging.getLogger("darkfac.sync_usage").info("usage_publish %s", result.model_dump_json())
        if not args.loop:
            return 0 if result.ok else 1
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
