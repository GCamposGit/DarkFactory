"""CLI for collecting and verifying the HF-01 baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from pydantic import ValidationError

from .baseline_models import BaselineManifest, BaselineSnapshot, EvidenceClaim, SourceStatus, VerificationReport
from .baseline_probes import collect_probe_observations, load_probe_config
from .baseline_reconcile import reconcile_baseline, source_fingerprint
from .baseline_render import render_baseline, render_sources
from .baseline_sources import BaselineCatalogError, collect_sources, load_catalog
from .baseline_verify import verify_snapshot

SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


class BaselineCliError(ValueError):
    """A safe, user-actionable CLI error."""


class BaselineRequiredSourceMissingError(BaselineCliError):
    """Raised when one or more required sources are missing or unreadable."""


class BaselineCorruptionError(ValueError):
    """Integrity, forgery or corruption error requiring exit code 3."""


def _load_claims(path: Path) -> list[EvidenceClaim]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BaselineCliError("CLAIMS_UNREADABLE") from exc
    raw_claims = payload.get("claims") if isinstance(payload, dict) else None
    if not isinstance(raw_claims, list):
        raise BaselineCliError("CLAIMS_SCHEMA_CHANGED")
    try:
        return [EvidenceClaim.model_validate(claim) for claim in raw_claims]
    except ValidationError as exc:
        raise BaselineCliError("CLAIMS_SCHEMA_CHANGED") from exc


def _git_base_sha(root: Path, explicit: str | None) -> str:
    if explicit:
        normalized = explicit.strip().lower()
        if not SHA_RE.fullmatch(normalized):
            raise BaselineCliError("BASE_SHA_INVALID")
        return normalized
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8", errors="strict")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BaselineCliError("BASE_SHA_UNAVAILABLE") from exc
    normalized = result.stdout.strip().lower()
    if not SHA_RE.fullmatch(normalized):
        raise BaselineCliError("BASE_SHA_INVALID")
    return normalized


def _snapshot_id(fingerprint: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"hf01-{timestamp}-{fingerprint[:8]}"


def _relative_output(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _write_atomic(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json_atomic(path: Path, payload: Any) -> None:
    _write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _collect(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    catalog_path = Path(args.catalog).resolve() if args.catalog else root / "docs" / "handoffs" / "hf01-sources.json"
    claims_path = Path(args.claims).resolve() if args.claims else root / "docs" / "handoffs" / "hf01-claims.json"
    if bool(args.out) == bool(args.out_root):
        raise BaselineCliError("OUTPUT_TARGET_REQUIRED")
    if not root.is_dir():
        raise BaselineCliError("ROOT_MISSING")
    catalog = load_catalog(catalog_path)
    collected = collect_sources(root, catalog)
    claims = _load_claims(claims_path)
    probes = ()
    if args.probe_config:
        probes = collect_probe_observations(load_probe_config(Path(args.probe_config).resolve()))
    base_sha = _git_base_sha(root, args.base_sha)
    fingerprint = source_fingerprint(collected.observations)
    snapshot = reconcile_baseline(collected, claims, base_sha=base_sha, snapshot_id=_snapshot_id(fingerprint), probe_observations=probes)
    output = Path(args.out).resolve() if args.out else Path(args.out_root).resolve() / snapshot.snapshot_id
    if output.exists():
        raise BaselineCliError("OUTPUT_EXISTS")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()

    catalog_rel = catalog_path.relative_to(root) if catalog_path.is_relative_to(root) else Path(catalog_path.name)
    claims_rel = claims_path.relative_to(root) if claims_path.is_relative_to(root) else Path(claims_path.name)
    probe_cfg_rel = None
    probe_cfg_sha = None
    if args.probe_config:
        probe_cfg_file = Path(args.probe_config).resolve()
        probe_cfg_rel = probe_cfg_file.relative_to(root) if probe_cfg_file.is_relative_to(root) else Path(probe_cfg_file.name)
        probe_cfg_sha = hashlib.sha256(probe_cfg_file.read_bytes()).hexdigest()

    manifest = BaselineManifest(
        snapshot_id=snapshot.snapshot_id,
        base_sha=snapshot.base_sha,
        source_fingerprint=snapshot.source_fingerprint,
        catalog_relative_path=catalog_rel,
        catalog_sha256=hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
        claims_relative_path=claims_rel,
        claims_sha256=hashlib.sha256(claims_path.read_bytes()).hexdigest(),
        probe_config_relative_path=probe_cfg_rel,
        probe_config_sha256=probe_cfg_sha,
        source_relative_paths=[obs.relative_path for obs in snapshot.source_observations],
    )

    try:
        _write_json_atomic(output / "snapshot.json", snapshot.model_dump(mode="json"))
        _write_atomic(output / "BASELINE.md", render_baseline(snapshot))
        _write_json_atomic(output / "sources.json", render_sources(collected, claims, snapshot))
        _write_json_atomic(output / "manifest.json", manifest.model_dump(mode="json"))
    except OSError as exc:
        raise BaselineCliError("OUTPUT_WRITE_FAILED") from exc
    print(json.dumps({"snapshot_id": snapshot.snapshot_id, "output": _relative_output(root, output), "completeness": snapshot.completeness.value, "hf02_readiness": snapshot.hf02_readiness.value, "source_count": len(snapshot.source_observations), "item_count": len(snapshot.items)}, ensure_ascii=False, sort_keys=True))

    spec_by_id = {spec.source_id: spec for spec in catalog}
    missing_required = [
        obs
        for obs in collected.observations
        if spec_by_id.get(obs.source_id)
        and spec_by_id[obs.source_id].required
        and obs.status != SourceStatus.READ
    ]
    if missing_required:
        for obs in missing_required:
            print(
                f"[BASELINE_ERROR] REQUIRED_SOURCE_MISSING: Source '{obs.source_id}' ({obs.relative_path.as_posix()}) status is '{obs.status.value}'",
                file=sys.stderr,
            )
        return 2

    return 0


def _verify(args: argparse.Namespace) -> int:
    snapshot_path = Path(args.snapshot).resolve()
    if not snapshot_path.is_file():
        raise BaselineCliError("SNAPSHOT_NOT_FOUND")
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot = BaselineSnapshot.model_validate(payload)
    except (OSError, UnicodeError, ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise BaselineCliError("SNAPSHOT_INVALID") from exc

    root = Path(args.root).resolve() if args.root else None
    manifest: BaselineManifest | None = None
    if getattr(args, "manifest", None):
        manifest_path = Path(args.manifest).resolve()
        if not manifest_path.is_file():
            raise BaselineCliError("MANIFEST_NOT_FOUND")
        try:
            manifest = BaselineManifest.model_validate_json(manifest_path.read_bytes())
        except (OSError, UnicodeError, ValidationError, ValueError) as exc:
            raise BaselineCliError("MANIFEST_INVALID") from exc
    elif (snapshot_path.parent / "manifest.json").is_file():
        try:
            manifest = BaselineManifest.model_validate_json((snapshot_path.parent / "manifest.json").read_bytes())
        except (OSError, UnicodeError, ValidationError, ValueError):
            manifest = None

    report = verify_snapshot(snapshot, root=root, manifest=manifest)
    if not report.valid:
        for err in report.errors:
            if report.exit_code == 3:
                print(f"[BASELINE_CORRUPTED] {err}", file=sys.stderr)
            else:
                print(f"[BASELINE_ERROR] {err}", file=sys.stderr)
        return report.exit_code

    print(f"[BASELINE_VERIFY_PASS] snapshot={snapshot.snapshot_id} sources={report.source_count} items={report.item_count}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect or verify an HF-01 baseline snapshot.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect", help="collect catalogued sources and write a new run")
    collect.add_argument("--root", default=".")
    collect.add_argument("--catalog")
    collect.add_argument("--claims")
    collect.add_argument("--probe-config")
    collect.add_argument("--base-sha")
    output_group = collect.add_mutually_exclusive_group(required=True)
    output_group.add_argument("--out")
    output_group.add_argument("--out-root")
    collect.set_defaults(handler=_collect)
    verify = subparsers.add_parser("verify", help="verify a snapshot and optionally replay source hashes")
    verify.add_argument("--snapshot", required=True)
    verify.add_argument("--root")
    verify.add_argument("--manifest")
    verify.set_defaults(handler=_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except BaselineCorruptionError as exc:
        print(f"[BASELINE_CORRUPTED] {exc}", file=sys.stderr)
        return 3
    except BaselineCliError as exc:
        print(f"[BASELINE_ERROR] {exc}", file=sys.stderr)
        return 2
    except (BaselineCatalogError, OSError, ValueError) as exc:
        print(f"[BASELINE_ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
