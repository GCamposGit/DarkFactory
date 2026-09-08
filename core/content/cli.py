#!/usr/bin/env python3
"""
CLI interface for DarkFac Anti-AI-Slop Content Engine.
Allows generating persona-driven content, auditing existing texts for AI slop,
scrubbing buzzwords, and listing templates.
"""

import sys
import os
import json
import argparse
from pathlib import Path

from core.paths import project_root

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure DarkFac root in python path
_ROOT = project_root()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.content.models import ContentType, ContentRequest, ToneProfile
from core.content.anti_slop_linter import AntiSlopLinter
from core.content.presets import CONTENT_PRESETS
from core.content.engine import ContentEngine


def format_slop_report(report) -> str:
    """Formats a SlopReport into a clean ASCII-safe terminal dashboard."""
    lines = [
        "=" * 68,
        f"  ✦ ANTI-AI-SLOP AUDIT REPORT ✦",
        "=" * 68,
        f"  Slop Score         : {report.slop_score:.1f} / 100.0",
        f"  Cleanliness Rating : {report.cleanliness_rating.value}",
        f"  Violations Found   : {report.violations_count}",
        f"  Cadence Monotony   : {report.cadence_rating} (Variance: {report.sentence_length_variance:.1f} w²)",
        f"  Avg Sentence Length: {report.average_sentence_length:.1f} words",
        f"  Passive Voice Count: {report.passive_voice_count}",
        "-" * 68,
    ]
    if report.violations:
        lines.append("  [VIOLATIONS DETECTED]")
        for idx, v in enumerate(report.violations, 1):
            lines.append(f"  {idx}. [Line {v.line_number}] [{v.severity.value.upper()}] '{v.term}' ({v.category.value})")
            lines.append(f"     Context: {v.context}")
            lines.append(f"     --> Suggestion: {v.suggestion}")
            lines.append("")
    else:
        lines.append("  ✓ No AI-slop cliches or toxic buzzwords detected! Pristine voice.")
        lines.append("")
    lines.append("=" * 68)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="DarkFac Anti-AI-Slop Content Engine CLI")
    subparsers = parser.add_subparsers(dest="command")

    # Command: generate
    gen_parser = subparsers.add_parser("generate", help="Generate content with anti-slop guarantees")
    gen_parser.add_argument("--topic", required=True, help="Topic or title of the piece")
    gen_parser.add_argument(
        "--type",
        choices=[t.value for t in ContentType],
        default=ContentType.LINKEDIN_POST.value,
        help="Target format preset",
    )
    gen_parser.add_argument("--audience", default="software engineers, technical leaders, CTOs", help="Target audience")
    gen_parser.add_argument("--points", nargs="*", default=[], help="Key bullet points or arguments to include")
    gen_parser.add_argument("--offline", action="store_true", help="Force $0 offline deterministic generation")
    gen_parser.add_argument("--model", default=None, help="Cloud model override (e.g. anthropic/claude-3.7-sonnet)")

    # Command: lint
    lint_parser = subparsers.add_parser("lint", help="Audit text or file for AI slop and cliches")
    lint_parser.add_argument("--text", default=None, help="Raw text string to inspect")
    lint_parser.add_argument("--file", default=None, help="Path to text/markdown file to inspect")
    lint_parser.add_argument("--json", action="store_true", help="Output report as raw JSON")

    # Command: scrub
    scrub_parser = subparsers.add_parser("scrub", help="Automatically remove and replace slop buzzwords")
    scrub_parser.add_argument("--text", default=None, help="Raw text string to scrub")
    scrub_parser.add_argument("--file", default=None, help="Path to text file to scrub")
    scrub_parser.add_argument("--output", default=None, help="File path to write scrubbed result")

    # Command: presets
    subparsers.add_parser("presets", help="List available content presets and persona settings")

    # Command: list
    subparsers.add_parser("list", help="List previously generated content artifacts")

    args = parser.parse_args()

    if args.command == "generate":
        engine = ContentEngine()
        req = ContentRequest(
            topic=args.topic,
            content_type=ContentType(args.type),
            target_audience=args.audience,
            key_points=args.points,
            offline=args.offline,
            model_override=args.model,
        )
        resp = engine.generate(req)
        print(f"\n[+] Generated {resp.title} (ID: {resp.content_id})")
        print(f"    Provider: {resp.provider} | Model: {resp.model_used}")
        print(f"    Slop Score: {resp.final_slop_score:.1f} ({resp.slop_report.cleanliness_rating.value})\n")
        print("--- CONTENT ---")
        print(resp.final_content)
        print("---------------\n")

    elif args.command == "lint":
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
            print("Error: Specify either --text or --file to lint.", file=sys.stderr)
            sys.exit(1)

        linter = AntiSlopLinter()
        report = linter.audit(text_content)
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        else:
            print(format_slop_report(report))

    elif args.command == "scrub":
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
            print("Error: Specify either --text or --file to scrub.", file=sys.stderr)
            sys.exit(1)

        linter = AntiSlopLinter()
        scrubbed, count = linter.scrub(text_content)
        print(f"[+] Replaced {count} slop occurrences.\n")
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(scrubbed)
            print(f"[+] Saved clean text to {args.output}")
        else:
            print(scrubbed)

    elif args.command == "presets":
        print("\n✦ Available Anti-AI-Slop Presets ✦\n")
        for ctype, conf in CONTENT_PRESETS.items():
            tone = conf["default_tone"]
            print(f"• {ctype.value:<22} : {conf['title']}")
            print(f"  Description : {conf['description']}")
            print(f"  Defaults    : Formality={tone.formality}/5, Brevity={tone.brevity}/5, TechDepth={tone.technical_depth}/5")
            print()

    elif args.command == "list":
        engine = ContentEngine()
        records = engine.list_records()
        print(f"\n✦ Stored Content Records ({len(records)}) ✦\n")
        for r in records:
            print(f"[{r['content_id']}] {r['title']} | Score: {r['final_slop_score']} ({r['cleanliness_rating']}) | Words: {r['word_count']}")
        print()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
