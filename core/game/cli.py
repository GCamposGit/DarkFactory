"""CLI for the live three-tier Echo Garden build and headless verification."""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import List, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from core.game.factory import (
    build_asset_evidence,
    validate_local_contribution,
    validate_review_contribution,
    validate_strategy_contribution,
    verify_game_artifacts,
    write_game_artifacts,
)
from core.game.models import GameManifest, ModelEvidence
from core.visual import AssetType, AspectRatio, VisualPromptSpec, VisualStudio, VisualTheme
from hub.backend.models import PlaygroundProvider, UnifiedGenerateRequest, UnifiedGenerateResponse
from hub.backend.service import HubService


LOCAL_MODEL = "qwen-code-fast:latest"
BALANCED_MODEL = "mistralai/mistral-nemo"
FRONTIER_MODEL = "openai/gpt-6-astra"
FAST_IMAGE_MODEL = "google/gemini-3.1-flash-lite-image"
FRONTIER_IMAGE_MODEL = "openai/gpt-image-2"


def build_review_prompt(trace: dict) -> str:
    """Provide enough authoritative rules for an independent transition audit."""

    return (
        "Act as an adversarial code reviewer. Authoritative Echo Garden contract: start at [2,4,6]; "
        "on every turn compute after=clamp(before+direct_delta+echo_delta,0,8); on turn 1 echo_delta=[0,0,0]; "
        "on later turns echo_delta is the previous turn direct_delta rotated right from [a,b,c] to [c,a,b]. "
        "A game is won only on turns 3 through 6 when max(after)-min(after) <= 1; it is lost after turn 6 otherwise. "
        "Independently recompute every supplied turn and audit bounds, ordering, terminal status, and the win claim. Trace: "
        + json.dumps(trace, separators=(",", ":"))
        + '. Return one JSON object shaped like {"verdict":"APPROVE","risks_checked":["bounds","ordering","terminal status"],"summary":"8-300 chars"}. '
        'The verdict must be exactly "APPROVE" or "REJECT".'
    )


def _call_model(
    service: HubService,
    tier: str,
    provider: PlaygroundProvider,
    model: str,
    prompt: str,
    max_tokens: int,
) -> Tuple[str, ModelEvidence]:
    response: UnifiedGenerateResponse = service.generate_unified(
        UnifiedGenerateRequest(
            provider=provider,
            model=model,
            prompt=prompt,
            system="Return only the requested JSON object. No markdown, prose, links, or code fences.",
            temperature=0.0,
            max_tokens=max_tokens,
        )
    )
    text = response.response.strip()
    if not text:
        raise RuntimeError(f"{tier} returned an empty response")
    evidence = ModelEvidence(
        tier=tier,
        provider=response.provider.value,
        requested_model=model,
        returned_model=response.model,
        response_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        duration_ms=float(response.total_duration_ms or 0.0),
        tokens_used=response.tokens_used,
        cost_usd=response.cost_usd,
    )
    return text, evidence


def _generate_assets(output_dir: Path) -> List:
    assets_dir = output_dir / "assets"
    studio = VisualStudio(output_dir=assets_dir)
    shared_prompt = (
        "Use case: stylized-concept. Asset type: game environment concept art. "
        "Primary request: Echo Garden, three luminous energy channels weaving through a tiny nocturnal bioluminescent garden. "
        "Composition: wide clean game hero with a central triad, readable silhouettes, room for interface overlays. "
        "Palette: deep navy, cyan, warm ember, moss green. Constraints: no text, no logos, no watermark."
    )
    local = studio.create_asset(
        VisualPromptSpec(
            title="Echo Garden Local Signal Map",
            subtitle="Three channels, one delayed echo",
            asset_type=AssetType.ARCHITECTURE_DIAGRAM,
            theme=VisualTheme.BLUEPRINT_TECHNICAL,
            aspect_ratio=AspectRatio.RATIO_16_9,
            offline=True,
        )
    )
    fast = studio.create_asset(
        VisualPromptSpec(
            title="Echo Garden Fast Draft",
            asset_type=AssetType.BLOG_HERO,
            theme=VisualTheme.CLEAN_VECTOR_3D,
            aspect_ratio=AspectRatio.RATIO_16_9,
            prompt=shared_prompt,
            model_override=FAST_IMAGE_MODEL,
            strict_provider=True,
        )
    )
    frontier = studio.create_asset(
        VisualPromptSpec(
            title="Echo Garden Frontier Hero",
            asset_type=AssetType.BLOG_HERO,
            theme=VisualTheme.GLASSMORPHISM,
            aspect_ratio=AspectRatio.RATIO_16_9,
            prompt=shared_prompt,
            model_override=FRONTIER_IMAGE_MODEL,
            high_res=True,
            strict_provider=True,
        )
    )
    return [
        build_asset_evidence(local, output_dir, "local_procedural", "darkfac-vector-v1"),
        build_asset_evidence(fast, output_dir, "fast_cloud", FAST_IMAGE_MODEL),
        build_asset_evidence(frontier, output_dir, "frontier_cloud", FRONTIER_IMAGE_MODEL),
    ]


def build_live(output_dir: Path, seed: int) -> dict:
    service = HubService()
    local_text, local_evidence = _call_model(
        service,
        "local_fast",
        PlaygroundProvider.OLLAMA,
        LOCAL_MODEL,
        "Design terse UI copy for a three-action deterministic puzzle. Return exactly "
        '{"tagline":"8-90 chars","move_labels":{"weave":"2-18 chars","echo":"2-18 chars","ground":"2-18 chars"}}.',
        180,
    )
    local = validate_local_contribution(local_text)

    strategy_text, strategy_evidence = _call_model(
        service,
        "balanced_cloud",
        PlaygroundProvider.OPENROUTER,
        BALANCED_MODEL,
        "Solve this deterministic coding fixture. Start energies are [2,4,6]. Moves are "
        "weave=[1,0,-1], echo=[-1,1,0], ground=[0,-1,1]. From turn 2 onward, also add the previous move delta rotated right: [a,b,c] becomes [c,a,b]. Win on turn 3-6 when max-min <= 1. "
        'Return exactly one JSON object shaped like {"moves":["weave","ground","weave"],"rationale":"8-240 chars"}. '
        'The moves array must have 3-6 items. Every item must be exactly one enum value: "weave", "echo", or "ground"; never join alternatives with punctuation.',
        220,
    )
    strategy, trace = validate_strategy_contribution(strategy_text, seed=seed)

    review_prompt = build_review_prompt(trace)
    review_text, review_evidence = _call_model(
        service,
        "frontier_cloud",
        PlaygroundProvider.OPENROUTER,
        FRONTIER_MODEL,
        review_prompt,
        1200,
    )
    review = validate_review_contribution(review_text)
    assets = _generate_assets(output_dir)
    manifest = GameManifest(
        seed=seed,
        local_contribution=local,
        strategy_contribution=strategy,
        review_contribution=review,
        model_evidence=[local_evidence, strategy_evidence, review_evidence],
        assets=assets,
    )
    manifest_path, html_path = write_game_artifacts(manifest, output_dir)
    verification = verify_game_artifacts(output_dir)
    return {
        **verification,
        "manifest_path": str(manifest_path.resolve()),
        "html_path": str(html_path.resolve()),
        "total_cost_usd": round(
            sum(e.cost_usd or 0.0 for e in manifest.model_evidence)
            + sum(asset.cost_usd for asset in assets),
            6,
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Echo Garden Dark Factory E2E")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build", help="Run real local/OpenRouter model and image tiers")
    build_parser.add_argument("--live", action="store_true", help="Required acknowledgement for paid/live provider calls")
    build_parser.add_argument("--output-dir", default=".factory/e2e_game")
    build_parser.add_argument("--seed", type=int, default=0)
    verify_parser = subparsers.add_parser("verify", help="Verify an existing portable build")
    verify_parser.add_argument("--output-dir", default=".factory/e2e_game")
    args = parser.parse_args()

    if args.command == "build":
        if not args.live:
            parser.error("build requires --live so provider calls are never accidental")
        result = build_live(Path(args.output_dir), seed=args.seed)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print("[E2E_PASS] echo_garden_live_build")
    else:
        result = verify_game_artifacts(Path(args.output_dir))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print("[E2E_PASS] echo_garden_artifact_verification")


if __name__ == "__main__":
    main()
