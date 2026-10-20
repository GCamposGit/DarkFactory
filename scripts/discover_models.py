"""Discover new models and register them as ``candidate`` (USR-185, slice 5).

Usage: python scripts/discover_models.py [--dry-run | --write] [--fixture FILE] [--registry FILE] [--config FILE]
Default is --dry-run (prints the diff only). --write updates the registry and regenerates the derived docs.
Never promotes or edits existing models. Default sources are free (OpenRouter public catalog).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models import discovery  # noqa: E402
from core.models.docgen import process  # noqa: E402
from core.models.registry import DEFAULT_REGISTRY_PATH  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show the diff only (default)")
    mode.add_argument("--write", action="store_true", help="write the registry and regenerate docs")
    parser.add_argument("--fixture", help="read the OpenRouter catalog from a local JSON file (offline)")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY_PATH))
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    cfg = discovery.load_config(args.config)
    sources = None
    if args.fixture:
        sc = cfg.sources.get("openrouter", discovery.SourceConfig())
        sources = [discovery.OpenRouterSource(discovery.file_fetcher(args.fixture), cost_usd=sc.estimated_cost_usd)]
    try:
        result = discovery.run(args.registry, cfg, sources, write=args.write)
    except discovery.SpendCapExceeded as exc:
        print(f"REFUSED (spend cap): {exc}")
        return 2
    for entry in result.new_entries:
        flag = " [forbidden-autonomous]" if entry["forbidden_autonomous"] else ""
        print(
            f"+ candidate {entry['id']} in={entry['price_input_per_mtok']} out={entry['price_output_per_mtok']} "
            f"ctx={entry['context_tokens']} released={entry['release_date']} source={entry['source']}{flag}"
        )
    print(
        f"new={len(result.new_entries)} known={len(result.skipped_known)} invalid={result.skipped_invalid} "
        f"cost_usd={result.cost_usd} mode={'write' if args.write else 'dry-run'}"
    )
    if args.write and result.changed and Path(args.registry).resolve() == DEFAULT_REGISTRY_PATH.resolve():
        for r in process(write=True):
            print(f"docs: {r.path} {'updated' if r.changed else 'ok'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
