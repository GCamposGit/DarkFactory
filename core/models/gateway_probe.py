"""Validate active model gateway IDs against an OpenRouter catalog.

The default mode is offline: it reads the versioned snapshot and never calls
the network, so the gate can run without OpenRouter. Refresh the snapshot with::

    python -m core.models.gateway_probe --write-snapshot

A live check is ``fetch_catalog_ids()`` (OpenRouter, then the benchmark ledger).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY_PATH = REPOSITORY_ROOT / ".factory/config/model_registry.json"
DEFAULT_SNAPSHOT_PATH = REPOSITORY_ROOT / ".factory/config/openrouter_catalog.json"
UPDATE_COMMAND = "python -m core.models.gateway_probe --write-snapshot"

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


class CatalogError(Exception):
    """Empty catalog or a failed fetch. ``code`` is the structured error id."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class GatewayFinding:
    """An active model's effective gateway id is absent from the catalog."""

    model: str
    gateway_id: str
    reason: str = "absent_from_catalog"

    @property
    def message(self) -> str:
        return (
            f"model {self.model!r} uses gateway id {self.gateway_id!r}, "
            f"which is absent from the OpenRouter catalog ({self.reason}); "
            f"refresh the snapshot with `{UPDATE_COMMAND}`"
        )

    def __str__(self) -> str:
        return self.message


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _model_records(registry: Any) -> Iterable[tuple[str, Mapping[str, Any]]]:
    """Yield model names and records from supported registry layouts."""

    if not isinstance(registry, Mapping):
        return

    models = registry.get("models", registry)

    if isinstance(models, Mapping):
        for name, record in models.items():
            if isinstance(record, Mapping):
                yield str(name), record
        return

    if isinstance(models, list):
        for index, record in enumerate(models):
            if not isinstance(record, Mapping):
                continue
            name = (
                record.get("id")
                or record.get("name")
                or record.get("model")
                or f"models[{index}]"
            )
            yield str(name), record


def _is_active(record: Mapping[str, Any]) -> bool:
    """True only for routable models (``status == active``).

    Candidate and deprecated rows are naming leftovers. They are not required
    to exist in the live catalog. A record with no status stays active unless
    ``active`` or ``enabled`` is false.
    """

    if record.get("active") is False or record.get("enabled") is False:
        return False
    status = record.get("status")
    if isinstance(status, str):
        return status == "active"
    return True


def _string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for nested in value.values():
            yield from _string_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _string_values(nested)


def _declared_gateway_ids(record: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    for value in _string_values(record.get("gateway_ids")):
        text = value.strip()
        if text:
            found.append(text)
    return found


def _canonical_id(name: str, record: Mapping[str, Any]) -> str:
    raw = record.get("id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return name.strip()


def _effective_gateway_ids(name: str, record: Mapping[str, Any]) -> list[str]:
    """Declared wire ids, or the canonical id when none were declared.

    ``cli_ids`` are never wire ids. The canonical fallback is the same rule as
    ``registry.gateway_id`` when the model declares no gateway.
    """

    declared = _declared_gateway_ids(record)
    if declared:
        return declared
    canonical = _canonical_id(name, record)
    return [canonical] if canonical else []


def active_gateway_ids(registry: Any) -> Iterable[tuple[str, str]]:
    """Yield ``(model name, effective gateway id)`` for active models."""

    for name, record in _model_records(registry):
        if not _is_active(record):
            continue
        for gateway_id in _effective_gateway_ids(name, record):
            yield name, gateway_id


def _catalog_id_set(catalog_ids: Iterable[str] | None) -> frozenset[str]:
    if catalog_ids is None:
        return frozenset()
    return frozenset(item.strip() for item in catalog_ids if isinstance(item, str) and item.strip())


def check_gateway_ids(
    registry: Any,
    catalog_ids: Iterable[str] | None,
) -> list[GatewayFinding]:
    """Return findings for active models whose effective gateway is missing.

    An empty catalog is ``CatalogError('empty_catalog')``, not one finding per
    model. ``cli_ids`` are ignored.
    """

    available = _catalog_id_set(catalog_ids)
    if not available:
        raise CatalogError("empty_catalog", "OpenRouter catalog is empty")

    return [
        GatewayFinding(model=model, gateway_id=gateway_id)
        for model, gateway_id in active_gateway_ids(registry)
        if gateway_id not in available
    ]


def snapshot_model_ids(snapshot: Any) -> frozenset[str]:
    """Return IDs from either our snapshot or a raw OpenRouter response."""

    if not isinstance(snapshot, Mapping):
        raise ValueError("OpenRouter catalog must be a JSON object")

    values = snapshot.get("model_ids")
    if isinstance(values, list):
        return frozenset(value for value in values if isinstance(value, str) and value.strip())

    data = snapshot.get("data")
    if isinstance(data, list):
        return frozenset(
            item["id"]
            for item in data
            if isinstance(item, Mapping) and isinstance(item.get("id"), str) and item["id"].strip()
        )

    raise ValueError("OpenRouter catalog has neither model_ids nor data")


def probe_registry(
    registry_path: Path | str = DEFAULT_REGISTRY_PATH,
    snapshot_path: Path | str = DEFAULT_SNAPSHOT_PATH,
) -> list[GatewayFinding]:
    """Check active registry gateway IDs against the local snapshot."""

    registry = _read_json(Path(registry_path))
    snapshot = _read_json(Path(snapshot_path))
    return check_gateway_ids(registry, snapshot_model_ids(snapshot))


def assert_registry_gateways_available(
    registry_path: Path | str = DEFAULT_REGISTRY_PATH,
    snapshot_path: Path | str = DEFAULT_SNAPSHOT_PATH,
) -> None:
    """Raise an actionable error when an active wire ID is unavailable."""

    findings = probe_registry(registry_path, snapshot_path)
    if findings:
        details = "\n".join(f"- {finding.message}" for finding in findings)
        raise AssertionError(f"OpenRouter gateway probe failed:\n{details}")


def _download_catalog(url: str, *, timeout: float) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "DarkFac-gateway-probe/1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)

    if not isinstance(payload, Mapping):
        raise ValueError("OpenRouter returned a non-object catalog")
    return payload


def _ledger_model_ids(path: Path) -> frozenset[str]:
    """Model ids stored in the daily benchmark ledger (``.factory/benchmarks/latest.json``)."""

    payload = _read_json(path)
    if not isinstance(payload, Mapping):
        return frozenset()
    models = payload.get("models")
    found: set[str] = set()
    if isinstance(models, Mapping):
        for key, value in models.items():
            if isinstance(key, str) and key.strip():
                found.add(key.strip())
            if isinstance(value, Mapping):
                model_id = value.get("model_id") or value.get("id")
                if isinstance(model_id, str) and model_id.strip():
                    found.add(model_id.strip())
    elif isinstance(models, list):
        for item in models:
            if isinstance(item, str) and item.strip():
                found.add(item.strip())
            elif isinstance(item, Mapping):
                model_id = item.get("model_id") or item.get("id")
                if isinstance(model_id, str) and model_id.strip():
                    found.add(model_id.strip())
    return frozenset(found)


def _ids_from_ledger_or_raise(path: Path) -> frozenset[str]:
    try:
        ids = _ledger_model_ids(path)
    except (OSError, json.JSONDecodeError, ValueError):
        ids = frozenset()
    if not ids:
        raise CatalogError(
            "catalog_unavailable",
            "OpenRouter catalog is unavailable and the offline ledger has no model ids",
        )
    return ids


def fetch_catalog_ids(
    *,
    url: str | None = None,
    timeout: float = 30,
    ledger_path: Path | str | None = None,
) -> frozenset[str]:
    """Live OpenRouter model ids, with the benchmark ledger as offline fallback.

    Network failure uses the ledger. An empty live catalog, or a failure with
    no ledger ids, raises ``CatalogError``. The message does not include the
    exception text (no credentials, no upstream body).
    """

    from core.benchmarks.fetcher import LATEST_LEDGER_FILE, OPENROUTER_MODELS_URL

    source = url or OPENROUTER_MODELS_URL
    fallback = Path(ledger_path) if ledger_path is not None else LATEST_LEDGER_FILE
    try:
        payload = _download_catalog(source, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return _ids_from_ledger_or_raise(fallback)

    try:
        ids = snapshot_model_ids(payload)
    except ValueError as exc:
        raise CatalogError("catalog_unavailable", "OpenRouter catalog is unreadable") from exc
    if not ids:
        raise CatalogError("empty_catalog", "OpenRouter catalog is empty")
    return ids


def write_snapshot(
    snapshot_path: Path | str = DEFAULT_SNAPSHOT_PATH,
    *,
    source: str | None = None,
    catalog: Mapping[str, Any] | None = None,
    collected_at: str | None = None,
) -> Path:
    """Atomically write the normalized OpenRouter catalog. Empty input is an error."""

    from core.benchmarks.fetcher import OPENROUTER_MODELS_URL

    origin = source or OPENROUTER_MODELS_URL
    payload = catalog if catalog is not None else _download_catalog(origin, timeout=30)
    model_ids = sorted(snapshot_model_ids(payload))
    if not model_ids:
        raise CatalogError("empty_catalog", "OpenRouter catalog is empty")
    timestamp = collected_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    snapshot = {
        "collected_at": timestamp,
        "source": origin,
        "generated_by": UPDATE_COMMAND,
        "model_ids": model_ids,
    }

    destination = Path(snapshot_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")
    temporary.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return destination


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check OpenRouter wire IDs against a versioned catalog",
    )
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH, help="model registry path")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT_PATH, help="catalog snapshot path")
    parser.add_argument(
        "--write-snapshot",
        action="store_true",
        help="download OpenRouter's catalog and rewrite the snapshot",
    )
    parser.add_argument("--json", action="store_true", help="print a JSON report on stdout")
    return parser


def _emit_error(as_json: bool, exc: CatalogError) -> int:
    payload = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"OpenRouter gateway probe error [{exc.code}]: {exc}", file=sys.stderr)
    return EXIT_ERROR


def _emit_findings(as_json: bool, findings: list[GatewayFinding]) -> int:
    if as_json:
        print(json.dumps({
            "ok": not findings,
            "findings": [
                {
                    "model": finding.model,
                    "gateway_id": finding.gateway_id,
                    "reason": finding.reason,
                    "message": finding.message,
                }
                for finding in findings
            ],
        }, ensure_ascii=False))
    elif not findings:
        print("OpenRouter gateway probe passed (offline snapshot)")
    else:
        for finding in findings:
            print(finding.message, file=sys.stderr)
    return EXIT_FINDINGS if findings else EXIT_OK


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            return


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = _build_parser().parse_args(argv)

    try:
        if args.write_snapshot:
            from core.benchmarks.fetcher import OPENROUTER_MODELS_URL

            payload = _download_catalog(OPENROUTER_MODELS_URL, timeout=30)
            destination = write_snapshot(args.snapshot, source=OPENROUTER_MODELS_URL, catalog=payload)
            count = len(snapshot_model_ids(_read_json(destination)))
            if args.json:
                print(json.dumps({
                    "ok": True,
                    "snapshot": str(destination),
                    "model_count": count,
                    "findings": [],
                }, ensure_ascii=False))
            else:
                print(f"Wrote OpenRouter catalog snapshot to {destination} ({count} models)")
            return EXIT_OK
        findings = probe_registry(args.registry, args.snapshot)
    except CatalogError as exc:
        return _emit_error(args.json, exc)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return _emit_error(
            args.json,
            CatalogError("catalog_unavailable", f"OpenRouter catalog is unavailable ({type(exc).__name__})"),
        )
    return _emit_findings(args.json, findings)


if __name__ == "__main__":
    raise SystemExit(main())
