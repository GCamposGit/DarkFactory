"""Single source of truth for infrastructure node liveness (USR-217).

Every Hub surface that reports whether a node is reachable (``/api/harness/workers``,
``/api/infra/metrics``, the workers counter) must call :func:`get_node_liveness` so the
same ``/health`` payload always yields the same status.

Status vocabulary:
    ``online``       /health answered and no harness job is running.
    ``online_busy``  /health answered and the node is running a job ("online - ocupado").
    ``offline``      /health did not answer within the (short) timeout.

Node ids, IPs and ports come from the infrastructure inventory
(``core.infra.inventory.build_default_inventory``); nothing is hardcoded here.
Hardware telemetry sent by the node itself (heartbeat) is out of scope.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, ConfigDict

from core.infra.inventory import build_default_inventory

logger = logging.getLogger(__name__)

STATUS_ONLINE = "online"
STATUS_ONLINE_BUSY = "online_busy"
STATUS_OFFLINE = "offline"

DEFAULT_WORKER_PORT = 8080
DEFAULT_TIMEOUT_SECONDS = 0.8
RELAY_TIMEOUT_SECONDS = 3.0
CACHE_TTL_SECONDS = 2.0

#: Inventory node ids that run a ``/health`` worker daemon.
WORKER_NODE_IDS = ("grokbox-test-worker", "onprem-z97-server", "predator-neo-16")

#: ``(url, timeout) -> /health payload or None``.
Prober = Callable[[str, float], Optional[Dict[str, Any]]]


class NodeLiveness(BaseModel):
    model_config = ConfigDict(extra="ignore")

    node_id: str
    url: str
    status: str
    last_contact_at: Optional[datetime] = None
    latency_ms: Optional[float] = None
    details: Optional[Dict[str, Any]] = None

    @property
    def is_live(self) -> bool:
        return self.status != STATUS_OFFLINE

    @property
    def busy(self) -> bool:
        return self.status == STATUS_ONLINE_BUSY


_lock = threading.Lock()
_cache: Dict[str, tuple[float, NodeLiveness]] = {}
_last_contact: Dict[str, datetime] = {}


def clear_probe_cache() -> None:
    """Drop only the short probe cache, keeping last-contact memory."""
    with _lock:
        _cache.clear()


def reset_cache() -> None:
    """Drop cached results and last-contact memory (tests)."""
    with _lock:
        _cache.clear()
        _last_contact.clear()


def classify_health(payload: Optional[Dict[str, Any]]) -> str:
    """Map a ``/health`` payload (or ``None`` when unreachable) to a liveness status."""
    if not isinstance(payload, dict):
        return STATUS_OFFLINE
    try:
        active_runs = int(payload.get("active_runs") or 0)
    except (TypeError, ValueError):
        active_runs = 0
    if payload.get("busy") is True or payload.get("status") == "busy" or active_runs > 0:
        return STATUS_ONLINE_BUSY
    return STATUS_ONLINE


def worker_nodes() -> Dict[str, Dict[str, Any]]:
    """Return ``{node_id: {url, ip, port, timeout}}`` derived from the inventory."""
    result: Dict[str, Dict[str, Any]] = {}
    for node in build_default_inventory().nodes:
        if node.id not in WORKER_NODE_IDS or not node.network or not node.network.tailscale_ip:
            continue
        ip = node.network.tailscale_ip
        notes = node.network.notes or ""
        result[node.id] = {
            "url": f"http://{ip}:{DEFAULT_WORKER_PORT}",
            "ip": ip,
            "port": DEFAULT_WORKER_PORT,
            "timeout": RELAY_TIMEOUT_SECONDS if "DERP relay" in notes else DEFAULT_TIMEOUT_SECONDS,
        }
    return result


def _default_prober(url: str, timeout: float) -> Optional[Dict[str, Any]]:
    from core.harness.test_subagent import TestSubagentEngine

    return TestSubagentEngine().probe_remote_worker(url, timeout=timeout)


def get_node_liveness(
    node_id: str,
    *,
    url: Optional[str] = None,
    timeout: Optional[float] = None,
    prober: Optional[Prober] = None,
    cache_ttl: float = CACHE_TTL_SECONDS,
) -> NodeLiveness:
    """Probe ``/health`` of ``node_id`` (short timeout, short cache) and classify it."""
    spec = worker_nodes().get(node_id, {})
    target = (url or spec.get("url") or "").rstrip("/")
    bounded = timeout if timeout is not None else spec.get("timeout", DEFAULT_TIMEOUT_SECONDS)
    use_cache = prober is None and cache_ttl > 0
    now_mono = time.monotonic()
    if use_cache:
        with _lock:
            hit = _cache.get(node_id)
        if hit and hit[1].url == target and now_mono - hit[0] < cache_ttl:
            return hit[1]

    payload: Optional[Dict[str, Any]] = None
    latency_ms: Optional[float] = None
    if target:
        start = time.perf_counter()
        try:
            payload = (prober or _default_prober)(target, bounded)
        except Exception as exc:  # a probe must never break the report
            logger.debug("Liveness probe failed for %s: %s", node_id, exc)
            payload = None
        latency_ms = round((time.perf_counter() - start) * 1000, 1)

    status = classify_health(payload)
    with _lock:
        if status != STATUS_OFFLINE:
            _last_contact[node_id] = datetime.now(timezone.utc)
        last_contact = _last_contact.get(node_id)
    result = NodeLiveness(
        node_id=node_id,
        url=target,
        status=status,
        last_contact_at=last_contact,
        latency_ms=latency_ms if status != STATUS_OFFLINE else None,
        details=payload if status != STATUS_OFFLINE else None,
    )
    if use_cache:
        with _lock:
            _cache[node_id] = (now_mono, result)
    return result


def list_worker_liveness(prober: Optional[Prober] = None) -> List[NodeLiveness]:
    """Liveness of every inventory worker node, in :data:`WORKER_NODE_IDS` order."""
    nodes = worker_nodes()
    return [get_node_liveness(nid, prober=prober) for nid in WORKER_NODE_IDS if nid in nodes]


def count_online(items: List[NodeLiveness]) -> int:
    """Single definition of the "workers online" counter (busy counts as online)."""
    return sum(1 for item in items if item.is_live)
