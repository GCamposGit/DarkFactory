"""
Semantic Visual Prompt Synthesizer.
Extracts conceptual anchors and technical metaphors from arbitrary text
to generate high-fidelity image prompts and composition specs.
"""

import re
from typing import List, Dict, Tuple, Optional
from core.visual.models import (
    AssetType,
    VisualTheme,
    AspectRatio,
    VisualPromptSpec,
)

THEME_COLOR_PALETTES: Dict[VisualTheme, Tuple[str, str]] = {
    VisualTheme.MODERN_MINIMALIST_DARK: ("#0B0F19", "#38BDF8"),  # Slate deep navy + sky cyan
    VisualTheme.CYBERPUNK_TERMINAL: ("#05080E", "#10B981"),      # Pitch black + phosphor emerald
    VisualTheme.BLUEPRINT_TECHNICAL: ("#0F2744", "#60A5FA"),     # Deep Prussian blue + blueprint cobalt
    VisualTheme.CLEAN_VECTOR_3D: ("#18181B", "#A855F7"),         # Charcoal zinc + vibrant violet
    VisualTheme.GLASSMORPHISM: ("#090D16", "#EC4899"),           # Dark obsidian + neon rose
}

DEFAULT_NEGATIVE_PROMPT = (
    "text, typography, watermark, logo, blurry, distorted anatomy, messy artifacts, "
    "low resolution, oversaturated, amateurish, ugly, stock photography cliches, "
    "overcrowded composition, grainy noise, plastic skin, mutation"
)


class VisualPromptSynthesizer:
    """Analyzes text semantics and synthesizes production-grade generative visual specifications."""

    @classmethod
    def synthesize_from_text(
        cls,
        text_content: str,
        asset_type: AssetType = AssetType.BLOG_HERO,
        theme: Optional[VisualTheme] = None,
        aspect_ratio: Optional[AspectRatio] = None,
    ) -> VisualPromptSpec:
        """Parses content to extract key theme, title, keywords, and construct detailed prompt."""
        lines = [l.strip() for l in text_content.splitlines() if l.strip()]
        title = "Technical Architecture"
        subtitle = None

        # Extract title from markdown header or first sentence
        for line in lines[:5]:
            if line.startswith("#"):
                title = line.lstrip("#").strip()
                break
            elif len(line) > 10 and len(line) < 90:
                title = line
                break

        # Extract second line for subtitle if relevant
        if len(lines) > 1 and not lines[1].startswith("#"):
            subtitle = lines[1][:100]

        # Extract keywords
        clean_text = re.sub(r"[^\w\s]", " ", text_content.lower())
        words = clean_text.split()
        stopwords = {
            "the", "and", "for", "with", "this", "that", "from", "into", "para", "uma", "com",
            "sobre", "como", "mais", "este", "quando", "aqui", "mais", "entre", "about", "what",
            "have", "been", "here", "work", "works", "most", "teams", "core", "each", "these"
        }
        filtered_words = [w for w in words if len(w) > 4 and w not in stopwords]
        freq: Dict[str, int] = {}
        for w in filtered_words:
            freq[w] = freq.get(w, 0) + 1
        sorted_keywords = sorted(freq.items(), key=lambda x: x[1], reverse=True)
        top_tags = [k[0] for k in sorted_keywords[:6]]

        # Infer Theme if not provided
        if theme is None:
            theme = cls._infer_theme(text_content)

        # Infer Aspect Ratio based on AssetType if not specified
        if aspect_ratio is None:
            aspect_ratio = cls._default_aspect_for_type(asset_type)

        primary_color, accent_color = THEME_COLOR_PALETTES.get(theme, ("#0B0F19", "#38BDF8"))

        # Build Neural Prompt
        neural_prompt = cls._build_neural_prompt(title, top_tags, asset_type, theme)

        return VisualPromptSpec(
            title=title,
            subtitle=subtitle,
            asset_type=asset_type,
            theme=theme,
            aspect_ratio=aspect_ratio,
            primary_color=primary_color,
            accent_color=accent_color,
            tags=top_tags,
            prompt=neural_prompt,
            negative_prompt=DEFAULT_NEGATIVE_PROMPT,
        )

    @staticmethod
    def _infer_theme(text: str) -> VisualTheme:
        lower = text.lower()
        if any(k in lower for k in ["terminal", "kernel", "cli", "hacker", "low-level", "memory", "c++", "rust"]):
            return VisualTheme.CYBERPUNK_TERMINAL
        elif any(k in lower for k in ["blueprint", "spec", "deterministic", "invariants", "harness", "gate"]):
            return VisualTheme.BLUEPRINT_TECHNICAL
        elif any(k in lower for k in ["ui", "frontend", "interface", "react", "dashboard", "glass"]):
            return VisualTheme.GLASSMORPHISM
        elif any(k in lower for k in ["isometric", "cloud", "saas", "distributed", "microservice", "3d"]):
            return VisualTheme.CLEAN_VECTOR_3D
        else:
            return VisualTheme.MODERN_MINIMALIST_DARK

    @staticmethod
    def _default_aspect_for_type(asset_type: AssetType) -> AspectRatio:
        if asset_type == AssetType.SOCIAL_BANNER:
            return AspectRatio.RATIO_16_9
        elif asset_type == AssetType.BLOG_HERO:
            return AspectRatio.RATIO_16_9
        elif asset_type == AssetType.ARCHITECTURE_DIAGRAM:
            return AspectRatio.RATIO_16_9
        elif asset_type == AssetType.UI_MOCKUP:
            return AspectRatio.RATIO_16_9
        elif asset_type == AssetType.APP_ICON:
            return AspectRatio.RATIO_1_1
        else:
            return AspectRatio.RATIO_16_9

    @classmethod
    def _build_neural_prompt(
        cls,
        title: str,
        tags: List[str],
        asset_type: AssetType,
        theme: VisualTheme,
    ) -> str:
        tag_str = ", ".join(tags[:4]) if tags else "high-performance computing"

        theme_descriptions = {
            VisualTheme.MODERN_MINIMALIST_DARK: (
                "Dark obsidian and matte titanium materials, sharp cinematic cyan lighting streaks, "
                "subtle refractive volumetric atmospheric haze, minimal clean geometric forms."
            ),
            VisualTheme.CYBERPUNK_TERMINAL: (
                "Retro-futuristic terminal aesthetics, glowing phosphor emerald trace lines, CRT phosphor scanline "
                "subtlety, dark monolithic server racks, floating green telemetry matrices."
            ),
            VisualTheme.BLUEPRINT_TECHNICAL: (
                "Deep Prussian blue drafting table, precision laser-etched white and cyan technical lines, "
                "rigorous isometric architectural grids, optical measurement coordinates, camera obscura clarity."
            ),
            VisualTheme.CLEAN_VECTOR_3D: (
                "Stylized clean isometric 3D render, smooth matte silicone materials, soft ambient occlusion, "
                "delicate pastel violet and cobalt illumination, floating modular blocks."
            ),
            VisualTheme.GLASSMORPHISM: (
                "Layered translucent frosted glass slabs, vivid chromatic aberration on edges, internal glowing LEDs, "
                "deep black space background, crisp specular highlights."
            ),
        }

        type_instructions = {
            AssetType.SOCIAL_BANNER: (
                f"A widescreen digital artwork symbolizing '{title}'. Central abstract conceptual motif with dynamic energy flow. "
                "Plenty of negative space, perfect for high-impact social media headers."
            ),
            AssetType.BLOG_HERO: (
                f"An editorial hero banner artwork illustrating '{title}'. Abstract visual metaphor involving {tag_str}. "
                "Wide cinematic perspective, depth of field, premium editorial publication standard."
            ),
            AssetType.ARCHITECTURE_DIAGRAM: (
                f"A futuristic high-tech schematic diagram representing '{title}'. Modular computational nodes interconnected "
                "by glowing optical fiber conduits and deterministic state conduits."
            ),
            AssetType.UI_MOCKUP: (
                f"A sleek holographic futuristic software dashboard interface displaying '{title}'. Floating analytics cards, "
                "geometric graphs, clean minimalist typography layout, dark mode."
            ),
            AssetType.APP_ICON: (
                f"An iconic 3D app emblem representing '{title}'. Centered bold geometric insignia with bevelled edges and "
                "inner radiant glow, isolated on a deep slate gradient backdrop."
            ),
            AssetType.EDITORIAL_ILLUSTRATION: (
                f"A thought-provoking conceptual illustration about '{title}'. Symbolic visual interplay of {tag_str}, "
                "museum wall quality, elegant surrealist composition."
            ),
        }

        desc = theme_descriptions.get(theme, theme_descriptions[VisualTheme.MODERN_MINIMALIST_DARK])
        type_desc = type_instructions.get(asset_type, type_instructions[AssetType.BLOG_HERO])

        return (
            f"{type_desc} "
            f"Style & Lighting: {desc} "
            f"Rendered in 8K resolution, Octane Render style, ray-traced reflections, razor-sharp focus, masterwork."
        )
