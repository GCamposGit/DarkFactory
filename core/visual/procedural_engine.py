"""
High-Aesthetic Deterministic Procedural Visual Asset Engine.
100% Offline, $0 Cost, Zero External API Dependency.
Renders production-ready PNG assets using Pillow:
- Banners & Editorial Heroes with glowing grids & data highways
- Architecture Diagrams with interconnected nodes & flow vectors
- UI Mockups with modern dark mode dashboard elements
- App Icons with glossy squircles and geometric monograms
"""

import math
import time
from pathlib import Path
from typing import Tuple, List, Optional
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from core.visual.models import (
    AssetType,
    VisualTheme,
    AspectRatio,
    VisualPromptSpec,
    VisualAssetResult,
    ASPECT_DIMENSIONS,
    PREVIEW_DIMENSIONS,
)


def hex_to_rgb(hex_code: str) -> Tuple[int, int, int]:
    """Converts hex color string to RGB tuple."""
    hex_code = hex_code.lstrip("#")
    if len(hex_code) == 3:
        hex_code = "".join([c * 2 for c in hex_code])
    return int(hex_code[0:2], 16), int(hex_code[2:4], 16), int(hex_code[4:6], 16)


def get_default_font(size: int) -> ImageFont.ImageFont:
    """Attempts to load a clean system font on Windows/Linux or falls back to default."""
    font_candidates = [
        "arial.ttf",
        "segoeui.ttf",
        "consola.ttf",
        "DejaVuSans.ttf",
        "Helvetica.ttf",
    ]
    for candidate in font_candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return ImageFont.load_default()


class ProceduralVisualEngine:
    """Renders high-aesthetic technical visuals deterministically on local CPU."""

    def __init__(self, output_dir: Optional[Path] = None) -> None:
        if output_dir is None:
            self.output_dir = Path(__file__).resolve().parent.parent.parent / ".factory" / "visuals"
        else:
            self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def render(self, spec: VisualPromptSpec) -> VisualAssetResult:
        """Renders asset according to specification and saves to disk."""
        start_time = time.time()

        dimensions = ASPECT_DIMENSIONS.get(spec.aspect_ratio, (1920, 1080))
        if not spec.high_res:
            # Use preview dimensions for ultra-fast local rendering
            dimensions = PREVIEW_DIMENSIONS.get(spec.aspect_ratio, (800, 450))

        width, height = dimensions

        # Dispatch renderer by asset type
        if spec.asset_type == AssetType.ARCHITECTURE_DIAGRAM:
            img = self._render_diagram(width, height, spec)
        elif spec.asset_type == AssetType.UI_MOCKUP:
            img = self._render_ui_mockup(width, height, spec)
        elif spec.asset_type == AssetType.APP_ICON:
            img = self._render_app_icon(width, height, spec)
        else:
            img = self._render_hero_banner(width, height, spec)

        # Generate asset ID and persist
        asset_id = f"vis_{int(start_time * 1000) % 1000000:06d}_{spec.asset_type.value[:4]}"
        filename = f"{asset_id}.png"
        file_path = self.output_dir / filename
        img.save(file_path, format="PNG", optimize=True)

        elapsed_ms = int((time.time() - start_time) * 1000)

        return VisualAssetResult(
            asset_id=asset_id,
            title=spec.title,
            asset_type=spec.asset_type,
            theme=spec.theme,
            aspect_ratio=spec.aspect_ratio,
            width=width,
            height=height,
            file_path=str(file_path),
            file_format="png",
            provider="local_procedural",
            model_used="darkfac-vector-v1",
            prompt_used=spec.prompt or spec.title,
            generation_time_ms=elapsed_ms,
            cost_usd=0.0,
        )

    # -------------------------------------------------------------
    # 1. Hero Banner & Social Banner Renderer
    # -------------------------------------------------------------
    def _render_hero_banner(self, width: int, height: int, spec: VisualPromptSpec) -> Image.Image:
        base_color = hex_to_rgb(spec.primary_color)
        accent_color = hex_to_rgb(spec.accent_color)

        # Create vertical dark gradient
        img = Image.new("RGBA", (width, height), (*base_color, 255))
        draw = ImageDraw.Draw(img)

        # Draw subtle gradient shading
        for y in range(height):
            ratio = y / height
            shade = tuple(int(c * (1.0 - 0.45 * ratio)) for c in base_color)
            draw.line([(0, y), (width, y)], fill=(*shade, 255))

        # Draw perspective grid lines
        grid_overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        grid_draw = ImageDraw.Draw(grid_overlay)
        grid_color = (*accent_color, 35)

        # Perspective ground grid
        horizon_y = int(height * 0.45)
        for x in range(0, width + 60, 50):
            grid_draw.line([(x, height), (int(width * 0.5 + (x - width * 0.5) * 0.15), horizon_y)], fill=grid_color, width=1)
        for y in range(horizon_y, height, 25):
            factor = (y - horizon_y) / (height - horizon_y)
            alpha = int(10 + 40 * factor)
            grid_draw.line([(0, y), (width, y)], fill=(*accent_color, alpha), width=1)

        # Radial accent glow in the center-right
        glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        glow_cx, glow_cy = int(width * 0.72), int(height * 0.48)
        glow_radius = int(height * 0.45)
        glow_draw.ellipse(
            [(glow_cx - glow_radius, glow_cy - glow_radius), (glow_cx + glow_radius, glow_cy + glow_radius)],
            fill=(*accent_color, 50),
        )
        glow = glow.filter(ImageFilter.GaussianBlur(glow_radius // 2))

        # Merge background layers
        img = Image.alpha_composite(img, glow)
        img = Image.alpha_composite(img, grid_overlay)
        draw = ImageDraw.Draw(img)

        # Draw Abstract Technical Nodes in background
        node_coords = [
            (int(width * 0.65), int(height * 0.35)),
            (int(width * 0.82), int(height * 0.28)),
            (int(width * 0.75), int(height * 0.60)),
            (int(width * 0.88), int(height * 0.55)),
        ]
        for i, (nx, ny) in enumerate(node_coords):
            for j, (ox, oy) in enumerate(node_coords):
                if i < j:
                    draw.line([(nx, ny), (ox, oy)], fill=(*accent_color, 90), width=2)
            draw.ellipse([(nx - 7, ny - 7), (nx + 7, ny + 7)], fill=(*accent_color, 240), outline=(255, 255, 255, 255), width=2)

        # Draw Typographic Card on Left
        card_w, card_h = int(width * 0.52), int(height * 0.68)
        card_x, card_y = int(width * 0.08), int(height * 0.16)

        # Semi-translucent frosted card
        card_overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        card_draw = ImageDraw.Draw(card_overlay)
        card_draw.rounded_rectangle(
            [(card_x, card_y), (card_x + card_w, card_y + card_h)],
            radius=16,
            fill=(15, 23, 42, 210),
            outline=(*accent_color, 140),
            width=2,
        )
        img = Image.alpha_composite(img, card_overlay)
        draw = ImageDraw.Draw(img)

        # Text inside card
        font_tag = get_default_font(max(12, int(height * 0.032)))
        font_title = get_default_font(max(18, int(height * 0.065)))
        font_sub = get_default_font(max(13, int(height * 0.035)))

        # Category Badge
        badge_text = f"✦ DARKFAC FACTORY • {spec.theme.value.replace('_', ' ').upper()} ✦"
        draw.text((card_x + 28, card_y + 30), badge_text, font=font_tag, fill=(*accent_color, 255))

        # Title (wrapped if needed)
        title_lines = self._wrap_text(spec.title, max_chars=28)
        curr_y = card_y + 70
        for tline in title_lines[:3]:
            draw.text((card_x + 28, curr_y), tline, font=font_title, fill=(255, 255, 255, 255))
            curr_y += int(height * 0.08)

        # Subtitle or Tags
        if spec.subtitle:
            sub_lines = self._wrap_text(spec.subtitle, max_chars=36)
            curr_y += 10
            for sline in sub_lines[:2]:
                draw.text((card_x + 28, curr_y), sline, font=font_sub, fill=(148, 163, 184, 255))
                curr_y += int(height * 0.045)
        elif spec.tags:
            tag_line = "  •  ".join([f"#{t}" for t in spec.tags[:4]])
            draw.text((card_x + 28, curr_y + 15), tag_line, font=font_sub, fill=(*accent_color, 200))

        return img.convert("RGB")

    # -------------------------------------------------------------
    # 2. Architecture Diagram Renderer
    # -------------------------------------------------------------
    def _render_diagram(self, width: int, height: int, spec: VisualPromptSpec) -> Image.Image:
        base_color = hex_to_rgb(spec.primary_color)
        accent_color = hex_to_rgb(spec.accent_color)

        img = Image.new("RGB", (width, height), base_color)
        draw = ImageDraw.Draw(img)

        # Header Title
        font_title = get_default_font(max(18, int(height * 0.055)))
        font_node = get_default_font(max(12, int(height * 0.035)))
        font_sub = get_default_font(max(11, int(height * 0.026)))

        draw.text((40, 30), f"SYSTEM ARCHITECTURE: {spec.title.upper()}", font=font_title, fill=(255, 255, 255))
        draw.line([(40, 75), (width - 40, 75)], fill=(*accent_color, 120), width=2)

        # 4 Core Nodes Pipeline
        nodes = [
            {"title": "Specs / Requirements", "sub": "Zero-Slop PRD", "status": "READY"},
            {"title": "Model Router", "sub": "Pareto Optimal Tier", "status": "ROUTED"},
            {"title": "Autonomous PIV Loop", "sub": "Fresh Subagent Task", "status": "ACTIVE"},
            {"title": "Deterministic Gate", "sub": "Harness Pass@1", "status": "VERIFIED"},
        ]

        node_w = int((width - 160) / 4)
        node_h = int(height * 0.45)
        top_y = int(height * 0.28)

        for i, node in enumerate(nodes):
            x = 40 + i * (node_w + 25)
            # Node Box
            draw.rounded_rectangle(
                [(x, top_y), (x + node_w, top_y + node_h)],
                radius=12,
                fill=(15, 23, 42),
                outline=accent_color,
                width=2,
            )
            # Node Header Bar
            draw.rounded_rectangle(
                [(x, top_y), (x + node_w, top_y + 36)],
                radius=12,
                fill=(30, 41, 59),
            )
            draw.text((x + 14, top_y + 10), f"STAGE 0{i+1}", font=font_sub, fill=accent_color)
            draw.text((x + 14, top_y + 60), node["title"], font=font_node, fill=(255, 255, 255))
            draw.text((x + 14, top_y + 95), node["sub"], font=font_sub, fill=(148, 163, 184))

            # Status pill
            pill_y = top_y + node_h - 40
            draw.rounded_rectangle([(x + 14, pill_y), (x + node_w - 14, pill_y + 24)], radius=6, fill=(6, 78, 59))
            draw.text((x + 24, pill_y + 4), f"● {node['status']}", font=font_sub, fill=(52, 211, 153))

            # Connection arrow to next node
            if i < len(nodes) - 1:
                arrow_start = (x + node_w, top_y + int(node_h * 0.5))
                arrow_end = (x + node_w + 25, top_y + int(node_h * 0.5))
                draw.line([arrow_start, arrow_end], fill=accent_color, width=3)

        # Footer Metrics
        draw.text((40, height - 45), "DarkFac Autonomous Engine • Strict Headless Contracts • Deterministic Auto-Merge", font=font_sub, fill=(100, 116, 139))

        return img

    # -------------------------------------------------------------
    # 3. UI Mockup Renderer
    # -------------------------------------------------------------
    def _render_ui_mockup(self, width: int, height: int, spec: VisualPromptSpec) -> Image.Image:
        base_color = hex_to_rgb(spec.primary_color)
        accent_color = hex_to_rgb(spec.accent_color)

        img = Image.new("RGB", (width, height), (8, 12, 20))
        draw = ImageDraw.Draw(img)

        # Outer Window Frame
        margin_x = int(width * 0.05)
        margin_y = int(height * 0.06)
        win_w = width - (2 * margin_x)
        win_h = height - (2 * margin_y)

        draw.rounded_rectangle(
            [(margin_x, margin_y), (margin_x + win_w, margin_y + win_h)],
            radius=14,
            fill=(15, 23, 42),
            outline=(51, 65, 85),
            width=2,
        )

        # Window Header Bar (Traffic lights)
        header_h = 38
        draw.rectangle([(margin_x, margin_y), (margin_x + win_w, margin_y + header_h)], fill=(30, 41, 59))
        draw.ellipse([(margin_x + 16, margin_y + 13), (margin_x + 28, margin_y + 25)], fill=(239, 68, 68))
        draw.ellipse([(margin_x + 36, margin_y + 13), (margin_x + 48, margin_y + 25)], fill=(245, 158, 11))
        draw.ellipse([(margin_x + 56, margin_y + 13), (margin_x + 68, margin_y + 25)], fill=(16, 185, 129))

        font_sm = get_default_font(max(10, int(height * 0.024)))
        draw.text((margin_x + 85, margin_y + 11), f"https://darkfac.local/app/{spec.title.lower().replace(' ', '-')}", font=font_sm, fill=(148, 163, 184))

        # Sidebar
        side_w = int(win_w * 0.22)
        content_y = margin_y + header_h
        draw.rectangle([(margin_x, content_y), (margin_x + side_w, margin_y + win_h)], fill=(20, 29, 47))

        nav_items = ["Dashboard", "Pipelines", "Content Studio", "Visual Engine", "Settings"]
        curr_nav_y = content_y + 25
        for item in nav_items:
            is_active = (item in ["Dashboard", "Content Studio"])
            bg_col = (30, 58, 138) if is_active else (20, 29, 47)
            text_col = accent_color if is_active else (148, 163, 184)
            draw.rounded_rectangle([(margin_x + 12, curr_nav_y), (margin_x + side_w - 12, curr_nav_y + 28)], radius=6, fill=bg_col)
            draw.text((margin_x + 24, curr_nav_y + 6), f"● {item}", font=font_sm, fill=text_col)
            curr_nav_y += 38

        # Main Workspace Cards
        main_x = margin_x + side_w + 20
        main_w = win_w - side_w - 40

        # Card 1: Metric Overview
        card1_w = int((main_w - 20) * 0.5)
        card_h = int((win_h - header_h - 60) * 0.45)
        draw.rounded_rectangle([(main_x, content_y + 20), (main_x + card1_w, content_y + 20 + card_h)], radius=10, fill=(30, 41, 59), outline=(51, 65, 85), width=1)
        draw.text((main_x + 18, content_y + 35), "Real-time Throughput", font=font_sm, fill=(148, 163, 184))
        font_big = get_default_font(max(16, int(height * 0.05)))
        draw.text((main_x + 18, content_y + 60), "142.8 tps", font=font_big, fill=(255, 255, 255))
        # Draw Mini Chart line
        chart_pts = [(main_x + 20, content_y + 20 + card_h - 20),
                     (main_x + 60, content_y + 20 + card_h - 45),
                     (main_x + 100, content_y + 20 + card_h - 30),
                     (main_x + 140, content_y + 20 + card_h - 65),
                     (main_x + card1_w - 20, content_y + 20 + card_h - 55)]
        for ci in range(len(chart_pts) - 1):
            draw.line([chart_pts[ci], chart_pts[ci+1]], fill=accent_color, width=3)

        # Card 2: Quality Pass Rate
        card2_x = main_x + card1_w + 20
        draw.rounded_rectangle([(card2_x, content_y + 20), (card2_x + card1_w, content_y + 20 + card_h)], radius=10, fill=(30, 41, 59), outline=(51, 65, 85), width=1)
        draw.text((card2_x + 18, content_y + 35), "Deterministic Pass@1", font=font_sm, fill=(148, 163, 184))
        draw.text((card2_x + 18, content_y + 60), "100.0%", font=font_big, fill=(52, 211, 153))

        # Lower Card: Data Table
        table_y = content_y + 40 + card_h
        table_h = win_h - header_h - 60 - card_h
        draw.rounded_rectangle([(main_x, table_y), (main_x + main_w, table_y + table_h)], radius=10, fill=(24, 33, 49), outline=(51, 65, 85), width=1)
        draw.text((main_x + 20, table_y + 15), f"Active Software Tasks for: {spec.title}", font=font_sm, fill=(255, 255, 255))

        return img

    # -------------------------------------------------------------
    # 4. App Icon / Logo Renderer
    # -------------------------------------------------------------
    def _render_app_icon(self, width: int, height: int, spec: VisualPromptSpec) -> Image.Image:
        base_color = hex_to_rgb(spec.primary_color)
        accent_color = hex_to_rgb(spec.accent_color)

        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        pad = int(width * 0.1)
        icon_box = [(pad, pad), (width - pad, height - pad)]
        corner_radius = int(width * 0.22)

        # Squircle Background with gradient
        draw.rounded_rectangle(icon_box, radius=corner_radius, fill=(*base_color, 255), outline=(*accent_color, 200), width=max(2, int(width * 0.015)))

        # Inner Glossy Radial Ring
        cx, cy = width // 2, height // 2
        r_inner = int(width * 0.28)
        draw.ellipse([(cx - r_inner, cy - r_inner), (cx + r_inner, cy + r_inner)], fill=(*accent_color, 40), outline=(*accent_color, 180), width=3)

        # Geometric Monogram Emblem (Hexagon + Central Core)
        hex_pts = []
        for a in range(6):
            angle = math.radians(a * 60 - 30)
            px = cx + int(r_inner * 0.7 * math.cos(angle))
            py = cy + int(r_inner * 0.7 * math.sin(angle))
            hex_pts.append((px, py))
        draw.polygon(hex_pts, outline=(255, 255, 255, 255), fill=(*accent_color, 120), width=4)

        # Center core dot
        draw.ellipse([(cx - 12, cy - 12), (cx + 12, cy + 12)], fill=(255, 255, 255, 255))

        return img.convert("RGB")

    @staticmethod
    def _wrap_text(text: str, max_chars: int = 30) -> List[str]:
        words = text.split()
        lines: List[str] = []
        curr = ""
        for w in words:
            if len(curr) + len(w) + 1 <= max_chars:
                curr = f"{curr} {w}".strip()
            else:
                if curr:
                    lines.append(curr)
                curr = w
        if curr:
            lines.append(curr)
        return lines
