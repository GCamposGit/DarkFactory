#!/usr/bin/env python3
"""
DarkFac Usage & Credits Cloud Synchronization Client (USR-01 / INFRA-09).

Collects local AI account quota snapshots and credit balances from this machine
(including Codex local app-server, Antigravity IDE RPC, Grok CLI, Ollama local)
and synchronizes them to the 24/7 DarkHub cloud gateway via POST /api/usage/sync.
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Anchor sys.path to repository root
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.usage.monitor import AccountUsageMonitor
from core.usage.api_credits import ApiCreditsMonitor

logger = logging.getLogger("darkfac.sync_usage")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def collect_local_usage_payload(
    repo_root: Path,
    node_id: str = "workstation",
    force_probe: bool = True,
) -> Dict[str, Any]:
    """Collects local provider accounts and API credits into a unified sync payload."""
    factory_dir = repo_root / ".factory"
    providers_dir = factory_dir / "usage" / "providers"
    credits_dir = factory_dir / "usage" / "credits"

    # 1. Inspect local accounts
    account_monitor = AccountUsageMonitor(providers_dir)
    account_report = account_monitor.inspect(force=force_probe)
    accounts_list: List[Dict[str, Any]] = [
        acc.model_dump() if hasattr(acc, "model_dump") else acc
        for acc in account_report.accounts
    ]

    # 2. Inspect local credits
    credits_monitor = ApiCreditsMonitor(credits_dir)
    credits_report = credits_monitor.generate_report(force=force_probe)
    credits_list: List[Dict[str, Any]] = [
        c.model_dump() if hasattr(c, "model_dump") else c
        for c in credits_report.accounts
    ]

    return {
        "client_node_id": node_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "accounts": accounts_list,
        "credits": credits_list,
    }


def send_sync_payload(
    target_url: str,
    payload: Dict[str, Any],
    telemetry_key: str,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """Posts the usage payload to the target DarkHub cloud endpoint."""
    import httpx

    endpoint = f"{target_url.rstrip('/')}/api/usage/sync"
    headers = {
        "Content-Type": "application/json",
        "X-DarkFac-Telemetry-Key": telemetry_key,
        "User-Agent": f"DarkFac-Sync/{payload.get('client_node_id', 'workstation')}",
    }

    response = httpx.post(endpoint, json=payload, headers=headers, timeout=timeout)
    if response.status_code == 200:
        return response.json()
    raise RuntimeError(
        f"Sync failed with HTTP {response.status_code}: {response.text}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize local account quotas and credits to DarkHub cloud.")
    parser.add_argument(
        "--target-url",
        default=os.environ.get("DARKHUB_CLOUD_URL", "https://darkhub.ggcampos.com"),
        help="Target DarkHub base URL (default: https://darkhub.ggcampos.com or DARKHUB_CLOUD_URL)",
    )
    parser.add_argument(
        "--key",
        default=os.environ.get("DARKFAC_TELEMETRY_KEY", ""),
        help="Telemetry secret key (or DARKFAC_TELEMETRY_KEY env var)",
    )
    parser.add_argument(
        "--node-id",
        default=os.environ.get("DARKFAC_NODE_ID", "predator-neo-16"),
        help="Identifier of this client workstation (default: predator-neo-16)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect and print the payload without sending it over the network",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously in a loop at specified interval",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Interval in seconds when running in loop mode (default: 60)",
    )

    args = parser.parse_args()

    if not args.dry_run and not args.key:
        logger.error("[SYNC_ERROR] Telemetry key missing. Set DARKFAC_TELEMETRY_KEY or pass --key.")
        return 1

    logger.info("Starting DarkFac usage synchronization (Node: %s)...", args.node_id)

    while True:
        try:
            payload = collect_local_usage_payload(REPO_ROOT, node_id=args.node_id)
            accounts_count = len(payload.get("accounts", []))
            credits_count = len(payload.get("credits", []))

            if args.dry_run:
                logger.info(
                    "[DRY_RUN] Collected %d accounts and %d credits. Payload preview:\n%s",
                    accounts_count,
                    credits_count,
                    json.dumps(payload, indent=2)[:500] + "...",
                )
                return 0

            result = send_sync_payload(args.target_url, payload, args.key)
            logger.info(
                "[SYNC_SUCCESS] Synchronized %d accounts and %d credits to %s: %s",
                result.get("accounts_updated", 0),
                result.get("credits_updated", 0),
                args.target_url,
                result.get("message", ""),
            )
        except Exception as exc:
            logger.error("[SYNC_FAIL] Synchronization attempt failed: %s", exc)
            if not args.loop:
                return 1

        if not args.loop:
            break

        logger.info("Sleeping for %d seconds before next sync...", args.interval)
        time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    sys.exit(main())
