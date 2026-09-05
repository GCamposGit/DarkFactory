"""
Comprehensive Test Suite for Visual Asset & Image Studio.
Tests data models, semantic prompt synthesis, procedural local rendering (Pillow),
diagram generation, UI mockup rendering, app icon synthesis, and DarkHub REST APIs.
"""

from pathlib import Path
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from core.visual.models import (
    AssetType,
    VisualTheme,
    AspectRatio,
    VisualPromptSpec,
    VisualAssetResult,
    ASPECT_DIMENSIONS,
    PREVIEW_DIMENSIONS,
)
from core.visual.prompt_synthesizer import VisualPromptSynthesizer
from core.visual.procedural_engine import ProceduralVisualEngine
from core.visual.studio import VisualStudio
from hub.backend.main import app

client = TestClient(app)


# -------------------------------------------------------------
# 1. Models & Dimensions Tests
# -------------------------------------------------------------
def test_models_and_dimensions():
    spec = VisualPromptSpec(
        title="Distributed Queue Worker",
        subtitle="Zero-lock ring buffer architecture",
        asset_type=AssetType.SOCIAL_BANNER,
        theme=VisualTheme.MODERN_MINIMALIST_DARK,
        aspect_ratio=AspectRatio.RATIO_16_9,
        offline=True,
    )
    s_dict = spec.to_dict()
    assert s_dict["asset_type"] == "social_banner"
    assert s_dict["aspect_ratio"] == "16:9"

    restored = VisualPromptSpec.from_dict(s_dict)
    assert restored.title == "Distributed Queue Worker"
    assert restored.theme == VisualTheme.MODERN_MINIMALIST_DARK

    w, h = ASPECT_DIMENSIONS[AspectRatio.RATIO_16_9]
    assert (w, h) == (1920, 1080)
    w_prev, h_prev = PREVIEW_DIMENSIONS[AspectRatio.RATIO_16_9]
    assert (w_prev, h_prev) == (800, 450)


# -------------------------------------------------------------
# 2. Semantic Prompt Synthesizer Tests
# -------------------------------------------------------------
def test_synthesizer_from_markdown():
    text = (
        "# High-Throughput Event Sourcing\n"
        "Implementing an immutable log for sub-millisecond financial transaction auditing.\n"
        "We enforce deterministic serialization using protocol buffers and memory mapped files."
    )
    spec = VisualPromptSynthesizer.synthesize_from_text(text, asset_type=AssetType.BLOG_HERO)

    assert "High-Throughput Event Sourcing" in spec.title
    assert spec.asset_type == AssetType.BLOG_HERO
    assert len(spec.tags) >= 2
    assert spec.prompt is not None
    assert "Octane Render" in spec.prompt
    assert "watermark" in spec.negative_prompt


def test_synthesizer_theme_inference():
    # Terminal/CLI text -> Cyberpunk Terminal
    term_text = "Optimizing Linux kernel memory pages and CLI telemetry."
    spec_term = VisualPromptSynthesizer.synthesize_from_text(term_text)
    assert spec_term.theme == VisualTheme.CYBERPUNK_TERMINAL

    # Blueprint/invariants text -> Blueprint Technical
    bp_text = "Enforcing deterministic test harness and state machine invariants."
    spec_bp = VisualPromptSynthesizer.synthesize_from_text(bp_text)
    assert spec_bp.theme == VisualTheme.BLUEPRINT_TECHNICAL

    # UI/Dashboard text -> Glassmorphism
    ui_text = "Building a responsive frontend web UI dashboard with analytics cards."
    spec_ui = VisualPromptSynthesizer.synthesize_from_text(ui_text)
    assert spec_ui.theme == VisualTheme.GLASSMORPHISM


# -------------------------------------------------------------
# 3. Procedural Engine Rendering Tests
# -------------------------------------------------------------
def test_procedural_render_social_banner(tmp_path):
    engine = ProceduralVisualEngine(output_dir=tmp_path)
    spec = VisualPromptSpec(
        title="Dark Factory Nível 3",
        subtitle="Autonomia total de software e portões determinísticos",
        asset_type=AssetType.SOCIAL_BANNER,
        theme=VisualTheme.MODERN_MINIMALIST_DARK,
        aspect_ratio=AspectRatio.RATIO_16_9,
        tags=["autonomous", "python", "ai"],
    )
    res = engine.render(spec)

    assert Path(res.file_path).exists()
    assert res.provider == "local_procedural"
    assert res.width == 800
    assert res.height == 450

    # Verify Pillow can open the file and it is valid RGB image
    with Image.open(res.file_path) as img:
        assert img.size == (800, 450)
        assert img.mode == "RGB"


def test_procedural_render_architecture_diagram(tmp_path):
    engine = ProceduralVisualEngine(output_dir=tmp_path)
    spec = VisualPromptSpec(
        title="Multi-Tier Event Pipeline",
        asset_type=AssetType.ARCHITECTURE_DIAGRAM,
        theme=VisualTheme.BLUEPRINT_TECHNICAL,
        aspect_ratio=AspectRatio.RATIO_16_9,
    )
    res = engine.render(spec)

    assert Path(res.file_path).exists()
    with Image.open(res.file_path) as img:
        assert img.size == (800, 450)


def test_procedural_render_ui_mockup(tmp_path):
    engine = ProceduralVisualEngine(output_dir=tmp_path)
    spec = VisualPromptSpec(
        title="Dev Command Center",
        asset_type=AssetType.UI_MOCKUP,
        theme=VisualTheme.GLASSMORPHISM,
        aspect_ratio=AspectRatio.RATIO_16_9,
    )
    res = engine.render(spec)

    assert Path(res.file_path).exists()
    with Image.open(res.file_path) as img:
        assert img.size == (800, 450)


def test_procedural_render_app_icon(tmp_path):
    engine = ProceduralVisualEngine(output_dir=tmp_path)
    spec = VisualPromptSpec(
        title="DarkFac",
        asset_type=AssetType.APP_ICON,
        theme=VisualTheme.CLEAN_VECTOR_3D,
        aspect_ratio=AspectRatio.RATIO_1_1,
    )
    res = engine.render(spec)

    assert Path(res.file_path).exists()
    with Image.open(res.file_path) as img:
        assert img.size == (512, 512)


# -------------------------------------------------------------
# 4. Studio Façade & Text Coupling Tests
# -------------------------------------------------------------
def test_studio_illustrate_text_coupling(tmp_path):
    studio = VisualStudio(output_dir=tmp_path)
    article_text = (
        "# Lock-Free Ring Buffer in Modern C++\n"
        "Eliminating mutex contention in high-frequency trading gateways.\n"
        "We compare atomic compare-and-swap with bounded single-producer single-consumer queues."
    )
    res = studio.illustrate_text(
        text_content=article_text,
        asset_type=AssetType.BLOG_HERO,
        offline=True,
    )

    assert Path(res.file_path).exists()
    assert (tmp_path / "metadata" / f"{res.asset_id}.json").exists()
    assert len(studio.list_assets()) >= 1
    fetched = studio.get_asset(res.asset_id)
    assert fetched is not None
    assert fetched["asset_id"] == res.asset_id


# -------------------------------------------------------------
# 5. REST API Endpoints Integration Tests
# -------------------------------------------------------------
def test_api_visual_generate():
    payload = {
        "title": "Autonomous Coding Agent Fleet",
        "subtitle": "Hierarquia híbrida local e nuvem",
        "asset_type": "social_banner",
        "theme": "modern_minimalist_dark",
        "aspect_ratio": "16:9",
        "offline": True,
    }
    resp = client.post("/api/visual/generate", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["asset_id"].startswith("vis_")
    assert data["asset_type"] == "social_banner"
    assert Path(data["file_path"]).exists()


def test_api_visual_illustrate():
    payload = {
        "text": "Deterministic Validation Harness with Five-Tier Test Pyramid.",
        "asset_type": "blog_hero",
        "offline": True,
    }
    resp = client.post("/api/visual/illustrate", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["asset_id"].startswith("vis_")
    assert Path(data["file_path"]).exists()


def test_api_visual_gallery():
    resp = client.get("/api/visual/gallery")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
