"""Durable, bounded worker-to-Hub synchronization of human requests (USR-194)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from core.line.human import HumanRequest
from core.owner_actions.models import redact
from core.owner_actions.store import _atomic_write_json, _file_lock
from core.paths import state_root

logger = logging.getLogger(__name__)
_transport_lock = threading.Lock()
RETRY_INTERVAL_SEC = 30.0
HTTP_TIMEOUT_SEC = 1.5


class SyncEvent(BaseModel):
    """Only human-request data crosses hosts; the Hub computes the action ID."""

    model_config = ConfigDict(extra="forbid")
    request: HumanRequest
    closed: bool = False


def outbox_path() -> Path:
    """Use a root artifact FILE, which workflow-directory retention never sweeps."""
    return state_root() / "artifacts" / "owner-action-outbox"


def _key(run_id: str, stage: str) -> str:
    return hashlib.sha256(json.dumps([run_id, stage]).encode("utf-8")).hexdigest()


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, dict):
        raise ValueError("invalid outbox")
    return rows


def publish_request(request: HumanRequest) -> None:
    """Persist before delivery; closed records remain terminal across restarts."""
    try:
        safe = request.model_copy(update={
            "guide_md": redact(request.guide_md),
            "probe_cmd": redact(request.probe_cmd) if request.probe_cmd else None,
        })
        event = SyncEvent(request=safe)
        path = outbox_path()
        with _file_lock(path):
            rows = _load(path)
            key = _key(request.run_id, request.blocking_stage)
            old = rows.get(key)
            if not old or not old["event"]["closed"]:
                data = event.model_dump(mode="json")
                if not old or old["event"] != data:
                    rows[key] = {"event": data, "sent": False}
                    _atomic_write_json(path, rows)
        flush_pending()
    except Exception as exc:
        logger.warning("owner action sync enqueue failed (%s)", type(exc).__name__)


def publish_closed(run_id: str, blocking_stage: str) -> None:
    """Queue terminal state using the durable request kept after its open ACK."""
    try:
        path = outbox_path()
        with _file_lock(path):
            rows = _load(path)
            key = _key(run_id, blocking_stage)
            old = rows.get(key)
            if old and not old["event"]["closed"]:
                old["event"]["closed"] = True
                old["sent"] = False
                old["next_attempt"] = 0
                _atomic_write_json(path, rows)
            elif old is None:
                request = HumanRequest(
                    kind="infra", run_id=run_id, blocking_stage=blocking_stage,
                    guide_md="Pedido encerrado pela linha de producao.",
                )
                rows[key] = {"event": SyncEvent(request=request, closed=True).model_dump(mode="json"), "sent": False}
                _atomic_write_json(path, rows)
        flush_pending()
    except Exception as exc:
        logger.warning("owner action close sync enqueue failed (%s)", type(exc).__name__)


def flush_pending(*, force: bool = False) -> int:
    """Retry at most every 30s in existing polls/heartbeats, without sleeping."""
    url = os.environ.get("DARKHUB_CLOUD_URL", "").strip().rstrip("/")
    key = os.environ.get("DARKFAC_TELEMETRY_KEY", "").strip()
    path = outbox_path()
    if not url or not key or not path.exists() or not _transport_lock.acquire(blocking=False):
        return 0
    sent = 0
    try:
        now = time.time()
        with _file_lock(path):
            pending = [
                (k, row["event"]) for k, row in _load(path).items()
                if not row["sent"] and (force or now >= row.get("next_attempt", 0))
            ][:2]
        for event_key, data in pending:
            event = SyncEvent.model_validate(data)
            with _file_lock(path):
                rows = _load(path)
                if rows.get(event_key, {}).get("event") != data:
                    continue
                rows[event_key]["next_attempt"] = now + RETRY_INTERVAL_SEC
                _atomic_write_json(path, rows)
            req = urllib.request.Request(
                url + "/api/owner-actions/sync", data=event.model_dump_json().encode("utf-8"),
                headers={"Content-Type": "application/json", "X-DarkFac-Telemetry-Key": key}, method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC):
                    pass
            except Exception as exc:
                logger.warning("owner action sync deferred (%s)", type(exc).__name__)
                continue
            with _file_lock(path):
                rows = _load(path)
                # Do not ACK a newer close queued while the open POST was in flight.
                if event_key in rows and rows[event_key]["event"] == data:
                    rows[event_key]["sent"] = True
                    _atomic_write_json(path, rows)
                    sent += 1
        return sent
    except Exception as exc:
        logger.warning("owner action sync deferred (%s)", type(exc).__name__)
        return sent
    finally:
        _transport_lock.release()
