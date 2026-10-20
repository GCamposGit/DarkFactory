"""Model discovery (USR-185, slice 5).

Lists models from pluggable sources and proposes NEW ones as ``candidate`` entries in the registry.

Invariants:
* never promotes: only appends ``candidate`` entries; existing entries are never modified;
* idempotent: a model already known (id, alias or gateway id) is skipped, so a second run yields no diff;
* default cost is zero; a source with ``estimated_cost_usd > 0`` needs ``spend_cap_usd > 0`` and the total
  estimated cost of the run must not exceed the cap, otherwise the whole run is refused BEFORE any fetch;
* forbidden markers (fable, astra, ...) flag the candidate ``forbidden_autonomous`` (never bound to a role).
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Union

from pydantic import BaseModel, ConfigDict, Field

from core.models.registry import DEFAULT_REGISTRY_PATH, REPO_ROOT, ModelRegistry, parse_registry

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = REPO_ROOT / ".factory" / "config" / "model_discovery.json"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


class SpendCapExceeded(RuntimeError):
    """Raised when the estimated cost of a discovery run is not allowed by the configured cap."""


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    enabled: bool = True
    estimated_cost_usd: float = Field(default=0.0, ge=0)


class DiscoveryConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    spend_cap_usd: float = Field(default=0.0, ge=0)
    sources: dict[str, SourceConfig] = Field(default_factory=dict)


def load_config(path: Optional[Union[str, Path]] = None) -> DiscoveryConfig:
    """Load the discovery config; missing file => zero-cap default (free sources only)."""
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        return DiscoveryConfig.model_validate(json.loads(target.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return DiscoveryConfig()


class DiscoverySource(Protocol):
    name: str

    def estimated_cost_usd(self) -> float: ...

    def fetch(self) -> list[dict[str, Any]]:
        """Return raw provider records; implementations must not mutate the registry."""
        ...


def _per_mtok(value: Any) -> Optional[float]:
    """OpenRouter prices are USD per token (string); convert to USD per million tokens."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return round(number * 1_000_000, 6)


def _positive_int(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


class OpenRouterSource:
    """Public OpenRouter catalog (``GET /api/v1/models``): free, no auth."""

    name = "openrouter"

    def __init__(
        self,
        fetch_json: Optional[Callable[[], Any]] = None,
        cost_usd: float = 0.0,
        timeout: int = 15,
    ) -> None:
        self._fetch_json = fetch_json
        self._cost = cost_usd
        self._timeout = timeout

    def estimated_cost_usd(self) -> float:
        return self._cost

    def _http_json(self) -> Any:
        req = urllib.request.Request(
            OPENROUTER_MODELS_URL,
            headers={"User-Agent": "DarkFactory/1.0 (Autonomous Software Engine)", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 - fixed https URL
            return json.loads(resp.read().decode("utf-8"))

    def fetch(self) -> list[dict[str, Any]]:
        payload = (self._fetch_json or self._http_json)()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        return [item for item in data if isinstance(item, dict)]


def file_fetcher(path: Union[str, Path]) -> Callable[[], Any]:
    """Offline fetcher reading a saved catalog (fixtures, ``--fixture``)."""
    return lambda: json.loads(Path(path).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class DiscoveryResult:
    new_entries: list[dict[str, Any]] = field(default_factory=list)
    skipped_known: list[str] = field(default_factory=list)
    skipped_invalid: int = 0
    cost_usd: float = 0.0

    @property
    def changed(self) -> bool:
        return bool(self.new_entries)


def check_spend(sources: list[DiscoverySource], cap_usd: float) -> float:
    """Fail closed: refuse the run when paid sources exceed (or lack) the cap. Returns total estimated cost."""
    total = 0.0
    for source in sources:
        cost = source.estimated_cost_usd()
        if cost < 0:
            raise SpendCapExceeded(f"source {source.name!r} reports negative cost")
        if cost > 0 and cap_usd <= 0:
            raise SpendCapExceeded(f"source {source.name!r} is paid (${cost:.4f}) but spend_cap_usd is {cap_usd}")
        total += cost
    if total > cap_usd and total > 0:
        raise SpendCapExceeded(f"estimated cost ${total:.4f} exceeds spend_cap_usd ${cap_usd:.4f}")
    return total


def _known_names(registry: ModelRegistry) -> set[str]:
    names: set[str] = set()
    for entry in registry.models:
        for key in (entry.id, *entry.aliases, *entry.gateway_ids):
            low = key.strip().lower()
            names.add(low)
            if "/" in low:
                names.add(low.split("/", 1)[1])
    return names


def normalize_openrouter(item: dict[str, Any], markers: list[str], discovered_on: str) -> Optional[dict[str, Any]]:
    model_id = item.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    model_id = model_id.strip()
    provider = model_id.split("/", 1)[0].lower() if "/" in model_id else "unknown"
    pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
    top = item.get("top_provider") if isinstance(item.get("top_provider"), dict) else {}
    arch = item.get("architecture") if isinstance(item.get("architecture"), dict) else {}
    context = _positive_int(item.get("context_length")) or _positive_int(top.get("context_length"))
    release: Optional[str] = None
    created = item.get("created")
    if isinstance(created, (int, float)) and created > 0:
        release = datetime.fromtimestamp(created, tz=timezone.utc).date().isoformat()
    capabilities = ["text"]
    modalities = [str(m).lower() for m in (arch.get("input_modalities") or [])]
    if "image" in modalities:
        capabilities.append("vision")
    lowered = model_id.lower()
    forbidden = any(m.strip().lower() in lowered for m in markers if m.strip())
    return {
        "id": model_id,
        "provider": provider,
        "family": provider,
        "tier": "unclassified",
        "aliases": [],
        "gateway_ids": [],
        "status": "candidate",
        "price_input_per_mtok": _per_mtok(pricing.get("prompt")),
        "price_output_per_mtok": _per_mtok(pricing.get("completion")),
        "context_tokens": context,
        "max_output_tokens": _positive_int(top.get("max_completion_tokens")),
        "capabilities": capabilities,
        "forbidden_autonomous": forbidden,
        "release_date": release,
        "retirement_date": None,
        "discovered_at": discovered_on,
        "source": "openrouter",
    }


_NORMALIZERS: dict[str, Callable[[dict[str, Any], list[str], str], Optional[dict[str, Any]]]] = {
    "openrouter": normalize_openrouter,
}


def discover(
    registry: ModelRegistry,
    sources: list[DiscoverySource],
    spend_cap_usd: float = 0.0,
    today: Optional[str] = None,
) -> DiscoveryResult:
    """Pure computation of the new candidate entries (no write)."""
    cost = check_spend(sources, spend_cap_usd)
    day = today or datetime.now(timezone.utc).date().isoformat()
    known = _known_names(registry)
    markers = list(registry.forbidden_autonomous_markers)
    new_entries: list[dict[str, Any]] = []
    skipped: list[str] = []
    invalid = 0
    for source in sources:
        normalize = _NORMALIZERS.get(source.name)
        if normalize is None:
            raise ValueError(f"no normalizer for source {source.name!r}")
        for raw in source.fetch():
            entry = normalize(raw, markers, day)
            if entry is None:
                invalid += 1
                continue
            keys = {entry["id"].lower()}
            if "/" in entry["id"]:
                keys.add(entry["id"].lower().split("/", 1)[1])
            if keys & known:
                skipped.append(entry["id"])
                continue
            known.update(keys)
            new_entries.append(entry)
    return DiscoveryResult(new_entries, skipped, invalid, cost)


def apply_to_document(raw: dict[str, Any], new_entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Append candidates to a raw registry document, validate it, and return it. Existing data is untouched."""
    out = dict(raw)
    out["models"] = [*raw.get("models", []), *new_entries]
    parse_registry(out)  # fail closed on any invariant violation
    return out


def write_registry(path: Union[str, Path], document: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run(
    registry_path: Union[str, Path] = DEFAULT_REGISTRY_PATH,
    config: Optional[DiscoveryConfig] = None,
    sources: Optional[list[DiscoverySource]] = None,
    write: bool = False,
    today: Optional[str] = None,
) -> DiscoveryResult:
    """Run discovery against a registry file. ``write=False`` (default) never touches disk."""
    cfg = config or load_config()
    raw = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    registry = parse_registry(raw)
    if sources is None:
        sources = []
        for name, sc in cfg.sources.items():
            if sc.enabled and name == "openrouter":
                sources.append(OpenRouterSource(cost_usd=sc.estimated_cost_usd))
    result = discover(registry, sources, cfg.spend_cap_usd, today)
    if write and result.changed:
        write_registry(registry_path, apply_to_document(raw, result.new_entries))
    return result
