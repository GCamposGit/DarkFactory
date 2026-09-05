"""
Unified Visual Asset Studio Façade.
Orchestrates prompt synthesis, semantic text illustration, procedural and cloud rendering.
"""

import json
from pathlib import Path
from typing import List, Dict, Any, Optional

from core.visual.models import (
    AssetType,
    VisualTheme,
    AspectRatio,
    VisualPromptSpec,
    VisualAssetResult,
)
from core.visual.prompt_synthesizer import VisualPromptSynthesizer
from core.visual.cloud_engine import CloudVisualEngine


class VisualStudio:
    """Central orchestrator for all visual artifact generation in DarkFac."""

    def __init__(self, output_dir: Optional[Path] = None) -> None:
        if output_dir is None:
            self.output_dir = Path(__file__).resolve().parent.parent.parent / ".factory" / "visuals"
        else:
            self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_dir = self.output_dir / "metadata"
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self.engine = CloudVisualEngine(output_dir=self.output_dir)

    def create_asset(self, spec: VisualPromptSpec) -> VisualAssetResult:
        """Generates a visual asset from explicit prompt specifications."""
        result = self.engine.generate(spec)
        self._save_metadata(result)
        return result

    def illustrate_text(
        self,
        text_content: str,
        asset_type: AssetType = AssetType.BLOG_HERO,
        theme: Optional[VisualTheme] = None,
        aspect_ratio: Optional[AspectRatio] = None,
        offline: bool = False,
    ) -> VisualAssetResult:
        """Analyzes text semantics, synthesizes prompt, and renders matching visual asset."""
        spec = VisualPromptSynthesizer.synthesize_from_text(
            text_content=text_content,
            asset_type=asset_type,
            theme=theme,
            aspect_ratio=aspect_ratio,
        )
        spec.offline = offline
        result = self.engine.generate(spec)
        self._save_metadata(result)
        return result

    def _save_metadata(self, result: VisualAssetResult) -> None:
        """Persists metadata json in .factory/visuals/metadata/."""
        meta_file = self.metadata_dir / f"{result.asset_id}.json"
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, indent=2, ensure_ascii=False)

    def list_assets(self) -> List[Dict[str, Any]]:
        """Returns catalog of all generated visual assets."""
        assets = []
        for file in sorted(self.metadata_dir.glob("*.json"), reverse=True):
            try:
                with open(file, "r", encoding="utf-8") as f:
                    assets.append(json.load(f))
            except Exception:
                continue
        return assets

    def get_asset(self, asset_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves single visual asset by ID."""
        meta_file = self.metadata_dir / f"{asset_id}.json"
        if not meta_file.exists():
            return None
        with open(meta_file, "r", encoding="utf-8") as f:
            return json.load(f)
