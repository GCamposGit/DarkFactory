"""CLI: ``manifest``, ``verify``, ``replay``, ``run`` and ``compare`` for the held-out E2E corpus.

Examples (PowerShell, absolute paths)::

    python -m evals.e2e.cli manifest --write
    python -m evals.e2e.cli verify
    python -m evals.e2e.cli replay --trajectories C:\\runs\\sysA --system sysA --out C:\\runs\\sysA-report.json
    python -m evals.e2e run --runner my_pkg.my_runner:build --system sysA --out C:\\runs\\sysA
    python -m evals.e2e compare --base C:\\runs\\a\\report.json --candidate C:\\runs\\b\\report.json

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
    Corpus,
    CorpusError,
    load_corpus,
    write_manifest,
)
from evals.e2e.driver import RunnerSpecError, load_runner
from evals.e2e.leakage import LeakError
from evals.e2e.orchestrator import E2EOrchestrator, OrchestratorConfig, load_report
from evals.e2e.replay import (
    ReplayError,
    build_report,
    compare_reports,
    load_trajectories,
)

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

    run = sub.add_parser("run", help="run corpus cases against a line runner and report (needs an explicit --runner)")
    run.add_argument("--runner", default=None, help="'package.module:factory' building a LineRunner (required, no default)")
    run.add_argument("--system", required=True, help="system_id recorded in every trajectory")
    run.add_argument("--out", type=Path, required=True, help="empty output directory (trajectories/ and report.json)")
    run.add_argument("--cases", default=None, help="comma separated case ids (default: the whole corpus)")
    run.add_argument("--repeat", type=int, default=1, help="runs per case")
    run.add_argument("--run-label", default="run", help="prefix of the trajectory ids")
    run.add_argument("--poll-interval", type=float, default=5.0, help="seconds between polls of the line")
    run.add_argument("--no-faults", action="store_true", help="do not inject recovery.fault (baseline run)")

    compare = sub.add_parser("compare", help="compare two sealed reports of the same manifest")
    compare.add_argument("--base", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    return parser


def _run_command(args: argparse.Namespace, corpus: Corpus) -> int:
    if not args.runner:
        print("error: --runner is required (fail closed: no default runner, no environment fallback)", file=sys.stderr)
        return 2
    runner = load_runner(args.runner)
    cases = tuple(part.strip() for part in args.cases.split(",") if part.strip()) if args.cases else None
    config = OrchestratorConfig(
        system_id=args.system,
        run_label=args.run_label,
        poll_interval_seconds=args.poll_interval,
        repeats=args.repeat,
        inject_faults=not args.no_faults,
        case_ids=cases,
    )
    trajectories, report = E2EOrchestrator(corpus, runner, config).run_and_report(args.out)
    print(json.dumps(report.summary.model_dump(mode="json"), indent=2))
    print(f"trajectories={len(trajectories)} report_sha256={report.report_sha256}")
    return 0


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
        if args.command == "compare":
            comparison = compare_reports(load_report(args.base), load_report(args.candidate))
            print(json.dumps(comparison.model_dump(mode="json"), indent=2))
            return 0
        corpus = load_corpus(args.corpus, manifest_path=args.manifest)
        if args.command == "run":
            return _run_command(args, corpus)
        if args.command == "verify":
            print(f"ok cases={len(corpus)} manifest_sha256={corpus.build_manifest().manifest_sha256}")
            return 0
        report = build_report(corpus, load_trajectories(args.trajectories), system_id=args.system)
        if args.out is not None:
            args.out.write_text(json.dumps(report.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8", newline="\n")
        print(json.dumps(report.summary.model_dump(mode="json"), indent=2))
        print(f"report_sha256={report.report_sha256}")
        return 0
    except (CorpusError, ReplayError, LeakError, RunnerSpecError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
