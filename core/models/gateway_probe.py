"""Validate model gateway IDs against an OpenRouter catalog snapshot.

The default mode is intentionally offline so this module can be used by local
and CI gates without depending on OpenRouter availability.  The versioned
snapshot is refreshed explicitly with::

    python -m core.models.gateway_probe --write-snapshot
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY_PATH = REPOSITORY_ROOT / ".factory/config/model_registry.json"
DEFAULT_SNAPSHOT_PATH = REPOSITORY_ROOT / ".factory/config/openrouter_catalog.json"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
UPDATE_COMMAND = "python -m core.models.gateway_probe --write-snapshot"


@dataclass(frozen=True)
class GatewayFinding:
    """A gateway ID from the registry which is absent from the snapshot."""

    model: str
    gateway_id: str

    @property
    def message(self) -> str:
        return (
            f"model {self.model!r} uses gateway id {self.gateway_id!r}, "
            "which is absent from the versioned OpenRouter catalog snapshot; "
            f"refresh it with `{UPDATE_COMMAND}`"
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
    if record.get("active") is False or record.get("enabled") is False:
        return False
    status = record.get("status")
    return status not in {"disabled", "inactive", "retired"}


def _string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for nested in value.values():
            yield from _string_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _string_values(nested)


def active_gateway_ids(
    registry: Any,
) -> Iterable[tuple[str, str]]:
    """Yield ``(model name, gateway ID)`` pairs for active models.

    Only ``gateway_ids`` is read.  In particular, ``cli_ids`` must never be
    interpreted as an OpenRouter wire ID.
    """

    for name, record in _model_records(registry):
        if not _is_active(record):
            continue
        for gateway_id in _string_values(record.get("gateway_ids")):
            yield name, gateway_id


def snapshot_model_ids(snapshot: Any) -> frozenset[str]:
    """Return IDs from either our snapshot or a raw OpenRouter response."""

    if not isinstance(snapshot, Mapping):
        raise ValueError("OpenRouter catalog must be a JSON object")

    values = snapshot.get("model_ids")
    if isinstance(values, list):
        return frozenset(value for value in values if isinstance(value, str))

    data = snapshot.get("data")
    if isinstance(data, list):
        return frozenset(
            item["id"]
            for item in data
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        )

    raise ValueError("OpenRouter catalog has neither model_ids nor data")


def probe_registry(
    registry_path: Path | str = DEFAULT_REGISTRY_PATH,
    snapshot_path: Path | str = DEFAULT_SNAPSHOT_PATH,
) -> list[GatewayFinding]:
    """Check active registry gateway IDs against the local snapshot."""

    registry = _read_json(Path(registry_path))
    snapshot = _read_json(Path(snapshot_path))
    available_ids = snapshot_model_ids(snapshot)

    return [
        GatewayFinding(model=model, gateway_id=gateway_id)
        for model, gateway_id in active_gateway_ids(registry)
        if gateway_id not in available_ids
    ]


def assert_registry_gateways_available(
    registry_path: Path | str = DEFAULT_REGISTRY_PATH,
    snapshot_path: Path | str = DEFAULT_SNAPSHOT_PATH,
) -> None:
    """Raise an actionable error when an active wire ID is unavailable."""

    findings = probe_registry(registry_path, snapshot_path)
    if findings:
        details = "\n".join(f"- {finding.message}" for finding in findings)
        raise AssertionError(f"OpenRouter gateway probe failed:\n{details}")


def _download_catalog(url: str = OPENROUTER_MODELS_URL) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "DarkFac-gateway-probe/1",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)

    if not isinstance(payload, Mapping):
        raise ValueError("OpenRouter returned a non-object catalog")
    return payload


def write_snapshot(
    snapshot_path: Path | str = DEFAULT_SNAPSHOT_PATH,
    *,
    source: str = OPENROUTER_MODELS_URL,
    catalog: Mapping[str, Any] | None = None,
    collected_at: str | None = None,
) -> Path:
    """Fetch and atomically write the normalized OpenRouter catalog."""

    payload = catalog if catalog is not None else _download_catalog(source)
    model_ids = sorted(snapshot_model_ids(payload))
    timestamp = collected_at or datetime.now(timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )

    snapshot = {
        "collected_at": timestamp,
        "source": source,
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
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY_PATH,
        help="model registry path",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=DEFAULT_SNAPSHOT_PATH,
        help="catalog snapshot path",
    )
    parser.add_argument(
        "--write-snapshot",
        action="store_true",
        help="download OpenRouter's catalog and rewrite the snapshot",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.write_snapshot:
        destination = write_snapshot(args.snapshot)
        print(f"Wrote OpenRouter catalog snapshot to {destination}")
        return 0

    findings = probe_registry(args.registry, args.snapshot)
    if not findings:
        print("OpenRouter gateway probe passed (offline snapshot)")
        return 0

    for finding in findings:
        print(finding.message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
