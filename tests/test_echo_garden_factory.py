"""Integration tests for one-shot contribution validation and export."""

import hashlib
from pathlib import Path

import pytest

from core.game.factory import (
    render_game_html,
    validate_local_contribution,
    validate_review_contribution,
    validate_strategy_contribution,
    verify_game_artifacts,
    write_game_artifacts,
)
from core.game.models import (
    AssetEvidence,
    GameManifest,
    LocalGameContribution,
    ModelEvidence,
    ReviewContribution,
    StrategyContribution,
)
from core.game.cli import build_review_prompt


def _manifest(tmp_path: Path) -> GameManifest:
    asset = tmp_path / "assets" / "hero.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"deterministic-image-fixture")
    return GameManifest(
        local_contribution=LocalGameContribution(
            tagline="Balance the garden before the echo returns.",
            move_labels={"weave": "Weave", "echo": "Reflect", "ground": "Ground"},
        ),
        strategy_contribution=StrategyContribution(
            moves=["weave", "ground", "weave"],
            rationale="The third direct pulse and the rotated ground echo cancel the spread.",
        ),
        review_contribution=ReviewContribution(
            verdict="APPROVE",
            risks_checked=["energy bounds", "turn budget", "echo ordering"],
            summary="The trace reaches exact resonance on turn three without leaving bounds.",
        ),
        model_evidence=[
            ModelEvidence(tier="local_fast", provider="ollama", requested_model="local", returned_model="local", response_sha256="a" * 64, duration_ms=1),
            ModelEvidence(tier="balanced_cloud", provider="openrouter", requested_model="mid", returned_model="mid", response_sha256="b" * 64, duration_ms=2),
            ModelEvidence(tier="frontier_cloud", provider="openrouter", requested_model="high", returned_model="high", response_sha256="c" * 64, duration_ms=3),
        ],
        assets=[
            AssetEvidence(
                tier="frontier_cloud",
                provider="openrouter",
                requested_model="image",
                returned_model="image",
                relative_path="assets/hero.png",
                sha256=hashlib.sha256(asset.read_bytes()).hexdigest(),
                size_bytes=asset.stat().st_size,
                width=32,
                height=24,
            )
        ],
    )


def test_structured_contributions_accept_fenced_json_and_verify_strategy() -> None:
    local = validate_local_contribution(
        '```json\n{"tagline":"A small echo changes everything.","move_labels":{"weave":"Weave","echo":"Reflect","ground":"Ground"}}\n```'
    )
    strategy, trace = validate_strategy_contribution(
        '{"moves":["weave","ground","weave"],"rationale":"The rotated echo closes the final two-point gap."}'
    )
    review = validate_review_contribution(
        '{"verdict":"APPROVE","risks_checked":["bounds","turns","echo order"],"summary":"The trace is valid and deterministic."}'
    )
    assert local.move_labels["weave"] == "Weave"
    assert strategy.moves[-1].value == "weave"
    assert trace["final_status"] == "won"
    assert trace["turns"][1] == {
        "turn": 2,
        "move": "ground",
        "before": [3, 4, 5],
        "direct_delta": [0, -1, 1],
        "echo_delta": [-1, 1, 0],
        "after": [2, 4, 6],
        "spread": 4,
        "status": "running",
    }
    assert review.verdict == "APPROVE"


def test_technical_transition_arrow_is_safe_plain_text(tmp_path: Path) -> None:
    local = validate_local_contribution(
        '{"tagline":"Move A -> B while spread <= 1.","move_labels":{"weave":"Weave","echo":"Reflect","ground":"Ground"}}'
    )
    assert "->" in local.tagline
    assert "<=" in local.tagline
    manifest = _manifest(tmp_path)
    manifest.local_contribution.tagline = "<script>alert(1)</script>"
    html = render_game_html(manifest)
    assert "<script>alert" not in html
    assert "\\u003cscript>" in html


def test_invalid_or_active_model_data_is_rejected() -> None:
    with pytest.raises(ValueError):
        validate_strategy_contribution(
            '{"moves":["weave|echo|ground","ground","weave"],"rationale":"Pipe-joined enum alternatives are invalid data."}'
        )
    with pytest.raises(ValueError, match="does not win"):
        validate_strategy_contribution(
            '{"moves":["echo","echo","echo"],"rationale":"This intentionally misses resonance."}'
        )
    with pytest.raises(ValueError, match="unsafe"):
        validate_local_contribution(
            '{"tagline":"Open https://example.com now","move_labels":{"weave":"A","echo":"B","ground":"C"}}'
        )
    with pytest.raises(ValueError, match="rejected"):
        validate_review_contribution(
            '{"verdict":"REJECT","risks_checked":["bounds","turns","echo"],"summary":"A real issue remains unresolved."}'
        )


def test_review_prompt_contains_recomputable_authoritative_contract() -> None:
    _, trace = validate_strategy_contribution(
        '{"moves":["weave","ground","weave"],"rationale":"Known deterministic winning fixture."}'
    )
    prompt = build_review_prompt(trace)
    assert "after=clamp(before+direct_delta+echo_delta,0,8)" in prompt
    assert "previous turn direct_delta rotated right" in prompt
    assert "won only on turns 3 through 6" in prompt
    assert '"verdict":"APPROVE|REJECT"' not in prompt


def test_portable_html_and_manifest_round_trip(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    manifest_path, html_path = write_game_artifacts(manifest, tmp_path)
    html = render_game_html(manifest)
    verification = verify_game_artifacts(tmp_path)

    assert manifest_path.exists() and html_path.exists()
    assert "<script src=" not in html
    assert "https://" not in html and "http://" not in html
    assert "data-move" in html
    assert "button.dataset.move=name" in html
    assert verification == {
        "status": "passed",
        "models_verified": 3,
        "assets_verified": 1,
        "winning_turns": 3,
        "final_status": "won",
    }


def test_verifier_detects_asset_tampering(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    write_game_artifacts(manifest, tmp_path)
    (tmp_path / "assets" / "hero.png").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_game_artifacts(tmp_path)


def test_verifier_detects_html_manifest_split_brain(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    write_game_artifacts(manifest, tmp_path)
    (tmp_path / "index.html").write_text("stale or partial", encoding="utf-8")

    with pytest.raises(ValueError, match="committed manifest"):
        verify_game_artifacts(tmp_path)
