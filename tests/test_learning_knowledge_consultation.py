"""Tests for DH-06 (USR-53): Learning Packs and Knowledge Consultation in DarkHub.

Validates that:
1. Learning packs API endpoints (listing, latest, detail, Anki export, HTML rendering)
   respond correctly and serve genuine cognitive data.
2. The frontend drawer and consultation script (learning.js) provide rich
   read-only consultation for Feynman levels, mental models, and flashcards.
3. DarkHub adheres strictly to read-only mode with zero mutations or workflow execution.
"""

from pathlib import Path
import pytest
from starlette.testclient import TestClient

from core.learning_pack.generator import LearningPackGenerator
from core.learning_pack.storage import LearningPackStore
from hub.backend.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def ensure_learning_pack():
    """Ensure at least one learning pack is available in the store for testing."""
    store = LearningPackStore()
    if not store.list_packs():
        generator = LearningPackGenerator()
        pack = generator.generate_pack(title="DH-06 Verification Pack")
        store.save_pack(pack)


@pytest.fixture
def client():
    return TestClient(app)


def test_learning_packs_list_endpoint(client):
    response = client.get("/api/learning-packs")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    # Validate summary fields
    first = data[0]
    assert "pack_id" in first
    assert "title" in first


def test_learning_packs_latest_endpoint(client):
    response = client.get("/api/learning-packs/latest")
    assert response.status_code == 200
    pack = response.json()
    assert "pack_id" in pack
    assert "title" in pack
    assert "executive_summary" in pack
    assert "concepts" in pack
    assert isinstance(pack["concepts"], list)
    if pack["concepts"]:
        concept = pack["concepts"][0]
        assert "name" in concept
        assert "tiers" in concept or "feynman_levels" in concept
        assert "mental_anchor" in concept or "analogical_anchor" in concept


def test_learning_pack_by_id_and_exports(client):
    # Fetch list to pick an existing ID
    list_res = client.get("/api/learning-packs")
    assert list_res.status_code == 200
    packs = list_res.json()
    assert len(packs) >= 1
    pack_id = packs[0]["pack_id"]

    # Test single pack detail
    detail_res = client.get(f"/api/learning-packs/{pack_id}")
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert detail["pack_id"] == pack_id

    # Test Anki export TSV
    anki_res = client.get(f"/api/learning-packs/{pack_id}/export-anki")
    assert anki_res.status_code == 200
    assert "text/tab-separated-values" in anki_res.headers.get("content-type", "")

    # Test standalone HTML rendering
    html_res = client.get(f"/api/learning-packs/{pack_id}/html")
    assert html_res.status_code == 200
    assert "text/html" in html_res.headers.get("content-type", "")
    assert "<html" in html_res.text.lower()


def test_learning_frontend_drawer_dom():
    index_html = (REPO_ROOT / "hub" / "frontend" / "index.html").read_text(encoding="utf-8")
    # Drawer container exists
    assert 'id="learning-drawer"' in index_html
    # Header trigger button exists
    assert "openLearningDrawer()" in index_html
    # Tab buttons exist
    assert 'id="learn-tab-latest"' in index_html
    assert 'id="learn-tab-history"' in index_html
    assert 'id="learn-tab-flashcards"' in index_html
    assert 'id="learn-tab-knowledge"' in index_html
    # Views exist
    assert 'id="learn-view-latest"' in index_html
    assert 'id="learn-view-history"' in index_html
    assert 'id="learn-view-flashcards"' in index_html
    assert 'id="learn-view-knowledge"' in index_html
    # Script tag is included with canonical cache version
    assert 'src="/static/learning.js?v=20260924b"' in index_html


def test_learning_js_is_read_only_and_defines_handlers():
    learning_js = (REPO_ROOT / "hub" / "frontend" / "learning.js").read_text(encoding="utf-8")
    # Verify core UI functions
    assert "function openLearningDrawer" in learning_js
    assert "function closeLearningDrawer" in learning_js
    assert "function switchLearningTab" in learning_js
    assert "function loadLatestLearningPack" in learning_js
    assert "function loadLearningPacks" in learning_js
    assert "function inspectLearningPack" in learning_js
    assert "function openLearningPackHtml" in learning_js
    assert "function exportLearningPackAnki" in learning_js

    # Verify read-only guarantees: no mutations to runs, tickets, or deploy endpoints
    assert "/api/demands/tickets" not in learning_js
    assert "/api/cloud/deploy" not in learning_js
    assert "DELETE" not in learning_js
    assert "PATCH" not in learning_js
    assert 'method: "POST"' not in learning_js
    assert "method: 'POST'" not in learning_js
