"""Provider Models API discovery sources and daily job (USR-185, slice 9a).

Adapters for Anthropic, OpenAI, xAI and Google Gemini model lists with injectable HTTP. A source without an API
key is skipped with a recorded reason; keys are read from env and never logged. New entries are ``candidate`` only
(reusing the slice-5 pipeline). The daily job is idempotent through a day marker under the runtime state root.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Union

from core.models import discovery
from core.models.registry import DEFAULT_REGISTRY_PATH
from core.paths import state_root
from core.usage.probe_deadline import ProbeTimeout, run_with_deadline

logger = logging.getLogger(__name__)

PROVIDER_ENDPOINTS: dict[str, dict[str, Any]] = {
    "anthropic": {"url": "https://api.anthropic.com/v1/models?limit=1000", "env": ("ANTHROPIC_API_KEY",)},
    "openai": {"url": "https://api.openai.com/v1/models", "env": ("OPENAI_API_KEY",)},
    "xai": {"url": "https://api.x.ai/v1/models", "env": ("XAI_API_KEY",)},
    "google": {
        "url": "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
        "env": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    },
}

HttpGet = Callable[[str, dict[str, str]], Any]


class MissingKey(RuntimeError):
    pass


def _default_http(url: str, headers: dict[str, str]) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "DarkFactory/1.0", "Accept": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - fixed https URLs
        return json.loads(resp.read().decode("utf-8"))


class ProviderApiSource:
    """Models API of one provider; ``fetch`` raises ``MissingKey`` when no key is configured."""

    def __init__(
        self,
        name: str,
        http_get: Optional[HttpGet] = None,
        env: Optional[dict[str, str]] = None,
        cost_usd: float = 0.0,
        deadline_s: Optional[float] = None,
    ) -> None:
        if name not in PROVIDER_ENDPOINTS:
            raise ValueError(f"unknown provider source {name!r}")
        self.name = name
        self._http = http_get or _default_http
        self._env = os.environ if env is None else env
        self._cost = cost_usd
        self._deadline = deadline_s

    def estimated_cost_usd(self) -> float:
        return self._cost

    def api_key(self) -> Optional[str]:
        for var in PROVIDER_ENDPOINTS[self.name]["env"]:
            value = (self._env.get(var) or "").strip()
            if value:
                return value
        return None

    def skip_reason(self) -> Optional[str]:
        if self.api_key() is None:
            return "missing_api_key:" + "|".join(PROVIDER_ENDPOINTS[self.name]["env"])
        return None

    def _headers(self, key: str) -> dict[str, str]:
        if self.name == "anthropic":
            return {"x-api-key": key, "anthropic-version": "2023-06-01"}
        if self.name == "google":
            return {"x-goog-api-key": key}
        return {"Authorization": f"Bearer {key}"}

    def fetch(self) -> list[dict[str, Any]]:
        key = self.api_key()
        if key is None:
            raise MissingKey(self.name)
        url = PROVIDER_ENDPOINTS[self.name]["url"]
        headers = self._headers(key)
        payload = run_with_deadline(f"discovery:{self.name}", lambda: self._http(url, headers), self._deadline)
        if not isinstance(payload, dict):
            return []
        items = payload.get("models") if self.name == "google" else payload.get("data")
        return [i for i in (items or []) if isinstance(i, dict)]


def _entry(model_id: str, provider: str, markers: list[str], day: str, **extra: Any) -> dict[str, Any]:
    lowered = model_id.lower()
    return {
        "id": model_id,
        "provider": provider,
        "family": provider,
        "tier": "unclassified",
        "aliases": [],
        "gateway_ids": [],
        "status": "candidate",
        "price_input_per_mtok": None,
        "price_output_per_mtok": None,
        "context_tokens": extra.get("context_tokens"),
        "max_output_tokens": extra.get("max_output_tokens"),
        "capabilities": ["text"],
        "forbidden_autonomous": any(m.strip().lower() in lowered for m in markers if m.strip()),
        "release_date": extra.get("release_date"),
        "retirement_date": None,
        "discovered_at": day,
        "source": provider,
    }


def _date_from(value: Any) -> Optional[str]:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, tz=timezone.utc).date().isoformat()
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    return None


def _simple(provider: str, date_key: str) -> Callable[[dict[str, Any], list[str], str], Optional[dict[str, Any]]]:
    def normalize(item: dict[str, Any], markers: list[str], day: str) -> Optional[dict[str, Any]]:
        mid = item.get("id")
        if not isinstance(mid, str) or not mid.strip():
            return None
        return _entry(mid.strip(), provider, markers, day, release_date=_date_from(item.get(date_key)))

    return normalize


def normalize_google(item: dict[str, Any], markers: list[str], day: str) -> Optional[dict[str, Any]]:
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    mid = name.strip().split("/", 1)[1] if name.startswith("models/") else name.strip()
    return _entry(
        mid,
        "google",
        markers,
        day,
        context_tokens=discovery._positive_int(item.get("inputTokenLimit")),
        max_output_tokens=discovery._positive_int(item.get("outputTokenLimit")),
    )


discovery._NORMALIZERS.update(
    {
        "anthropic": _simple("anthropic", "created_at"),
        "openai": _simple("openai", "created"),
        "xai": _simple("xai", "created"),
        "google": normalize_google,
    }
)


def build_sources(
    cfg: discovery.DiscoveryConfig,
    http_get: Optional[HttpGet] = None,
    env: Optional[dict[str, str]] = None,
) -> tuple[list[Any], dict[str, str]]:
    """Enabled sources with a key, plus ``{source: skip_reason}`` for the rest. Provider sources default to on."""
    sources: list[Any] = []
    skipped: dict[str, str] = {}
    for name in PROVIDER_ENDPOINTS:
        sc = cfg.sources.get(name, discovery.SourceConfig())
        if not sc.enabled:
            skipped[name] = "disabled"
            continue
        src = ProviderApiSource(name, http_get, env, cost_usd=sc.estimated_cost_usd)
        reason = src.skip_reason()
        if reason:
            skipped[name] = reason
        else:
            sources.append(src)
    sc = cfg.sources.get("openrouter", discovery.SourceConfig())
    if sc.enabled:
        sources.append(discovery.OpenRouterSource(cost_usd=sc.estimated_cost_usd))
    return sources, skipped


class _Prefetched:
    """Replays already fetched records so a failing source is isolated from the others."""

    def __init__(self, src: Any, records: list[dict[str, Any]]) -> None:
        self.name = src.name
        self._cost = src.estimated_cost_usd()
        self._records = records

    def estimated_cost_usd(self) -> float:
        return self._cost

    def fetch(self) -> list[dict[str, Any]]:
        return self._records


def _marker_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or state_root()) / "model_discovery" / "last_run.json"


def already_ran_today(today: str, state_dir: Optional[Path] = None) -> bool:
    try:
        return json.loads(_marker_path(state_dir).read_text(encoding="utf-8")).get("date") == today
    except (OSError, ValueError, AttributeError):
        return False


def _candidates_path(day: str, state_dir: Optional[Path] = None) -> Path:
    return (state_dir or state_root()) / "model_discovery" / f"candidates-{day}.json"


def run_daily_discovery(
    registry_path: Union[str, Path] = DEFAULT_REGISTRY_PATH,
    config: Optional[discovery.DiscoveryConfig] = None,
    http_get: Optional[HttpGet] = None,
    env: Optional[dict[str, str]] = None,
    state_dir: Optional[Path] = None,
    today: Optional[str] = None,
    sources: Optional[list[Any]] = None,
) -> Optional[dict[str, Any]]:
    """Run discovery at most once per UTC day. Never writes the registry or docs: new candidates go to
    ``<state>/model_discovery/candidates-<day>.json``. Returns the summary, or ``None`` when already done today."""
    day = today or datetime.now(timezone.utc).date().isoformat()
    if already_ran_today(day, state_dir):
        return None
    cfg = config or discovery.load_config()
    skipped: dict[str, str] = {}
    if sources is None:
        sources, skipped = build_sources(cfg, http_get, env)
    failed: dict[str, str] = {}
    live: list[Any] = []
    for src in sources:
        try:
            records = src.fetch()
        except ProbeTimeout:
            failed[src.name] = "probe_timeout"
            continue
        except Exception as exc:  # noqa: BLE001 - one source must not stop the others; no secrets in the reason
            failed[src.name] = type(exc).__name__
            continue
        live.append(_Prefetched(src, records))
    result = discovery.run(registry_path, cfg, live, write=False, today=day)
    out = _candidates_path(day, state_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    if result.new_entries:
        out.write_text(json.dumps(result.new_entries, indent=2, ensure_ascii=False) + chr(10), encoding="utf-8")
    summary = {
        "date": day,
        "new": [e["id"] for e in result.new_entries],
        "skipped_sources": skipped,
        "failed_sources": failed,
        "written": False,
    }
    marker = _marker_path(state_dir)
    marker.write_text(json.dumps(summary, indent=2) + chr(10), encoding="utf-8")
    logger.info(
        "model discovery daily: new=%d skipped=%s failed=%s", len(summary["new"]), skipped, failed
    )
    return summary


def pending_candidates(state_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """All candidate entries saved by daily runs (deduplicated by id, oldest first)."""
    base = (state_dir or state_root()) / "model_discovery"
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for path in sorted(base.glob("candidates-*.json")):
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].lower() not in seen:
                seen.add(item["id"].lower())
                out.append(item)
    return out


def apply_candidates(
    registry_path: Union[str, Path], state_dir: Optional[Path] = None
) -> list[str]:
    """Append pending candidates still unknown to the registry at ``registry_path`` (e.g. inside a PR worktree).
    Writes only that file (docs regeneration is the caller's job). Returns the ids applied."""
    raw = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    known = discovery._known_names(discovery.parse_registry(raw))
    fresh = []
    for entry in pending_candidates(state_dir):
        low = entry["id"].lower()
        keys = {low, low.split("/", 1)[1]} if "/" in low else {low}
        if keys & known:
            continue
        known.update(keys)
        fresh.append(entry)
    if fresh:
        discovery.write_registry(registry_path, discovery.apply_to_document(raw, fresh))
    return [e["id"] for e in fresh]
