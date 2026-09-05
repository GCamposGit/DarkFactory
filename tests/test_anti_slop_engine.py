"""
Comprehensive Test Suite for Anti-AI-Slop Content Engine.
Tests data models, deterministic linter, scrub replacements, cadence analysis,
persona presets, offline procedural generation, and DarkHub REST API endpoints.
"""

import pytest
from fastapi.testclient import TestClient

from core.content.models import (
    ContentType,
    ToneProfile,
    SlopCategory,
    SeverityLevel,
    CleanlinessRating,
    SlopViolation,
    SlopReport,
    ContentRequest,
    ContentResponse,
)
from core.content.anti_slop_linter import AntiSlopLinter
from core.content.presets import CONTENT_PRESETS, get_preset
from core.content.engine import ContentEngine
from hub.backend.main import app

client = TestClient(app)


# -------------------------------------------------------------
# 1. Models & Serialization Tests
# -------------------------------------------------------------
def test_models_serialization():
    tone = ToneProfile(formality=4, brevity=5, technical_depth=4, contrarianism=3)
    t_dict = tone.to_dict()
    restored_tone = ToneProfile.from_dict(t_dict)
    assert restored_tone.formality == 4
    assert restored_tone.contrarianism == 3

    req = ContentRequest(
        topic="Distributed Consensus",
        content_type=ContentType.TECHNICAL_BLOG,
        key_points=["Raft log compaction", "Heartbeat timers"],
        tone_profile=tone,
    )
    r_dict = req.to_dict()
    assert r_dict["content_type"] == "technical_blog"
    assert len(r_dict["key_points"]) == 2

    restored_req = ContentRequest.from_dict(r_dict)
    assert restored_req.topic == "Distributed Consensus"
    assert restored_req.tone_profile.formality == 4


# -------------------------------------------------------------
# 2. Anti-Slop Linter Tests
# -------------------------------------------------------------
def test_linter_detects_severe_slop_buzzwords():
    linter = AntiSlopLinter()
    toxic_text = (
        "In today's fast-paced digital world, we must delve into the tapestry of AI "
        "to unleash a game-changer paradigm shift and harness the power of neural systems."
    )
    report = linter.audit(toxic_text)

    assert report.slop_score > 35.0
    assert report.cleanliness_rating in [CleanlinessRating.HIGH_SLOP, CleanlinessRating.TOXIC_SLOP]
    assert report.violations_count >= 4

    terms = [v.term.lower() for v in report.violations]
    assert any("delve" in t for t in terms)
    assert any("tapestry" in t for t in terms)
    assert any("game-changer" in t for t in terms)
    assert any("unleash" in t for t in terms)


def test_linter_detects_portuguese_slop():
    linter = AntiSlopLinter()
    pt_text = (
        "No mundo acelerado de hoje, este mergulho profundo na tapeçaria de microsserviços "
        "é um divisor de águas que vai revolucionar a arquitetura."
    )
    report = linter.audit(pt_text)

    assert report.slop_score > 30.0
    terms = [v.term.lower() for v in report.violations]
    assert any("mergulho profundo" in t for t in terms)
    assert any("divisor de águas" in t for t in terms)
    assert any("revolucionar" in t for t in terms)


def test_linter_approves_pristine_technical_prose():
    linter = AntiSlopLinter()
    pristine_text = (
        "The storage engine utilizes an append-only write-ahead log. "
        "Every write flushes to disk synchronously. "
        "Throughput scales linearly across partitions. "
        "When a crash occurs, state recovery completes within 40 milliseconds."
    )
    report = linter.audit(pristine_text)

    assert report.slop_score < 10.0
    assert report.cleanliness_rating in [CleanlinessRating.PRISTINE, CleanlinessRating.CLEAN]
    assert report.violations_count == 0


def test_linter_cadence_monotony_penalty():
    linter = AntiSlopLinter()
    # 5 sentences of exact same word length (10 words each) -> zero variance
    monotonous_text = (
        "This is a system that processes data in real time. "
        "Every worker node handles an equal partition of incoming messages. "
        "The cluster manager monitors heartbeat intervals on all worker nodes. "
        "When failure occurs the system triggers an immediate failover election. "
        "State consistency is maintained across memory buffers without manual intervention."
    )
    report = linter.audit(monotonous_text)
    assert report.cadence_rating == "Monotonous Robotic Droning"
    assert report.sentence_length_variance < 8.0
    assert any(v.category == SlopCategory.CADENCE_MONOTONY for v in report.violations)


def test_linter_custom_banned_words():
    linter = AntiSlopLinter(custom_banned_words=["synergy", "paradigm", "disrupt"])
    text = "Our team delivers strong synergy across data streams."
    report = linter.audit(text)
    assert any(v.term.lower() == "synergy" for v in report.violations)


def test_linter_scrub():
    linter = AntiSlopLinter()
    messy = "We need to delve into the codebase and unleash the power of test automation."
    clean, count = linter.scrub(messy)

    assert count >= 2
    assert "delve" not in clean.lower()
    assert "unleash" not in clean.lower()
    assert "examine" in clean.lower()


# -------------------------------------------------------------
# 3. Content Engine Generation Tests
# -------------------------------------------------------------
def test_engine_generate_linkedin_post_offline(tmp_path):
    engine = ContentEngine(storage_dir=tmp_path)
    req = ContentRequest(
        topic="Zero-Downtime Database Migrations",
        content_type=ContentType.LINKEDIN_POST,
        key_points=[
            "Expand and contract pattern",
            "Backfill data asynchronously",
            "Drop deprecated columns in separate release",
        ],
        offline=True,
    )
    resp = engine.generate(req)

    assert resp.content_id.startswith("cnt_")
    assert resp.content_type == ContentType.LINKEDIN_POST
    assert resp.final_slop_score < 15.0
    assert resp.slop_report.cleanliness_rating in [CleanlinessRating.PRISTINE, CleanlinessRating.CLEAN]
    assert "Zero-Downtime Database Migrations" in resp.final_content
    assert "Expand and contract pattern" in resp.final_content
    assert (tmp_path / f"{resp.content_id}.json").exists()


def test_engine_generate_technical_blog_offline(tmp_path):
    engine = ContentEngine(storage_dir=tmp_path)
    req = ContentRequest(
        topic="High-Throughput Lock-Free Queues",
        content_type=ContentType.TECHNICAL_BLOG,
        key_points=["Atomic compare-and-swap", "Cacheline padding to prevent false sharing"],
        offline=True,
    )
    resp = engine.generate(req)

    assert resp.content_type == ContentType.TECHNICAL_BLOG
    assert "# Deep Dive:" in resp.final_content
    assert "Benchmark Verification" in resp.final_content
    assert resp.final_slop_score < 15.0


def test_engine_generate_commercial_proposal_offline(tmp_path):
    engine = ContentEngine(storage_dir=tmp_path)
    req = ContentRequest(
        topic="Autonomous Dark Factory Implementation",
        content_type=ContentType.COMMERCIAL_PROPOSAL,
        key_points=["Deterministic test harness", "Pareto model router", "Cognitive uplift packs"],
        offline=True,
    )
    resp = engine.generate(req)

    assert resp.content_type == ContentType.COMMERCIAL_PROPOSAL
    assert "Deliverables & Scope of Work" in resp.final_content
    assert "ROI & Risk Mitigation" in resp.final_content


def test_engine_presets():
    for ctype in ContentType:
        preset = get_preset(ctype)
        assert "title" in preset
        assert "system_prompt" in preset
        assert "default_tone" in preset


# -------------------------------------------------------------
# 4. REST API Endpoints Integration Tests
# -------------------------------------------------------------
def test_api_content_generate():
    payload = {
        "topic": "Microservices Reliability Invariants",
        "content_type": "linkedin_post",
        "target_audience": "backend engineers",
        "key_points": ["Circuit breakers", "Bulkheads", "Idempotent retries"],
        "offline": True,
    }
    resp = client.post("/api/content/generate", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["content_id"].startswith("cnt_")
    assert data["content_type"] == "linkedin_post"
    assert "Microservices Reliability" in data["final_content"]
    assert data["final_slop_score"] < 15.0


def test_api_content_lint():
    payload = {
        "text": "In today's fast-paced digital world, this game-changer tool will unleash a paradigm shift.",
    }
    resp = client.post("/api/content/lint", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["slop_score"] > 25.0
    assert data["violations_count"] >= 3
    assert len(data["top_fixes"]) >= 3


def test_api_content_presets():
    resp = client.get("/api/content/presets")
    assert resp.status_code == 200
    data = resp.json()

    assert isinstance(data, list)
    assert len(data) >= 7
    types = [p["type"] for p in data]
    assert "linkedin_post" in types
    assert "technical_blog" in types
    assert "commercial_proposal" in types
