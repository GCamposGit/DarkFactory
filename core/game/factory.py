"""Validation and artifact export for the Echo Garden one-shot factory.

# [RESEARCH PROVENANCE & INSIGHTS]
# Ledger ID: 20260905_dark_factory_game_reuse
# Audit Doc: .factory/research/20260905_dark_factory_game_reuse/INSIGHTS.md
# Canonical Sources: .factory/research/20260905_dark_factory_game_reuse/ledger.json
"""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple, Type, TypeVar

from pydantic import BaseModel

from core.game.engine import MOVE_DELTAS, play_sequence
from core.game.models import (
    AssetEvidence,
    GameManifest,
    GameMove,
    GameStatus,
    LocalGameContribution,
    ModelEvidence,
    ReviewContribution,
    StrategyContribution,
)
from core.visual.models import VisualAssetResult


_ModelT = TypeVar("_ModelT", bound=BaseModel)
_UNSAFE_TEXT = re.compile(r"(?:https?://|javascript:)", re.IGNORECASE)


def _extract_json_object(text: str) -> Dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model response did not contain a JSON object")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"model response contained invalid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("model JSON response must be an object")
    return payload


def _validate_safe_strings(value: Any) -> None:
    if isinstance(value, str) and _UNSAFE_TEXT.search(value):
        raise ValueError("model contribution contains unsafe markup or URL content")
    if isinstance(value, dict):
        for nested in value.values():
            _validate_safe_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            _validate_safe_strings(nested)


def parse_contribution(text: str, model_type: Type[_ModelT]) -> _ModelT:
    """Extract one JSON object, reject active content, and apply a Pydantic contract."""

    payload = _extract_json_object(text)
    _validate_safe_strings(payload)
    return model_type.model_validate(payload)


def validate_local_contribution(text: str) -> LocalGameContribution:
    contribution = parse_contribution(text, LocalGameContribution)
    if set(contribution.move_labels) != set(GameMove):
        raise ValueError("local contribution must label all three canonical moves")
    if any(not 2 <= len(label.strip()) <= 18 for label in contribution.move_labels.values()):
        raise ValueError("move labels must contain between 2 and 18 characters")
    return contribution


def validate_strategy_contribution(text: str, seed: int = 0) -> Tuple[StrategyContribution, Dict[str, Any]]:
    contribution = parse_contribution(text, StrategyContribution)
    result = play_sequence(contribution.moves, seed=seed)
    if result.final.status != GameStatus.WON:
        raise ValueError("balanced-tier candidate sequence does not win Echo Garden")
    trace = {
        "initial": list(result.initial.energies),
        "turns": [
            {
                "turn": item.after.turn,
                "move": item.move.value,
                "before": list(item.before.energies),
                "direct_delta": list(item.direct_delta),
                "echo_delta": list(item.echo_delta),
                "after": list(item.after.energies),
                "spread": item.spread,
                "status": item.after.status.value,
            }
            for item in result.traces
        ],
        "final_status": result.final.status.value,
    }
    return contribution, trace


def validate_review_contribution(text: str) -> ReviewContribution:
    contribution = parse_contribution(text, ReviewContribution)
    if contribution.verdict != "APPROVE":
        raise ValueError("frontier review rejected the one-shot candidate")
    if len({risk.casefold() for risk in contribution.risks_checked}) != len(contribution.risks_checked):
        raise ValueError("frontier review must report distinct risk checks")
    return contribution


def build_asset_evidence(
    result: VisualAssetResult,
    output_dir: Path,
    tier: str,
    requested_model: str,
) -> AssetEvidence:
    file_path = Path(result.file_path).resolve()
    root = output_dir.resolve()
    if not file_path.is_relative_to(root):
        raise ValueError("visual artifact escaped the E2E output directory")
    payload = file_path.read_bytes()
    return AssetEvidence(
        tier=tier,
        provider=result.provider,
        requested_model=requested_model,
        returned_model=result.model_used,
        relative_path=file_path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        width=result.width,
        height=result.height,
        cost_usd=result.cost_usd,
    )


def _json_for_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def render_game_html(manifest: GameManifest) -> str:
    """Render a dependency-free browser game without interpolating executable model text."""

    hero = manifest.assets[-1].relative_path
    public_manifest = {
        "seed": manifest.seed,
        "tagline": manifest.local_contribution.tagline,
        "moveLabels": {
            move.value: manifest.local_contribution.move_labels[move]
            for move in GameMove
        },
        "strategy": [move.value for move in manifest.strategy_contribution.moves],
        "hero": hero,
    }
    game_data = _json_for_script(public_manifest)
    delta_data = _json_for_script({move.value: list(delta) for move, delta in MOVE_DELTAS.items()})
    template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Echo Garden</title>
<style>
:root{color-scheme:dark;font-family:Inter,Segoe UI,sans-serif;background:#07131d;color:#e8fff7}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at top,#15394a,#07131d 58%)}main{width:min(920px,94vw);border:1px solid #3be7c4;border-radius:24px;overflow:hidden;background:#0b1f2ddd;box-shadow:0 24px 80px #0008}.hero{min-height:260px;padding:36px;background:linear-gradient(90deg,#07131ddd,#07131d44),url('__HERO__') center/cover}.hero h1{font-size:clamp(42px,8vw,78px);margin:0;letter-spacing:-.06em}.hero p{max-width:520px;font-size:18px}.panel{padding:28px}.channel{display:grid;grid-template-columns:90px 1fr 40px;gap:12px;align-items:center;margin:14px 0}.track{height:20px;background:#173040;border-radius:99px;overflow:hidden}.fill{height:100%;width:0;background:linear-gradient(90deg,#3be7c4,#d8ff70);transition:width .25s}.actions{display:flex;gap:12px;flex-wrap:wrap;margin-top:24px}button{border:1px solid #3be7c4;background:#102d3d;color:#e8fff7;border-radius:12px;padding:12px 18px;font-weight:700;cursor:pointer}button:hover{background:#17465a}button:disabled{opacity:.45;cursor:not-allowed}#message{min-height:48px;color:#bfeee4}.meta{color:#83b9b1;font-size:14px}</style>
</head>
<body><main><section class="hero"><h1>Echo Garden</h1><p id="tagline"></p></section><section class="panel"><p>Balance all three channels by turn six. Your previous pulse returns rotated on the next turn.</p><div id="channels"></div><p id="message"></p><p class="meta" id="meta"></p><div class="actions" id="actions"></div><div class="actions"><button id="reset">Reset</button><button id="hint">Show verified one-shot path</button></div></section></main>
<script type="application/json" id="game-data">__GAME_DATA__</script>
<script>
(()=>{'use strict';const cfg=JSON.parse(document.getElementById('game-data').textContent);const deltas=__DELTA_DATA__;const names=['EMBER','TIDE','MOSS'];let state;const rotate=v=>[v[2],v[0],v[1]];const start=()=>{const base=[2,4,6],n=((cfg.seed%3)+3)%3;state={energies:n?[...base.slice(-n),...base.slice(0,-n)]:base,turn:0,pending:null,status:'running'};draw('Choose a pulse.');};const move=name=>{if(state.status!=='running')return;const direct=deltas[name],echo=state.pending?rotate(deltas[state.pending]):[0,0,0];state.energies=state.energies.map((v,i)=>Math.max(0,Math.min(8,v+direct[i]+echo[i])));state.turn++;state.pending=name;const spread=Math.max(...state.energies)-Math.min(...state.energies);if(state.turn>=3&&spread<=1)state.status='won';else if(state.turn>=6)state.status='lost';draw(state.status==='won'?'Resonance achieved.':state.status==='lost'?'The garden fell out of phase.':'The previous pulse echoed sideways.');};const draw=message=>{document.getElementById('tagline').textContent=cfg.tagline;document.getElementById('channels').innerHTML='';state.energies.forEach((value,index)=>{const row=document.createElement('div');row.className='channel';const label=document.createElement('strong');label.textContent=names[index];const track=document.createElement('div');track.className='track';const fill=document.createElement('div');fill.className='fill';fill.style.width=(value/8*100)+'%';track.append(fill);const score=document.createElement('span');score.textContent=String(value);row.append(label,track,score);document.getElementById('channels').append(row);});document.getElementById('message').textContent=message;document.getElementById('meta').textContent=`Turn ${state.turn}/6 | Next echo: ${state.pending||'none'} | ${state.status.toUpperCase()}`;document.querySelectorAll('[data-move]').forEach(button=>button.disabled=state.status!=='running');};Object.keys(deltas).forEach(name=>{const button=document.createElement('button');button.dataset.move=name;button.textContent=cfg.moveLabels[name];button.addEventListener('click',()=>move(name));document.getElementById('actions').append(button);});document.getElementById('reset').addEventListener('click',start);document.getElementById('hint').addEventListener('click',()=>{document.getElementById('message').textContent='Verified path: '+cfg.strategy.map(x=>cfg.moveLabels[x]).join(' -> ');});start();})();
</script></body></html>"""
    return (
        template.replace("__HERO__", hero)
        .replace("__GAME_DATA__", game_data)
        .replace("__DELTA_DATA__", delta_data)
    )


def write_game_artifacts(manifest: GameManifest, output_dir: Path) -> Tuple[Path, Path]:
    """Publish the HTML first and the manifest as the final atomic commit marker."""

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    html_path = output_dir / "index.html"
    _atomic_write_text(html_path, render_game_html(manifest))
    _atomic_write_text(manifest_path, manifest.model_dump_json(indent=2))
    return manifest_path, html_path


def _atomic_write_text(target: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def verify_game_artifacts(output_dir: Path) -> Dict[str, Any]:
    """Recompute every portable invariant and return deterministic verification evidence."""

    manifest_path = output_dir / "manifest.json"
    html_path = output_dir / "index.html"
    manifest = GameManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    strategy, trace = validate_strategy_contribution(
        manifest.strategy_contribution.model_dump_json(), seed=manifest.seed
    )
    if strategy != manifest.strategy_contribution:
        raise ValueError("strategy round-trip changed the manifest")
    for asset in manifest.assets:
        target = (output_dir / asset.relative_path).resolve()
        if not target.is_relative_to(output_dir.resolve()):
            raise ValueError("manifest asset escaped the output directory")
        payload = target.read_bytes()
        if hashlib.sha256(payload).hexdigest() != asset.sha256:
            raise ValueError(f"asset hash mismatch: {asset.relative_path}")
    html = html_path.read_text(encoding="utf-8")
    if html != render_game_html(manifest):
        raise ValueError("generated HTML does not match its committed manifest")
    if "http://" in html or "https://" in html or "<script src=" in html:
        raise ValueError("generated HTML contains a remote runtime dependency")
    interactive_markers = (
        'id="actions"',
        "button.dataset.move=name",
        "Object.keys(deltas).forEach",
    )
    if "Echo Garden" not in html or any(marker not in html for marker in interactive_markers):
        raise ValueError("generated HTML is missing the interactive game surface")
    providers = {e.provider for e in manifest.model_evidence}
    if providers != {"ollama", "openrouter"}:
        raise ValueError("manifest does not prove both local and OpenRouter model execution")
    return {
        "status": "passed",
        "models_verified": len(manifest.model_evidence),
        "assets_verified": len(manifest.assets),
        "winning_turns": len(trace["turns"]),
        "final_status": trace["final_status"],
    }
