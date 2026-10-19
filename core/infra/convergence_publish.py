"""Publish the node-sync convergence snapshot to the DarkHub in the cloud (USR-204).

``dokploy_redeploy.py`` runs on the Notebook and writes ``node_sync_divergence.json`` /
``node_sync_pending.json`` into the local state dir. The Telegram ``/status`` runs on the VPS, which never
sees those files. Same channel as ``scripts/sync_usage_to_cloud.py``: an HTTPS POST to the Hub authenticated
by ``X-DarkFac-Telemetry-Key``. The Hub writes the received snapshot into *its* state dir, using the same
file names, so ``telegram_status.read_convergence_snapshot`` keeps being a cheap local read.

Everything here is best effort: a publish failure never affects the deploy, and the key is never logged.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("darkfac.infra.convergence_publish")

DIVERGENCE_FILE = "node_sync_divergence.json"
PENDING_FILE = "node_sync_pending.json"
ENDPOINT_PATH = "/api/node-sync/convergence"
DEFAULT_TARGET_URL = "https://darkhub.ggcampos.com"
DEFAULT_TIMEOUT = 5.0

Poster = Callable[[str, dict[str, Any], dict[str, str], float], int]


def collect_convergence_payload(state_dir: Path, node_id: str = "workstation") -> dict[str, Any] | None:
    """Read the local snapshot files. Returns None when no divergence snapshot exists."""
    try:
        raw = json.loads((Path(state_dir) / DIVERGENCE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    nodes = {str(k): int(v or 0) for k, v in raw.items()}
    pending: dict[str, Any] | None = None
    try:
        loaded = json.loads((Path(state_dir) / PENDING_FILE).read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            pending = loaded
    except (OSError, ValueError):
        pending = None
    return {"client_node_id": node_id, "nodes": nodes, "pending": pending}


def _urllib_post(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> int:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https Hub URL
        return int(response.status)


def publish_convergence_snapshot(
    state_dir: Path,
    *,
    telemetry_key: str,
    target_url: str | None = None,
    node_id: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    post: Poster | None = None,
) -> bool:
    """POST the snapshot to the Hub. Returns True on HTTP 200; never raises."""
    if not telemetry_key:
        logger.info("convergence publish skipped: no telemetry key")
        return False
    try:
        payload = collect_convergence_payload(
            state_dir, node_id or os.environ.get("DARKFAC_NODE_ID", "predator-neo-16")
        )
        if payload is None:
            return False
        base = (target_url or os.environ.get("DARKHUB_CLOUD_URL") or DEFAULT_TARGET_URL).rstrip("/")
        headers = {
            "Content-Type": "application/json",
            "X-DarkFac-Telemetry-Key": telemetry_key,
            "User-Agent": f"DarkFac-Sync/{payload['client_node_id']}",
        }
        status = (post or _urllib_post)(base + ENDPOINT_PATH, payload, headers, timeout)
        return status == 200
    except Exception as exc:  # best effort: the deploy result must not depend on this
        logger.warning("convergence publish failed: %s", type(exc).__name__)
        return False


def ingest_convergence_snapshot(
    state_dir: Path, nodes: dict[str, Any], pending: dict[str, Any] | None
) -> int:
    """Hub side: persist a received snapshot under the same file names the Notebook uses."""
    target = Path(state_dir)
    target.mkdir(parents=True, exist_ok=True)
    clean = {str(k)[:40]: int(v or 0) for k, v in list(nodes.items())[:16]}
    (target / DIVERGENCE_FILE).write_text(json.dumps(clean, indent=2), encoding="utf-8")
    pending_path = target / PENDING_FILE
    if pending:
        pending_path.write_text(json.dumps(pending), encoding="utf-8")
    else:
        pending_path.unlink(missing_ok=True)
    return len(clean)
