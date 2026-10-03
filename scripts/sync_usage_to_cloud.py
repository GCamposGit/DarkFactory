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
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

# Anchor sys.path to repository root
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.usage.monitor import AccountUsageMonitor
from core.usage.api_credits import ApiCreditsMonitor

logger = logging.getLogger("darkfac.sync_usage")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def _unquote_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", "'"):
        return value[1:-1].strip()
    return value


def load_telemetry_key(
    repo_root: Path,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """Read the telemetry key at runtime, preferring the process environment to local .env."""
    environment = os.environ if environ is None else environ
    from_environment = _unquote_env_value(environment.get("DARKFAC_TELEMETRY_KEY", ""))
    if from_environment:
        return from_environment

    env_file = Path(repo_root) / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            candidate = line.strip()
            if not candidate or candidate.startswith("#"):
                continue
            if candidate.lower().startswith("export "):
                candidate = candidate[7:].lstrip()
            name, separator, value = candidate.partition("=")
            if separator and name.strip() == "DARKFAC_TELEMETRY_KEY":
                return _unquote_env_value(value)
    except OSError:
        return ""
    return ""


def configure_sync_logging(repo_root: Path) -> None:
    """Persist task diagnostics locally because pythonw has no visible console."""
    if any(getattr(handler, "_darkfac_sync_file", False) for handler in logger.handlers):
        return
    try:
        log_dir = Path(repo_root) / ".factory" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "usage_sync.log",
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler._darkfac_sync_file = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)
    except OSError as exc:
        logger.warning("Could not open local sync log: %s", type(exc).__name__)


def _redact_secret(message: str, secret: str) -> str:
    return message.replace(secret, "[redacted]") if secret else message


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
        f"Sync failed with HTTP {response.status_code}: {response.text[:300]}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize local account quotas and credits to DarkHub cloud.")
    parser.add_argument(
        "--target-url",
        default=os.environ.get("DARKHUB_CLOUD_URL", "https://darkhub.ggcampos.com"),
        help="Target DarkHub base URL (default: https://darkhub.ggcampos.com or DARKHUB_CLOUD_URL)",
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
    configure_sync_logging(REPO_ROOT)
    logger.info("Starting DarkFac usage synchronization (Node: %s)...", args.node_id)

    missing_key_logged = False
    while True:
        iteration_started = time.monotonic()
        telemetry_key = load_telemetry_key(REPO_ROOT)
        if not args.dry_run and not telemetry_key:
            if not args.loop:
                logger.error(
                    "[SYNC_DEGRADED] DARKFAC_TELEMETRY_KEY ausente. Defina a variável de ambiente do usuário "
                    "ou adicione DARKFAC_TELEMETRY_KEY=<chave> em %s. Diagnóstico local: %s",
                    REPO_ROOT / ".env",
                    REPO_ROOT / ".factory" / "logs" / "usage_sync.log",
                )
                return 1
            if not missing_key_logged:
                logger.error(
                    "[SYNC_DEGRADED] DARKFAC_TELEMETRY_KEY ausente. O monitor continuará ativo e retomará "
                    "a sincronização automaticamente quando a variável do usuário ou a linha correspondente "
                    "em %s estiver disponível. Diagnóstico local: %s",
                    REPO_ROOT / ".env",
                    REPO_ROOT / ".factory" / "logs" / "usage_sync.log",
                )
                missing_key_logged = True
            delay = max(0.0, args.interval - (time.monotonic() - iteration_started))
            if delay:
                time.sleep(delay)
            continue

        if missing_key_logged:
            logger.info("[SYNC_RECOVERED] Telemetry key is available; resuming quota synchronization.")
            missing_key_logged = False

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

            result = send_sync_payload(args.target_url, payload, telemetry_key)
            logger.info(
                "[SYNC_SUCCESS] Synchronized %d accounts and %d credits to %s: %s",
                result.get("accounts_updated", 0),
                result.get("credits_updated", 0),
                args.target_url,
                _redact_secret(str(result.get("message", "")), telemetry_key),
            )
        except Exception as exc:
            logger.error("[SYNC_FAIL] Synchronization attempt failed: %s", _redact_secret(str(exc), telemetry_key))
            if not args.loop:
                return 1

        if not args.loop:
            break

        delay = max(0.0, args.interval - (time.monotonic() - iteration_started))
        logger.info("Waiting %.1f seconds before the next sync...", delay)
        if delay:
            time.sleep(delay)

    return 0


if __name__ == "__main__":
    sys.exit(main())
