"""USR-42: shipped catalog evolutions reach a persisted (Docker volume) services.json once."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from hub.backend.service import HubService

REPO_DATA = Path(__file__).resolve().parents[1] / "hub" / "data"


def _service(data_dir: Path, tmp_path: Path) -> HubService:
    return HubService(
        data_dir=data_dir,
        usage_dir=tmp_path / "usage",
        roadmap_root=Path.cwd(),
        state_path=tmp_path / "state.json",
        orchestrator_path=tmp_path / "orchestrator.sqlite3",
        control_db_path=tmp_path / "control.db",
        control_database_url="",
    )


def _persisted_volume(tmp_path: Path) -> Path:
    """Simulate a volume created before this revision: old texts and a user-edited URL."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    shutil.copy(REPO_DATA / "default_services.json", data_dir / "default_services.json")
    shutil.copy(REPO_DATA / "default_prompts.json", data_dir / "default_prompts.json")
    old = [
        {
            "id": "claude-console",
            "name": "Claude Console & Chat",
            "url": "https://claude.ai/new",
            "category": "llm_chat",
            "description": "Claude 3.7 Sonnet com Extended Thinking.",
            "tags": ["anthropic"],
            "is_favorite": False,
            "pinned": False,
        },
        {"id": "my-tool", "name": "Minha ferramenta", "url": "https://example.com/", "category": "custom"},
    ]
    (data_dir / "services.json").write_text(json.dumps(old), encoding="utf-8")
    return data_dir


def test_revision_refreshes_texts_and_adds_harnesses_preserving_owner_fields(tmp_path: Path) -> None:
    data_dir = _persisted_volume(tmp_path)

    _service(data_dir, tmp_path)

    items = {item["id"]: item for item in json.loads((data_dir / "services.json").read_text(encoding="utf-8"))}
    assert {"claude-code", "openai-codex", "dokploy-console", "n8n-workflows"} <= set(items)
    claude = items["claude-console"]
    assert "3.7" not in claude["description"]
    assert claude["url"] == "https://claude.ai/new"
    assert claude["is_favorite"] is False and claude["pinned"] is False
    assert "my-tool" in items
    assert all("catalog_revision" not in item for item in items.values())
    marker = json.loads((data_dir / "catalog_meta.json").read_text(encoding="utf-8"))
    assert marker["applied_revision"] == "2026-09-23"


def test_revision_is_applied_once_so_deleted_services_stay_deleted(tmp_path: Path) -> None:
    data_dir = _persisted_volume(tmp_path)
    _service(data_dir, tmp_path)
    services_file = data_dir / "services.json"
    kept = [item for item in json.loads(services_file.read_text(encoding="utf-8")) if item["id"] != "claude-code"]
    services_file.write_text(json.dumps(kept), encoding="utf-8")

    _service(data_dir, tmp_path)

    ids = {item["id"] for item in json.loads(services_file.read_text(encoding="utf-8"))}
    assert "claude-code" not in ids


def test_shipped_catalog_has_no_pinned_model_versions() -> None:
    """Model versions age fast; they belong to the Benchmarks panel, not catalog copy."""
    items = json.loads((REPO_DATA / "default_services.json").read_text(encoding="utf-8"))
    stale_markers = ("3.7 Sonnet", "GPT-4o", "o3-mini", "Grok 4.6", "Gemini 3.8")
    for item in items:
        text = f"{item['name']} {item['description']}"
        assert not any(marker in text for marker in stale_markers), item["id"]
    assert (REPO_DATA / "services.json").read_text(encoding="utf-8") == (
        REPO_DATA / "default_services.json"
    ).read_text(encoding="utf-8")
