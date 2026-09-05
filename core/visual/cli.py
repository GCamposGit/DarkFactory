#!/usr/bin/env python3
"""
CLI interface for DarkFac Context-Aware Visual Asset & Image Studio.
Enables creating social banners, blog heroes, architecture diagrams, UI mockups,
and app icons with $0 local procedural rendering or cloud frontier generation.
"""

import sys
import os
import json
import argparse
from pathlib import Path

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure DarkFac root in python path
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.visual.models import (
    AssetType,
    VisualTheme,
    AspectRatio,
    VisualPromptSpec,
)
from core.visual.studio import VisualStudio


def main():
    parser = argparse.ArgumentParser(description="DarkFac Visual Asset & Image Studio CLI")
    subparsers = parser.add_subparsers(dest="command")

    # Command: create
    create_parser = subparsers.add_parser("create", help="Create visual asset from specifications")
    create_parser.add_argument("--title", required=True, help="Title / text to render on asset")
    create_parser.add_argument("--subtitle", default=None, help="Secondary subtitle or descriptor")
    create_parser.add_argument(
        "--type",
        choices=[t.value for t in AssetType],
        default=AssetType.SOCIAL_BANNER.value,
        help="Visual asset format",
    )
    create_parser.add_argument(
        "--theme",
        choices=[t.value for t in VisualTheme],
        default=VisualTheme.MODERN_MINIMALIST_DARK.value,
        help="Aesthetic theme",
    )
    create_parser.add_argument(
        "--ratio",
        choices=[r.value for r in AspectRatio],
        default=AspectRatio.RATIO_16_9.value,
        help="Aspect ratio",
    )
    create_parser.add_argument("--high-res", action="store_true", help="Render at 1080p/4K production dimensions")
    create_parser.add_argument("--offline", action="store_true", help="Force $0 local procedural rendering")

    # Command: illustrate
    ill_parser = subparsers.add_parser("illustrate", help="Illustrate text or article with context-aware artwork")
    ill_parser.add_argument("--text", default=None, help="Direct text content to illustrate")
    ill_parser.add_argument("--file", default=None, help="Path to markdown/text file to illustrate")
    ill_parser.add_argument(
        "--type",
        choices=[t.value for t in AssetType],
        default=AssetType.BLOG_HERO.value,
        help="Target visual asset type",
    )
    ill_parser.add_argument(
        "--theme",
        choices=[t.value for t in VisualTheme],
        default=None,
        help="Override inferred theme",
    )
    ill_parser.add_argument("--offline", action="store_true", help="Force $0 local procedural rendering")

    # Command: diagram
    diag_parser = subparsers.add_parser("diagram", help="Quick shortcut to generate an architecture diagram")
    diag_parser.add_argument("--title", required=True, help="Title of the architecture/pipeline")
    diag_parser.add_argument(
        "--theme",
        choices=[t.value for t in VisualTheme],
        default=VisualTheme.BLUEPRINT_TECHNICAL.value,
        help="Visual theme",
    )

    # Command: mockup
    mock_parser = subparsers.add_parser("mockup", help="Quick shortcut to generate a software UI mockup")
    mock_parser.add_argument("--title", required=True, help="Name of the software application/feature")
    mock_parser.add_argument(
        "--theme",
        choices=[t.value for t in VisualTheme],
        default=VisualTheme.GLASSMORPHISM.value,
        help="Visual theme",
    )

    # Command: icon
    icon_parser = subparsers.add_parser("icon", help="Quick shortcut to generate an app icon / badge")
    icon_parser.add_argument("--title", required=True, help="Name or initials for the app icon")
    icon_parser.add_argument(
        "--theme",
        choices=[t.value for t in VisualTheme],
        default=VisualTheme.CLEAN_VECTOR_3D.value,
        help="Visual theme",
    )

    # Command: list
    subparsers.add_parser("list", help="List generated visual assets")

    args = parser.parse_args()
    studio = VisualStudio()

    if args.command == "create":
        spec = VisualPromptSpec(
            title=args.title,
            subtitle=args.subtitle,
            asset_type=AssetType(args.type),
            theme=VisualTheme(args.theme),
            aspect_ratio=AspectRatio(args.ratio),
            high_res=args.high_res,
            offline=args.offline,
        )
        res = studio.create_asset(spec)
        print(f"\n[+] Visual asset generated successfully!")
        print(f"    Asset ID   : {res.asset_id}")
        print(f"    Type       : {res.asset_type.value} ({res.aspect_ratio.value})")
        print(f"    Dimensions : {res.width}x{res.height} px ({res.file_format.upper()})")
        print(f"    Provider   : {res.provider} ({res.generation_time_ms} ms)")
        print(f"    File Path  : {res.file_path}\n")

    elif args.command == "illustrate":
        text_content = ""
        if args.file:
            path = Path(args.file)
            if not path.exists():
                print(f"Error: File not found: {args.file}", file=sys.stderr)
                sys.exit(1)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text_content = f.read()
        elif args.text:
            text_content = args.text
        else:
            print("Error: Provide --text or --file to illustrate.", file=sys.stderr)
            sys.exit(1)

        theme_enum = VisualTheme(args.theme) if args.theme else None
        res = studio.illustrate_text(
            text_content=text_content,
            asset_type=AssetType(args.type),
            theme=theme_enum,
            offline=args.offline,
        )
        print(f"\n[+] Text illustrated successfully!")
        print(f"    Asset ID   : {res.asset_id}")
        print(f"    Title      : {res.title}")
        print(f"    Theme      : {res.theme.value}")
        print(f"    File Path  : {res.file_path}\n")

    elif args.command == "diagram":
        spec = VisualPromptSpec(
            title=args.title,
            asset_type=AssetType.ARCHITECTURE_DIAGRAM,
            theme=VisualTheme(args.theme),
            aspect_ratio=AspectRatio.RATIO_16_9,
            offline=True,
        )
        res = studio.create_asset(spec)
        print(f"\n[+] Architecture diagram rendered: {res.file_path}\n")

    elif args.command == "mockup":
        spec = VisualPromptSpec(
            title=args.title,
            asset_type=AssetType.UI_MOCKUP,
            theme=VisualTheme(args.theme),
            aspect_ratio=AspectRatio.RATIO_16_9,
            offline=True,
        )
        res = studio.create_asset(spec)
        print(f"\n[+] UI mockup rendered: {res.file_path}\n")

    elif args.command == "icon":
        spec = VisualPromptSpec(
            title=args.title,
            asset_type=AssetType.APP_ICON,
            theme=VisualTheme(args.theme),
            aspect_ratio=AspectRatio.RATIO_1_1,
            offline=True,
        )
        res = studio.create_asset(spec)
        print(f"\n[+] App icon rendered: {res.file_path}\n")

    elif args.command == "list":
        assets = studio.list_assets()
        print(f"\n✦ Stored Visual Assets ({len(assets)}) ✦\n")
        for a in assets:
            print(f"[{a['asset_id']}] {a['title']} | {a['asset_type']} ({a['width']}x{a['height']}) | {a['provider']}")
        print()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
