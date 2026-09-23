"""USR-44: shipped catalog/prompt seeds reach the cloud Hub despite a stale persisted volume.

In Dokploy, ``darkhub-hub-data`` mounts ``/app/hub/data`` as a named volume. A redeploy
that ships an updated ``default_services.json`` / ``default_prompts.json`` never reaches
the running container because the volume's old copies keep shadowing the image's new
ones. ``DARKHUB_SEED_DIR`` (baked in by ``Dockerfile.hub`` as ``COPY hub/data
/app/hub_seed``) points at an image-only copy of these seed files, outside the volume,
so ``HubService`` can overlay them onto ``data_dir`` before storage is initialized.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hub.backend.service import HubService

REPO_DATA = Path(__file__).resolve().parents[1] / "hub" / "data"


def _make_seed_dir(tmp_path: Path, name: str = "hub_seed") -> Path:
    """Simulate the image-level seed dir Dockerfile.hub bakes in at /app/hub_seed."""
    seed_dir = tmp_path / name
    seed_dir.mkdir()
    shutil.copy(REPO_DATA / "default_services.json", seed_dir / "default_services.json")
    shutil.copy(REPO_DATA / "default_prompts.json", seed_dir / "default_prompts.json")
    return seed_dir


def _stale_volume(tmp_path: Path) -> Path:
    """Simulate a Docker volume created long before this revision shipped: an old
    default_services.json without catalog_revision, and an old services.json with
    ~15 items including a claude-console entry carrying a stale description, plus
    one owner-added custom service.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    old_default_services = [
        {
            "id": "claude-console",
            "name": "Claude Console & Chat",
            "url": "https://claude.ai/new",
            "category": "llm_chat",
            "description": "Claude 3.7 Sonnet com Extended Thinking.",
            "tags": ["anthropic"],
        },
    ]
    (data_dir / "default_services.json").write_text(json.dumps(old_default_services), encoding="utf-8")

    old_default_prompts = [
        {
            "id": "prompt-old",
            "title": "Old prompt",
            "description": "stale seed",
            "tags": [],
            "content": "stale content",
        }
    ]
    (data_dir / "default_prompts.json").write_text(json.dumps(old_default_prompts), encoding="utf-8")

    old_services = [
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
    ] + [
        {
            "id": f"legacy-{i}",
            "name": f"Legacy {i}",
            "url": f"https://legacy-{i}.example.com/",
            "category": "custom",
        }
        for i in range(13)
    ]
    (data_dir / "services.json").write_text(json.dumps(old_services), encoding="utf-8")
    return data_dir


def _service(data_dir: Path, tmp_path: Path, **kwargs) -> HubService:
    return HubService(
        data_dir=data_dir,
        usage_dir=tmp_path / "usage",
        roadmap_root=Path.cwd(),
        state_path=tmp_path / "state.json",
        orchestrator_path=tmp_path / "orchestrator.sqlite3",
        control_db_path=tmp_path / "control.db",
        control_database_url="",
        **kwargs,
    )


def test_cloud_seed_env_refreshes_catalog_and_prompts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = _stale_volume(tmp_path)
    seed_dir = _make_seed_dir(tmp_path)
    monkeypatch.setenv("DARKHUB_SEED_DIR", str(seed_dir))

    service = _service(data_dir, tmp_path)

    # default_services.json itself was overlaid with the shipped seed content.
    assert (data_dir / "default_services.json").read_text(encoding="utf-8") == (
        seed_dir / "default_services.json"
    ).read_text(encoding="utf-8")

    items = {item["id"]: item for item in json.loads((data_dir / "services.json").read_text(encoding="utf-8"))}
    assert {"claude-code", "openai-codex", "dokploy-console", "n8n-workflows"} <= set(items)
    assert "3.7" not in items["claude-console"]["description"]
    assert "my-tool" in items, "owner-added custom service must be preserved"

    marker = json.loads((data_dir / "catalog_meta.json").read_text(encoding="utf-8"))
    assert marker["applied_revision"] == "2026-09-23"

    prompts = service.list_prompts()
    repo_prompts = json.loads((REPO_DATA / "default_prompts.json").read_text(encoding="utf-8"))
    assert {p.id for p in prompts} == {p["id"] for p in repo_prompts}
    assert "prompt-old" not in {p.id for p in prompts}


def test_cloud_seed_constructor_arg_wins_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = _stale_volume(tmp_path)
    seed_dir = _make_seed_dir(tmp_path)
    wrong_seed_dir = tmp_path / "wrong_seed"
    wrong_seed_dir.mkdir()
    (wrong_seed_dir / "default_services.json").write_text("[]", encoding="utf-8")
    (wrong_seed_dir / "default_prompts.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("DARKHUB_SEED_DIR", str(wrong_seed_dir))

    _service(data_dir, tmp_path, seed_dir=seed_dir)

    items = {item["id"] for item in json.loads((data_dir / "services.json").read_text(encoding="utf-8"))}
    assert "claude-code" in items, "the explicit seed_dir argument must win over DARKHUB_SEED_DIR"


def test_without_seed_dir_or_env_nothing_is_copied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DARKHUB_SEED_DIR", raising=False)
    data_dir = _stale_volume(tmp_path)
    original_default_services = (data_dir / "default_services.json").read_text(encoding="utf-8")
    original_default_prompts = (data_dir / "default_prompts.json").read_text(encoding="utf-8")

    _service(data_dir, tmp_path)

    assert (data_dir / "default_services.json").read_text(encoding="utf-8") == original_default_services
    assert (data_dir / "default_prompts.json").read_text(encoding="utf-8") == original_default_prompts
    items = {item["id"] for item in json.loads((data_dir / "services.json").read_text(encoding="utf-8"))}
    assert "claude-code" not in items


def test_cloud_seed_is_idempotent_across_constructions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = _stale_volume(tmp_path)
    seed_dir = _make_seed_dir(tmp_path)
    monkeypatch.setenv("DARKHUB_SEED_DIR", str(seed_dir))

    _service(data_dir, tmp_path)
    services_file = data_dir / "services.json"
    kept = [item for item in json.loads(services_file.read_text(encoding="utf-8")) if item["id"] != "claude-code"]
    services_file.write_text(json.dumps(kept), encoding="utf-8")

    _service(data_dir, tmp_path)

    ids = [item["id"] for item in json.loads(services_file.read_text(encoding="utf-8"))]
    assert "claude-code" not in ids, "a service the owner deleted must not be resurrected"
    assert len(ids) == len(set(ids)), "no duplicate services after a second construction"


def test_dockerfile_copies_seed_dir_outside_the_volume_and_sets_env() -> None:
    dockerfile = Path(__file__).resolve().parents[1] / "deploy" / "dokploy" / "Dockerfile.hub"
    content = dockerfile.read_text(encoding="utf-8")
    assert "COPY hub/data /app/hub_seed" in content
    assert "DARKHUB_SEED_DIR=/app/hub_seed" in content
