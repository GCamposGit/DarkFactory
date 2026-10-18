"""USR-213: dock migration (remove Canaletto, pin Grok) on the persisted catalog."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hub.backend import api
from hub.backend.service_migrations import GROK_SERVICE, apply_service_migrations

CANALETTO = {"id": "canaletto-gallery", "name": "Canaletto", "url": "http://127.0.0.1:8899", "pinned": True}
CHATGPT = {"id": "chatgpt-platform", "name": "ChatGPT", "url": "https://chatgpt.com/", "pinned": True}
CUSTOM = {"id": "my-tool", "name": "Mine", "url": "https://example.com/", "pinned": False}


def _write(tmp_path: Path, items: list[dict[str, Any]]) -> Path:
    path = tmp_path / "services.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    return path


def _read(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_removes_canaletto_adds_pinned_grok_keeps_user_services(tmp_path: Path) -> None:
    path = _write(tmp_path, [CHATGPT, CANALETTO, CUSTOM])
    assert apply_service_migrations(tmp_path) is True
    items = _read(path)
    ids = [i["id"] for i in items]
    assert ids == ["chatgpt-platform", "my-tool", "xai-grok"]
    grok = items[-1]
    assert grok["pinned"] is True and grok["url"] == "https://grok.com/"
    assert grok["category"] == CHATGPT.get("category", "llm_chat") == "llm_chat"
    assert {k for k in GROK_SERVICE} >= {"id", "name", "url", "category", "icon", "color", "pinned"}


def test_second_run_is_noop_and_does_not_recreate_canaletto(tmp_path: Path) -> None:
    path = _write(tmp_path, [CANALETTO, CUSTOM])
    apply_service_migrations(tmp_path)
    first = path.read_text(encoding="utf-8")
    items = _read(path) + [dict(CANALETTO)]
    path.write_text(json.dumps(items), encoding="utf-8")
    assert apply_service_migrations(tmp_path) is False
    assert [i["id"] for i in _read(path)].count("xai-grok") == 1
    assert any(i["id"] == "canaletto-gallery" for i in _read(path))  # user re-added; left alone
    assert first != ""


def test_existing_grok_url_is_not_duplicated(tmp_path: Path) -> None:
    mine = {"id": "my-grok", "name": "Grok", "url": "https://grok.com/chat", "pinned": False}
    path = _write(tmp_path, [mine, CANALETTO])
    apply_service_migrations(tmp_path)
    items = _read(path)
    assert [i["id"] for i in items] == ["my-grok"]
    assert items[0]["pinned"] is True


def test_existing_unpinned_seed_grok_gets_pinned(tmp_path: Path) -> None:
    seed = {**GROK_SERVICE, "pinned": False}
    path = _write(tmp_path, [seed])
    apply_service_migrations(tmp_path)
    items = _read(path)
    assert len(items) == 1 and items[0]["pinned"] is True


def test_missing_or_invalid_file_is_safe(tmp_path: Path) -> None:
    assert apply_service_migrations(tmp_path) is False
    (tmp_path / "services.json").write_text("{not json", encoding="utf-8")
    assert apply_service_migrations(tmp_path) is False


def test_shipped_catalog_has_no_canaletto_and_pinned_grok() -> None:
    root = Path(__file__).resolve().parents[1] / "hub" / "data"
    for name in ("default_services.json", "services.json"):
        items = json.loads((root / name).read_text(encoding="utf-8"))
        assert all(i["id"] != "canaletto-gallery" for i in items)
        grok = [i for i in items if i["id"] == "xai-grok"]
        assert len(grok) == 1 and grok[0]["pinned"] is True


def test_boot_point_runs_migration(tmp_path: Path, monkeypatch: Any) -> None:
    path = _write(tmp_path, [CANALETTO])

    class FakeService:
        data_dir = tmp_path

    monkeypatch.setattr(api, "HubService", FakeService)
    monkeypatch.setattr(api, "_service_instance", None)
    try:
        api.get_hub_service()
    finally:
        monkeypatch.setattr(api, "_service_instance", None)
    assert [i["id"] for i in _read(path)] == ["xai-grok"]
