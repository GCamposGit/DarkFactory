"""CLI: ``manifest``, ``verify`` and ``replay`` for the held-out E2E corpus.

Examples (PowerShell, absolute paths)::

    python -m evals.e2e.cli manifest --write
    python -m evals.e2e.cli verify
    python -m evals.e2e.cli replay --trajectories C:\\runs\\sysA --system sysA --out C:\\runs\\sysA-report.json

Output contains ids, hashes and metrics only; reserved case text is never printed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from evals.e2e.corpus import (
    DEFAULT_CORPUS_DIR,
    DEFAULT_MANIFEST_PATH,
    CorpusError,
    load_corpus,
    write_manifest,
)
from evals.e2e.leakage import LeakError
from evals.e2e.replay import ReplayError, build_report, load_trajectories

logger = logging.getLogger("evals.e2e")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals.e2e.cli", description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_DIR, help="directory with case JSON files")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH, help="manifest path")
    sub = parser.add_subparsers(dest="command", required=True)

    manifest = sub.add_parser("manifest", help="print (or --write) the versioned manifest")
    manifest.add_argument("--write", action="store_true")
    manifest.add_argument("--corpus-version", default="1.0.0", help="semantic version recorded in the manifest")

    sub.add_parser("verify", help="validate cases and check them against the manifest")

    replay = sub.add_parser("replay", help="replay recorded trajectories into a hashed report")
    replay.add_argument("--trajectories", type=Path, required=True, help="file or directory of trajectory JSON/JSONL")
    replay.add_argument("--system", default=None, help="system_id to report (required with several systems)")
    replay.add_argument("--out", type=Path, default=None, help="write the full report JSON here")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "manifest":
            corpus = load_corpus(args.corpus, manifest_path=None)
            corpus.corpus_version = args.corpus_version
            if args.write:
                manifest = write_manifest(corpus, args.manifest)
            else:
                manifest = corpus.build_manifest()
            print(json.dumps(manifest.model_dump(mode="json"), indent=2))
            return 0
        corpus = load_corpus(args.corpus, manifest_path=args.manifest)
        if args.command == "verify":
            print(f"ok cases={len(corpus)} manifest_sha256={corpus.build_manifest().manifest_sha256}")
            return 0
        report = build_report(corpus, load_trajectories(args.trajectories), system_id=args.system)
        if args.out is not None:
            args.out.write_text(json.dumps(report.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8", newline="\n")
        print(json.dumps(report.summary.model_dump(mode="json"), indent=2))
        print(f"report_sha256={report.report_sha256}")
        return 0
    except (CorpusError, ReplayError, LeakError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
