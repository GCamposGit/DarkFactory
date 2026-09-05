"""
Deterministic tests for Session Learning Pack & Cognitive Uplift Engine.
Tests models, analyzer, generator, renderer, storage, CLI, and REST API.
Strict adherence to Universal Engineering Standards (AGENTS.md).
"""

import os
import json
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from core.learning_pack.models import (
    ConceptCategory,
    ExplanationTier,
    DefenseQA,
    TradeOffOption,
    LearningConcept,
    ActiveRecallCard,
    SessionLearningPack,
)
from core.learning_pack.analyzer import CodebaseAnalyzer
from core.learning_pack.generator import LearningPackGenerator
from core.learning_pack.renderer import LearningPackRenderer
from core.learning_pack.storage import LearningPackStore
from hub.backend.main import app
from hub.backend.service import HubService
from hub.backend.api import get_hub_service


@pytest.fixture
def temp_store(tmp_path):
    """Provides an isolated store instance in a temporary folder."""
    return LearningPackStore(storage_dir=tmp_path / "learning_packs")


@pytest.fixture
def sample_pack():
    """Generates a sample SessionLearningPack for testing."""
    concept = LearningConcept(
        concept_id="concept_pareto",
        name="Pareto Efficiency Frontier Optimization",
        category=ConceptCategory.ALGORITHMS,
        mental_anchor="An all-you-can-eat buffet budget curve.",
        tiers=ExplanationTier(
            pitch_30s="Mathematical filter choosing best AI models at lowest cost.",
            staff_architect="Multi-objective non-dominated sorting guaranteeing Pareto optimal boundary.",
            under_the_hood="O(N log N) dominance sorting on 2D coordinates.",
        ),
        trade_offs=[
            TradeOffOption(
                option="Pareto Frontier",
                pros="No arbitrary weights needed.",
                cons="Slightly higher compute than scalar score.",
                why_chosen="Provides unbiased non-dominated set.",
            )
        ],
        defense=[
            DefenseQA(
                question="Why not use a weighted sum?",
                bulletproof_answer="Weighted sums break when unit scales change.",
                context="core/benchmarks/frontier.py",
            )
        ],
        code_anchor="core/benchmarks/frontier.py",
    )

    card = ActiveRecallCard(
        card_id="card_001",
        concept_id="concept_pareto",
        front_prompt="What is a Pareto Frontier in model routing?",
        back_solution="The set of non-dominated models where no other model is both cheaper and smarter.",
        why_it_matters="Prevents routing to strictly suboptimal models.",
        tags=["Algorithms", "Routing", "Pareto"],
    )

    return SessionLearningPack(
        pack_id="pack_test_001",
        session_id="session_test_123",
        timestamp="2026-09-05T08:00:00Z",
        title="Test Architectural Learning Pack",
        executive_summary="Reinforced mathematical optimization and deterministic state control.",
        concepts=[concept],
        flashcards=[card],
        files_analyzed=["core/benchmarks/frontier.py"],
        metrics={"read_time_min": 3, "concepts_count": 1},
    )


# -------------------------------------------------------------
# 1. Models & Serialization Tests
# -------------------------------------------------------------
def test_models_serialization(sample_pack):
    data = sample_pack.to_dict()
    assert data["pack_id"] == "pack_test_001"
    assert len(data["concepts"]) == 1
    assert data["concepts"][0]["name"] == "Pareto Efficiency Frontier Optimization"
    assert data["concepts"][0]["tiers"]["pitch_30s"].startswith("Mathematical")
    assert len(data["flashcards"]) == 1

    # Roundtrip from_dict
    reconstructed = SessionLearningPack.from_dict(data)
    assert reconstructed.pack_id == sample_pack.pack_id
    assert reconstructed.title == sample_pack.title
    assert reconstructed.concepts[0].category == ConceptCategory.ALGORITHMS
    assert reconstructed.flashcards[0].front_prompt == sample_pack.flashcards[0].front_prompt


# -------------------------------------------------------------
# 2. Analyzer Pattern Recognition Tests
# -------------------------------------------------------------
def test_analyzer_pattern_detection(tmp_path):
    # Create mock files with specific architectural signatures
    fsm_file = tmp_path / "mock_state.py"
    fsm_file.write_text("class TaskStatus(str, Enum):\n    PLANNED = 'PLANNED'\n    def update_task_status(): pass\n", encoding="utf-8")

    analyzer = CodebaseAnalyzer(root_dir=tmp_path)
    patterns = analyzer.detect_patterns(file_paths=["mock_state.py"])

    assert len(patterns) >= 1
    ids = [p["id"] for p in patterns]
    assert "finite_state_machine" in ids


def test_analyzer_fallback():
    analyzer = CodebaseAnalyzer()
    # If no files matched, fallback should still produce default high-yield concepts
    patterns = analyzer.detect_patterns(file_paths=["non_existent_file.xyz"])
    assert len(patterns) >= 2


# -------------------------------------------------------------
# 3. Generator Tests
# -------------------------------------------------------------
def test_generator_synthesis():
    generator = LearningPackGenerator()
    pack = generator.generate_pack(title="Custom Title Test", session_id="session_gen_test")

    assert pack.title == "Custom Title Test"
    assert pack.session_id == "session_gen_test"
    assert len(pack.concepts) > 0
    assert len(pack.flashcards) >= len(pack.concepts)

    for c in pack.concepts:
        assert c.tiers.pitch_30s
        assert c.tiers.staff_architect
        assert c.tiers.under_the_hood
        assert c.mental_anchor


# -------------------------------------------------------------
# 4. Renderer Tests
# -------------------------------------------------------------
def test_renderer_markdown(sample_pack):
    md = LearningPackRenderer.render_markdown(sample_pack)
    assert "# 🧠 Test Architectural Learning Pack" in md
    assert "The 30-Second Elevator Pitch" in md
    assert "Staff+ Architectural Rationale" in md
    assert "Engine Room Mechanics" in md
    assert "Third-Party Defense Shield" in md
    assert "Active Recall Flashcards" in md
    assert "What is a Pareto Frontier in model routing?" in md


def test_renderer_anki_tsv(sample_pack):
    tsv = LearningPackRenderer.render_anki_tsv(sample_pack)
    lines = tsv.strip().split("\n")
    assert len(lines) == 1
    parts = lines[0].split("\t")
    assert len(parts) == 3
    assert "What is a Pareto Frontier" in parts[0]
    assert "non-dominated models" in parts[1]
    assert "Algorithms" in parts[2]


def test_renderer_html(sample_pack):
    html_content = LearningPackRenderer.render_html(sample_pack)
    assert "<!DOCTYPE html>" in html_content
    assert "Test Architectural Learning Pack" in html_content
    assert "flip-card" in html_content
    assert "copyPitch" in html_content
    assert "Mental Anchor:" in html_content


# -------------------------------------------------------------
# 5. Storage Tests
# -------------------------------------------------------------
def test_storage_lifecycle(temp_store, sample_pack):
    saved = temp_store.save_pack(sample_pack)
    assert Path(saved["json"]).exists()
    assert Path(saved["md"]).exists()
    assert Path(saved["html"]).exists()
    assert Path(saved["anki"]).exists()

    # Load by ID
    loaded = temp_store.load_pack("pack_test_001")
    assert loaded is not None
    assert loaded.title == sample_pack.title

    # Load by 'latest'
    latest = temp_store.get_latest_pack()
    assert latest is not None
    assert latest.pack_id == "pack_test_001"

    # List packs
    index = temp_store.list_packs()
    assert len(index) == 1
    assert index[0]["pack_id"] == "pack_test_001"
    assert index[0]["concepts_count"] == 1


# -------------------------------------------------------------
# 6. REST API Endpoints Tests
# -------------------------------------------------------------
def test_rest_api_endpoints():
    client = TestClient(app)

    # 1. Generate via API
    res_gen = client.post("/api/learning-packs/generate", json={"title": "FastAPI Integration Pack"})
    assert res_gen.status_code == 200
    gen_data = res_gen.json()
    assert "pack_id" in gen_data
    assert gen_data["concepts_count"] > 0
    pack_id = gen_data["pack_id"]

    # 2. List learning packs
    res_list = client.get("/api/learning-packs")
    assert res_list.status_code == 200
    packs = res_list.json()
    assert any(p["pack_id"] == pack_id for p in packs)

    # 3. Get latest pack
    res_latest = client.get("/api/learning-packs/latest")
    assert res_latest.status_code == 200
    latest = res_latest.json()
    assert latest["pack_id"] == pack_id

    # 4. Get specific pack
    res_single = client.get(f"/api/learning-packs/{pack_id}")
    assert res_single.status_code == 200
    assert res_single.json()["title"] == "FastAPI Integration Pack"

    # 5. Export Anki TSV
    res_anki = client.get(f"/api/learning-packs/{pack_id}/export-anki")
    assert res_anki.status_code == 200
    assert "text/tab-separated-values" in res_anki.headers.get("content-type", "")

    # 6. Get standalone HTML
    res_html = client.get(f"/api/learning-packs/{pack_id}/html")
    assert res_html.status_code == 200
    assert "text/html" in res_html.headers.get("content-type", "")
    assert "<!DOCTYPE html>" in res_html.text
