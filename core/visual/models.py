"""
Domain models for DarkFac Context-Aware Visual Asset & Image Studio.
Strictly typed for compatibility with Universal Engineering Standards (AGENTS.md).
Defines asset types, themes, aspect ratios, prompt specs, and generation results.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timezone
import uuid


class AssetType(str, Enum):
    SOCIAL_BANNER = "social_banner"
    BLOG_HERO = "blog_hero"
    ARCHITECTURE_DIAGRAM = "architecture_diagram"
    UI_MOCKUP = "ui_mockup"
    APP_ICON = "app_icon"
    EDITORIAL_ILLUSTRATION = "editorial_illustration"


class VisualTheme(str, Enum):
    MODERN_MINIMALIST_DARK = "modern_minimalist_dark"
    CYBERPUNK_TERMINAL = "cyberpunk_terminal"
    BLUEPRINT_TECHNICAL = "blueprint_technical"
    CLEAN_VECTOR_3D = "clean_vector_3d"
    GLASSMORPHISM = "glassmorphism"


class AspectRatio(str, Enum):
    RATIO_1_1 = "1:1"
    RATIO_16_9 = "16:9"
    RATIO_4_5 = "4:5"
    RATIO_9_16 = "9:16"
    RATIO_21_9 = "21:9"


# Canonical aspect ratio pixel mappings (width, height)
ASPECT_DIMENSIONS: Dict[AspectRatio, Tuple[int, int]] = {
    AspectRatio.RATIO_1_1: (1080, 1080),
    AspectRatio.RATIO_16_9: (1920, 1080),
    AspectRatio.RATIO_4_5: (1080, 1350),
    AspectRatio.RATIO_9_16: (1080, 1920),
    AspectRatio.RATIO_21_9: (2560, 1080),
}

# Compact preview dimensions for fast generation / testing
PREVIEW_DIMENSIONS: Dict[AspectRatio, Tuple[int, int]] = {
    AspectRatio.RATIO_1_1: (512, 512),
    AspectRatio.RATIO_16_9: (800, 450),
    AspectRatio.RATIO_4_5: (480, 600),
    AspectRatio.RATIO_9_16: (450, 800),
    AspectRatio.RATIO_21_9: (960, 410),
}


@dataclass
class VisualPromptSpec:
    """Specification for generating or rendering a visual asset."""
    title: str
    subtitle: Optional[str] = None
    asset_type: AssetType = AssetType.SOCIAL_BANNER
    theme: VisualTheme = VisualTheme.MODERN_MINIMALIST_DARK
    aspect_ratio: AspectRatio = AspectRatio.RATIO_16_9
    primary_color: str = "#0F172A"
    accent_color: str = "#06B6D4"
    tags: List[str] = field(default_factory=list)
    prompt: Optional[str] = None
    negative_prompt: Optional[str] = None
    offline: bool = False
    model_override: Optional[str] = None
    high_res: bool = False
    strict_provider: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "asset_type": self.asset_type.value,
            "theme": self.theme.value,
            "aspect_ratio": self.aspect_ratio.value,
            "primary_color": self.primary_color,
            "accent_color": self.accent_color,
            "tags": self.tags,
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "offline": self.offline,
            "model_override": self.model_override,
            "high_res": self.high_res,
            "strict_provider": self.strict_provider,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VisualPromptSpec":
        return cls(
            title=data.get("title", ""),
            subtitle=data.get("subtitle"),
            asset_type=AssetType(data.get("asset_type", AssetType.SOCIAL_BANNER.value)),
            theme=VisualTheme(data.get("theme", VisualTheme.MODERN_MINIMALIST_DARK.value)),
            aspect_ratio=AspectRatio(data.get("aspect_ratio", AspectRatio.RATIO_16_9.value)),
            primary_color=data.get("primary_color", "#0F172A"),
            accent_color=data.get("accent_color", "#06B6D4"),
            tags=data.get("tags", []),
            prompt=data.get("prompt"),
            negative_prompt=data.get("negative_prompt"),
            offline=data.get("offline", False),
            model_override=data.get("model_override"),
            high_res=data.get("high_res", False),
            strict_provider=data.get("strict_provider", False),
        )


@dataclass
class VisualAssetResult:
    """Artifact produced by Visual Studio with file reference and metadata."""
    asset_id: str
    title: str
    asset_type: AssetType
    theme: VisualTheme
    aspect_ratio: AspectRatio
    width: int
    height: int
    file_path: str
    file_format: str
    provider: str
    model_used: str
    prompt_used: str
    generation_time_ms: int
    cost_usd: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    fallback_occurred: bool = False
    fallback_reason: Optional[str] = None
    original_provider_requested: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "title": self.title,
            "asset_type": self.asset_type.value,
            "theme": self.theme.value,
            "aspect_ratio": self.aspect_ratio.value,
            "width": self.width,
            "height": self.height,
            "file_path": self.file_path,
            "file_format": self.file_format,
            "provider": self.provider,
            "model_used": self.model_used,
            "prompt_used": self.prompt_used,
            "generation_time_ms": self.generation_time_ms,
            "cost_usd": self.cost_usd,
            "created_at": self.created_at,
            "fallback_occurred": self.fallback_occurred,
            "fallback_reason": self.fallback_reason,
            "original_provider_requested": self.original_provider_requested,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VisualAssetResult":
        return cls(
            asset_id=data.get("asset_id", str(uuid.uuid4())[:8]),
            title=data.get("title", ""),
            asset_type=AssetType(data.get("asset_type", AssetType.SOCIAL_BANNER.value)),
            theme=VisualTheme(data.get("theme", VisualTheme.MODERN_MINIMALIST_DARK.value)),
            aspect_ratio=AspectRatio(data.get("aspect_ratio", AspectRatio.RATIO_16_9.value)),
            width=data.get("width", 800),
            height=data.get("height", 450),
            file_path=data.get("file_path", ""),
            file_format=data.get("file_format", "png"),
            provider=data.get("provider", "local_procedural"),
            model_used=data.get("model_used", "darkfac-vector-v1"),
            prompt_used=data.get("prompt_used", ""),
            generation_time_ms=data.get("generation_time_ms", 0),
            cost_usd=data.get("cost_usd", 0.0),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            fallback_occurred=data.get("fallback_occurred", False),
            fallback_reason=data.get("fallback_reason"),
            original_provider_requested=data.get("original_provider_requested"),
        )
