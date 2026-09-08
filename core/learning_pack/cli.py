"""
CLI Interface for Session Learning Pack & Cognitive Uplift Engine.
Commands:
  generate     - Scan git diff/files and synthesize a session learning pack
  list         - List all stored learning packs
  show         - View a learning pack in terminal
  flashcards   - Study flashcards (includes interactive terminal quiz mode)
  export-anki  - Export flashcards as Anki TSV
  html         - Open standalone interactive HTML widget in browser
"""

import sys
import argparse
import webbrowser
from pathlib import Path
from typing import Optional

from core.paths import project_root

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure project root in sys.path
_ROOT = project_root()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.learning_pack.models import SessionLearningPack
from core.learning_pack.analyzer import CodebaseAnalyzer
from core.learning_pack.generator import LearningPackGenerator
from core.learning_pack.renderer import LearningPackRenderer
from core.learning_pack.storage import LearningPackStore


def handle_generate(args):
    analyzer = CodebaseAnalyzer()
    generator = LearningPackGenerator(analyzer)
    store = LearningPackStore()

    target_files = args.files if args.files else None
    pack = generator.generate_pack(
        title=args.title,
        session_id=args.session_id,
        files_analyzed=target_files,
    )

    if not args.dry_run:
        saved_paths = store.save_pack(pack)
        print(f"[STORED] Learning Pack saved successfully:")
        print(f"  - Markdown: {saved_paths['md']}")
        print(f"  - HTML:     {saved_paths['html']}")
        print(f"  - Anki TSV: {saved_paths['anki']}")
        print(f"  - JSON:     {saved_paths['json']}")
        print("")

    rendered = LearningPackRenderer.render_markdown(pack, brief_mode=args.brief)
    print(rendered)


def handle_list(args):
    store = LearningPackStore()
    packs = store.list_packs()
    if not packs:
        print("[INFO] No learning packs recorded yet. Run `python -m core.learning_pack.cli generate`.")
        return

    print(f"\n{'PACK ID':<30} {'TIMESTAMP':<20} {'CONCEPTS':<10} {'CARDS':<8} {'TITLE'}")
    print("-" * 105)
    for p in packs:
        ts = p.get("timestamp", "")[:19].replace("T", " ")
        c_count = p.get("concepts_count", 0)
        f_count = p.get("flashcards_count", 0)
        title = p.get("title", "")
        print(f"{p['pack_id']:<30} {ts:<20} {c_count:<10} {f_count:<8} {title}")
    print("")


def handle_show(args):
    store = LearningPackStore()
    pack = store.load_pack(args.pack_id)
    if not pack:
        print(f"[ERROR] Learning pack '{args.pack_id}' not found.")
        sys.exit(1)

    rendered = LearningPackRenderer.render_markdown(pack, brief_mode=args.brief)
    print(rendered)


def handle_flashcards(args):
    store = LearningPackStore()
    pack = store.load_pack(args.pack_id)
    if not pack:
        print(f"[ERROR] Learning pack '{args.pack_id}' not found.")
        sys.exit(1)

    if not pack.flashcards:
        print(f"[INFO] No flashcards found in pack '{pack.pack_id}'.")
        return

    if not args.interactive:
        print(f"\n=== Flashcards for {pack.title} ({len(pack.flashcards)} cards) ===\n")
        for idx, card in enumerate(pack.flashcards, 1):
            print(f"Card {idx}: {card.front_prompt}")
            print(f"Answer: {card.back_solution}")
            print(f"Why it matters: {card.why_it_matters}")
            print("-" * 60)
    else:
        print(f"\n🎯 [INTERACTIVE TERMINAL QUIZ] {pack.title}")
        print("Read the prompt, formulate your answer mentally or out loud, then press [ENTER].\n")
        for idx, card in enumerate(pack.flashcards, 1):
            input(f"Card {idx}/{len(pack.flashcards)}: {card.front_prompt}\n>>> [Press ENTER to reveal answer]")
            print(f"\n💡 EXPLANATION:\n{card.back_solution}")
            print(f"\n🧠 SCHEMA ANCHOR:\n{card.why_it_matters}\n")
            print("=" * 65)
        print("🏆 Quiz complete! Germane schema reinforced.")


def handle_export_anki(args):
    store = LearningPackStore()
    pack = store.load_pack(args.pack_id)
    if not pack:
        print(f"[ERROR] Learning pack '{args.pack_id}' not found.")
        sys.exit(1)

    tsv = LearningPackRenderer.render_anki_tsv(pack)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(tsv, encoding="utf-8")
        print(f"[EXPORT] Wrote {len(pack.flashcards)} cards to {out_path}")
    else:
        print(tsv)


def handle_html(args):
    store = LearningPackStore()
    pack = store.load_pack(args.pack_id)
    if not pack:
        print(f"[ERROR] Learning pack '{args.pack_id}' not found.")
        sys.exit(1)

    html_file = store.storage_dir / f"{pack.pack_id}.html"
    if not html_file.exists():
        # Re-render and save
        content = LearningPackRenderer.render_html(pack)
        html_file.write_text(content, encoding="utf-8")

    print(f"[HTML WIDGET] file:///{html_file.resolve().as_posix()}")
    if args.open:
        webbrowser.open(f"file:///{html_file.resolve().as_posix()}")


def main():
    parser = argparse.ArgumentParser(
        prog="python -m core.learning_pack.cli",
        description="Session Learning Pack & Cognitive Uplift Engine",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # generate
    gen_parser = subparsers.add_parser("generate", help="Synthesize learning pack from session delta")
    gen_parser.add_argument("--title", type=str, help="Custom title for learning pack")
    gen_parser.add_argument("--session-id", type=str, help="Session ID reference")
    gen_parser.add_argument("--files", nargs="*", help="Specific files to analyze")
    gen_parser.add_argument("--dry-run", action="store_true", help="Print without saving to store")
    gen_parser.add_argument("--brief", action="store_true", help="Omit mechanics/trade-offs in stdout")

    # list
    subparsers.add_parser("list", help="List stored learning packs")

    # show
    show_parser = subparsers.add_parser("show", help="View a specific learning pack")
    show_parser.add_argument("pack_id", nargs="?", default="latest", help="Pack ID or 'latest'")
    show_parser.add_argument("--brief", action="store_true", help="Omit detailed mechanics")

    # flashcards
    fc_parser = subparsers.add_parser("flashcards", help="Review flashcards")
    fc_parser.add_argument("pack_id", nargs="?", default="latest", help="Pack ID or 'latest'")
    fc_parser.add_argument("-i", "--interactive", action="store_true", help="Run interactive terminal quiz")

    # export-anki
    anki_parser = subparsers.add_parser("export-anki", help="Export cards to Anki TSV")
    anki_parser.add_argument("pack_id", nargs="?", default="latest", help="Pack ID or 'latest'")
    anki_parser.add_argument("--out", type=str, help="Output file path")

    # html
    html_parser = subparsers.add_parser("html", help="View or open standalone HTML widget")
    html_parser.add_argument("pack_id", nargs="?", default="latest", help="Pack ID or 'latest'")
    html_parser.add_argument("--open", action="store_true", help="Open in default browser")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "generate":
        handle_generate(args)
    elif args.command == "list":
        handle_list(args)
    elif args.command == "show":
        handle_show(args)
    elif args.command == "flashcards":
        handle_flashcards(args)
    elif args.command == "export-anki":
        handle_export_anki(args)
    elif args.command == "html":
        handle_html(args)


if __name__ == "__main__":
    main()
