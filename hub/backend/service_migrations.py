"""One-shot migrations of the persisted ``services.json`` (USR-213).

``hub/data`` lives in the ``darkhub-hub-data`` volume in production, so editing the
shipped ``default_services.json`` never reaches an existing ``services.json``. Each
migration here runs at most once (tracked in ``service_migrations.json``), is
idempotent, and never touches services the user added.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MIGRATIONS_MARKER = "service_migrations.json"
DOCK_GROK_MIGRATION_ID = "usr-213-dock-grok-no-canaletto"
REMOVED_SERVICE_IDS: tuple[str, ...] = ("canaletto-gallery",)

GROK_SERVICE: dict[str, Any] = {
    "id": "xai-grok",
    "name": "xAI Grok",
    "url": "https://grok.com/",
    "category": "llm_chat",
    "description": (
        "Chat e harness Grok para busca em tempo real e raciocínio rápido. "
        "Opera a fábrica lendo AGENTS.md e FACTORY_RULES.md."
    ),
    "tags": ["xai", "grok", "harness", "realtime-search"],
    "icon": "zap",
    "color": "#ef4444",
    "is_favorite": True,
    "pinned": True,
    "is_local": False,
    "catalog_revision": "2026-09-23",
}


def _is_grok(item: dict[str, Any]) -> bool:
    host = (urlparse(str(item.get("url") or "")).hostname or "").lower()
    return host == "grok.com" or host.endswith(".grok.com")


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def apply_service_migrations(data_dir: Path) -> bool:
    """Apply pending migrations to ``data_dir/services.json``; return True if it changed."""
    services_file = data_dir / "services.json"
    marker_file = data_dir / MIGRATIONS_MARKER
    if not services_file.exists():
        return False
    marker = _read_json(marker_file, {})
    applied = marker.get("applied") if isinstance(marker, dict) else None
    applied = list(applied) if isinstance(applied, list) else []
    if DOCK_GROK_MIGRATION_ID in applied:
        return False
    items = _read_json(services_file, None)
    if not isinstance(items, list):
        logger.warning("Service migration skipped: %s is not a JSON list", services_file)
        return False

    kept = [i for i in items if not (isinstance(i, dict) and i.get("id") in REMOVED_SERVICE_IDS)]
    changed = len(kept) != len(items)
    grok = next((i for i in kept if isinstance(i, dict) and (i.get("id") == "xai-grok" or _is_grok(i))), None)
    if grok is None:
        kept.append(dict(GROK_SERVICE))
        changed = True
    elif not grok.get("pinned"):
        grok["pinned"] = True
        changed = True

    try:
        if changed:
            tmp = services_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(kept, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(services_file)
        marker_file.write_text(
            json.dumps({"applied": applied + [DOCK_GROK_MIGRATION_ID]}, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("Service migration not persisted: %s", exc)
        return False
    logger.info("Applied service migration %s (changed=%s)", DOCK_GROK_MIGRATION_ID, changed)
    return changed
