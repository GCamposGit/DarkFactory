"""
Daily Model Benchmark Fetcher & Ledger Service.
Runs once per calendar day to fetch frontier models, real-time pricing, and benchmarks.
Guarantees 100% fail-safe operation: never crashes callers, falls back to cached/baseline data.
"""

import os
import sys
import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

from core.benchmarks.models import (
    ModelBenchmarkEntry,
    DailyBenchmarkLedger,
    ModelTier,
    ModelProvider,
    FieldProvenance,
    MetricAcquisitionMode,
    MetricQuality,
    FIELD_UNITS,
)
from core.benchmarks.frontier import (
    DEFAULT_STANDARD_INPUT_TOKENS,
    DEFAULT_STANDARD_OUTPUT_TOKENS,
    calculate_task_cost,
    calculate_efficiency_score,
    compute_pareto_frontier,
    compute_domain_pareto_frontiers,
    build_frontier_summary,
    select_best_model_for_task,
)

logger = logging.getLogger("dark_factory.benchmarks")

BENCHMARKS_DIR = Path(".factory/benchmarks")
LATEST_LEDGER_FILE = BENCHMARKS_DIR / "latest.json"
HISTORY_DIR = BENCHMARKS_DIR / "history"
BASELINE_CATALOG_FILE = Path(__file__).parent / "data" / "benchmark_catalog.json"

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
ARTIFICIAL_ANALYSIS_MODELS_URL = "https://artificialanalysis.ai/api/v2/language/models"

TRACKED_PROVIDERS = {
    "openai",
    "anthropic",
    "google",
    "x-ai",
    "xai",
    "meta",
    "meta-llama",
    "deepseek",
    "qwen",
    "zhipu",
    "moonshot",
    "minimax",
    "mistralai",
    "mistral",
    "ollama",
    "black_forest",
    "recraft",
    "stability",
    "kuaishou",
    "luma",
    "runway",
    "local",
}


def _optional_float(value: Any) -> Optional[float]:
    """Parse a numeric field without turning absence into a fabricated zero."""

    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> Optional[int]:
    """Parse an integer field without assigning a default measurement."""

    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_numeric(payload: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = _optional_float(payload.get(key))
        if value is not None:
            return value
    return None


class DailyBenchmarkService:
    """Orchestrates once-per-day benchmarking of LLM frontier models."""

    def __init__(self, workspace_root: Optional[Path] = None):
        self.root = workspace_root or Path.cwd()
        self.benchmarks_dir = self.root / BENCHMARKS_DIR
        self.latest_file = self.root / LATEST_LEDGER_FILE
        self.history_dir = self.root / HISTORY_DIR
        self._catalog_field_provenance: Dict[str, Dict[str, Any]] = {}
        self._catalog_observed_at: Optional[str] = None
        self._last_openrouter_observed_at: Optional[str] = None
        self._last_artificial_analysis_observed_at: Optional[str] = None

    def get_today_str(self) -> str:
        """Returns current date in YYYY-MM-DD format (UTC)."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def should_run_today(self) -> bool:
        """
        Determines whether the benchmark needs to run today.
        Returns False if already executed today, True otherwise.
        """
        if not self.latest_file.exists():
            return True

        try:
            with open(self.latest_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                latest_date = data.get("date")
                today = self.get_today_str()
                if latest_date == today:
                    return False
        except Exception as exc:
            logger.warning(f"Failed to inspect existing latest ledger: {exc}. Will re-run.")
            return True

        return True

    def load_latest_ledger(self) -> Optional[DailyBenchmarkLedger]:
        """Loads latest saved ledger from disk if available."""
        if not self.latest_file.exists():
            return None
        try:
            with open(self.latest_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return DailyBenchmarkLedger.from_dict(data)
        except Exception as exc:
            logger.warning(f"Failed to read latest ledger from {self.latest_file}: {exc}")
            return None

    def load_baseline_catalog(self) -> List[Dict[str, Any]]:
        """Loads calibrated baseline catalog from package data."""
        if not BASELINE_CATALOG_FILE.exists():
            logger.warning(f"Baseline catalog not found at {BASELINE_CATALOG_FILE}")
            return []
        try:
            with open(BASELINE_CATALOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                self._catalog_field_provenance = data.get("field_provenance_defaults", {})
                self._catalog_observed_at = data.get("catalog_observed_at")
                return data.get("models", [])
        except Exception as exc:
            logger.error(f"Error loading baseline catalog: {exc}")
            return []

    def _catalog_provenance(
        self,
        field_name: str,
        model_data: Dict[str, Any],
    ) -> FieldProvenance:
        """Build provenance from the per-model or catalog-wide fixture contract."""

        raw = model_data.get("field_provenance", {}).get(field_name)
        if raw is None:
            raw = self._catalog_field_provenance.get(field_name)
        if raw is not None:
            return FieldProvenance.from_dict(raw)
        return FieldProvenance(
            source="benchmark_catalog.json",
            observed_at=self._catalog_observed_at,
            unit=FIELD_UNITS.get(field_name, "unknown"),
            acquisition_mode=MetricAcquisitionMode.OFFLINE_FIXTURE,
            quality=MetricQuality.REPORTED,
        )

    @staticmethod
    def _live_provenance(field_name: str, source: str, observed_at: str) -> FieldProvenance:
        return FieldProvenance(
            source=source,
            observed_at=observed_at,
            unit=FIELD_UNITS.get(field_name, "unknown"),
            acquisition_mode=MetricAcquisitionMode.LIVE_API,
            quality=MetricQuality.OBSERVED,
        )

    @staticmethod
    def _derived_provenance(field_name: str, note: str) -> FieldProvenance:
        return FieldProvenance(
            source="darkfac:derived",
            observed_at=None,
            unit=FIELD_UNITS.get(field_name, "unknown"),
            acquisition_mode=MetricAcquisitionMode.DERIVED,
            quality=MetricQuality.DERIVED,
            note=note,
        )

    @staticmethod
    def _api_model_id(model_data: Dict[str, Any]) -> str:
        value = model_data.get("id") or model_data.get("model_id") or model_data.get("model")
        return str(value) if value else ""

    def _apply_artificial_analysis_metrics(
        self,
        entries: Dict[str, ModelBenchmarkEntry],
        payloads: List[Dict[str, Any]],
    ) -> None:
        """Merge only explicitly returned metrics; never infer missing scores."""

        observed_at = self._last_artificial_analysis_observed_at or datetime.now(timezone.utc).isoformat()
        for payload in payloads:
            model_id = self._api_model_id(payload)
            entry = entries.get(model_id)
            if entry is None:
                continue

            field_values = {
                "coding_score": _first_numeric(
                    payload,
                    "coding_score",
                    "coding_agent_index",
                    "swe_bench_verified",
                ),
                "intelligence_score": _first_numeric(
                    payload,
                    "intelligence_score",
                    "intelligence_index",
                ),
                "output_speed_tps": _first_numeric(
                    payload,
                    "output_speed_tps",
                    "tokens_per_second",
                ),
                "latency_ttft_sec": _first_numeric(
                    payload,
                    "latency_ttft_sec",
                    "time_to_first_token",
                ),
                "tokens_per_task": _optional_int(payload.get("tokens_per_task")),
            }
            for field_name, value in field_values.items():
                if value is None:
                    continue
                setattr(entry, field_name, value)
                entry.field_provenance[field_name] = self._live_provenance(
                    field_name,
                    ARTIFICIAL_ANALYSIS_MODELS_URL,
                    observed_at,
                )

            raw_domains = payload.get("domain_scores")
            if isinstance(raw_domains, dict):
                domain_scores = {
                    str(key): float(value)
                    for key, value in raw_domains.items()
                    if _optional_float(value) is not None
                }
                if domain_scores:
                    entry.domain_scores.update(domain_scores)
                    entry.field_provenance["domain_scores"] = self._live_provenance(
                        "domain_scores",
                        ARTIFICIAL_ANALYSIS_MODELS_URL,
                        observed_at,
                    )
            if any(value is not None for value in field_values.values()) or raw_domains:
                entry.benchmark_source = "field_provenance"

    def fetch_openrouter_catalog(self, timeout: int = 10) -> List[Dict[str, Any]]:
        """
        Fetches live model catalog & pricing from OpenRouter public API (0 auth required).
        Returns list of relevant tracked models.
        """
        req = urllib.request.Request(
            OPENROUTER_MODELS_URL,
            headers={
                "User-Agent": "DarkFactory/1.0 (Autonomous Software Engine)",
                "Accept": "application/json"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                self._last_openrouter_observed_at = datetime.now(timezone.utc).isoformat()
                data = json.loads(resp.read().decode("utf-8"))
                models = data.get("data", [])
                logger.info(f"OpenRouter public API returned {len(models)} models.")
                return models
        except urllib.error.URLError as exc:
            logger.warning(f"OpenRouter API unreachable: {exc}. Using baseline catalog.")
            return []
        except Exception as exc:
            logger.warning(f"OpenRouter fetch error: {exc}. Using baseline catalog.")
            return []

    def fetch_artificial_analysis_api(self, api_key: Optional[str] = None, timeout: int = 10) -> List[Dict[str, Any]]:
        """
        Fetches benchmarks directly from Artificial Analysis v2 API if an API key is available.
        """
        key = api_key or os.environ.get("ARTIFICIAL_ANALYSIS_API_KEY")
        if not key:
            return []

        req = urllib.request.Request(
            ARTIFICIAL_ANALYSIS_MODELS_URL,
            headers={
                "x-api-key": key,
                "User-Agent": "DarkFactory/1.0",
                "Accept": "application/json"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                self._last_artificial_analysis_observed_at = datetime.now(timezone.utc).isoformat()
                data = json.loads(resp.read().decode("utf-8"))
                logger.info(f"Artificial Analysis API returned {len(data)} models.")
                return data if isinstance(data, list) else data.get("models", [])
        except Exception as exc:
            logger.warning(f"Artificial Analysis API query failed: {exc}")
            return []

    def build_daily_ledger(self, force: bool = False, offline: bool = False) -> DailyBenchmarkLedger:
        """
        Executes daily benchmark run, calculates Pareto efficiency frontier, and persists ledger.

        ``offline=True`` is a deterministic fixture mode: it never calls either
        external API and keeps unknown values unknown.
        """
        today = self.get_today_str()

        # Step 1: Start from calibrated baseline catalog
        baseline_entries = self.load_baseline_catalog()
        model_entries_map: Dict[str, ModelBenchmarkEntry] = {}

        for b in baseline_entries:
            input_cost = _optional_float(b.get("input_cost_per_m"))
            output_cost = _optional_float(b.get("output_cost_per_m"))
            coding_score = _optional_float(b.get("coding_score"))
            intelligence_score = _optional_float(b.get("intelligence_score"))
            cost_per_task = _optional_float(b.get("cost_per_task"))
            field_provenance = {
                field_name: self._catalog_provenance(field_name, b)
                for field_name in (
                    "context_length",
                    "input_cost_per_m",
                    "output_cost_per_m",
                    "cache_read_cost_per_m",
                    "cost_per_task",
                    "coding_score",
                    "intelligence_score",
                    "output_speed_tps",
                    "latency_ttft_sec",
                    "tokens_per_task",
                    "release_date",
                    "domain_scores",
                )
            }
            if cost_per_task is None and input_cost is not None and output_cost is not None:
                cost_per_task = calculate_task_cost(input_cost, output_cost)
                field_provenance["cost_per_task"] = self._derived_provenance(
                    "cost_per_task",
                    "Calculated from the observed input and output token prices.",
                )

            eff_coding = (
                calculate_efficiency_score(coding_score, cost_per_task)
                if coding_score is not None and cost_per_task is not None
                else None
            )
            eff_general = (
                calculate_efficiency_score(intelligence_score, cost_per_task)
                if intelligence_score is not None and cost_per_task is not None
                else None
            )

            entry = ModelBenchmarkEntry(
                model_id=b["model_id"],
                name=b.get("name", b["model_id"]),
                provider=b.get("provider", "other"),
                context_length=_optional_int(b.get("context_length")),
                input_cost_per_m=input_cost,
                output_cost_per_m=output_cost,
                cost_per_task=cost_per_task,
                coding_score=coding_score,
                intelligence_score=intelligence_score,
                output_speed_tps=_optional_float(b.get("output_speed_tps")),
                latency_ttft_sec=_optional_float(b.get("latency_ttft_sec")),
                tokens_per_task=_optional_int(b.get("tokens_per_task")),
                efficiency_score_coding=eff_coding,
                efficiency_score_general=eff_general,
                tier=ModelTier(b.get("tier", "balanced_mid")),
                benchmark_source="field_provenance",
                release_date=b.get("release_date"),
                cache_read_cost_per_m=_optional_float(b.get("cache_read_cost_per_m")),
                has_subscription_plan=b.get("has_subscription_plan", False),
                subscription_name=b.get("subscription_name"),
                marginal_cost_plan=0.0 if b.get("has_subscription_plan", False) else cost_per_task,
                domain_scores=b.get("domain_scores", {}),
                metadata=b.get("metadata", {}),
                field_provenance=field_provenance,
            )
            if eff_coding is not None:
                entry.field_provenance["efficiency_score_coding"] = self._derived_provenance(
                    "efficiency_score_coding",
                    "Calculated capability-to-cost index.",
                )
            if eff_general is not None:
                entry.field_provenance["efficiency_score_general"] = self._derived_provenance(
                    "efficiency_score_general",
                    "Calculated capability-to-cost index.",
                )
            model_entries_map[entry.model_id] = entry

        # Step 2: Merge directly measured benchmark values when available.
        if not offline:
            artificial_analysis_models = self.fetch_artificial_analysis_api()
            self._apply_artificial_analysis_metrics(model_entries_map, artificial_analysis_models)

        # Step 3: Query OpenRouter live API to enrich token prices & discover models.
        openrouter_models = [] if offline else self.fetch_openrouter_catalog()
        for or_model in openrouter_models:
            mid = or_model.get("id", "")
            if not mid or "/" not in mid:
                continue

            provider_prefix = mid.split("/")[0].lower()
            if provider_prefix not in TRACKED_PROVIDERS:
                continue

            pricing = or_model.get("pricing", {}) or {}
            prompt_raw = _optional_float(pricing.get("prompt"))
            completion_raw = _optional_float(pricing.get("completion"))
            if prompt_raw is None and completion_raw is None:
                continue
            prompt_cost_m = prompt_raw * 1_000_000.0 if prompt_raw is not None else None
            comp_cost_m = completion_raw * 1_000_000.0 if completion_raw is not None else None

            if mid in model_entries_map:
                entry = model_entries_map[mid]
                # Protect media-specific pricing (image/video models are priced per generation, not per 25k text tokens)
                if entry.metadata.get("modality") in ["image", "video"]:
                    continue

                # Update with real-time live prices
                observed_at = self._last_openrouter_observed_at or datetime.now(timezone.utc).isoformat()
                if prompt_cost_m is not None:
                    entry.input_cost_per_m = round(prompt_cost_m, 4)
                    entry.field_provenance["input_cost_per_m"] = self._live_provenance(
                        "input_cost_per_m", OPENROUTER_MODELS_URL, observed_at
                    )
                if comp_cost_m is not None:
                    entry.output_cost_per_m = round(comp_cost_m, 4)
                    entry.field_provenance["output_cost_per_m"] = self._live_provenance(
                        "output_cost_per_m", OPENROUTER_MODELS_URL, observed_at
                    )
                if entry.input_cost_per_m is not None and entry.output_cost_per_m is not None:
                    entry.cost_per_task = calculate_task_cost(
                        entry.input_cost_per_m,
                        entry.output_cost_per_m,
                    )
                    entry.field_provenance["cost_per_task"] = self._derived_provenance(
                        "cost_per_task",
                        "Calculated from the live OpenRouter price fields.",
                    )
                if entry.coding_score is not None and entry.cost_per_task is not None:
                    entry.efficiency_score_coding = calculate_efficiency_score(
                        entry.coding_score, entry.cost_per_task
                    )
                if entry.intelligence_score is not None and entry.cost_per_task is not None:
                    entry.efficiency_score_general = calculate_efficiency_score(
                        entry.intelligence_score, entry.cost_per_task
                    )
                entry.benchmark_source = "field_provenance"
                entry.marginal_cost_plan = 0.0 if entry.has_subscription_plan else entry.cost_per_task
            else:
                # Discovered model: availability/pricing are known, capability is not.
                # Never promote a name-based heuristic into a benchmark measurement.
                name = or_model.get("name", mid)
                created_ts = _optional_int(or_model.get("created"))
                rel_date = (
                    datetime.fromtimestamp(created_ts, timezone.utc).strftime("%Y-%m-%d")
                    if created_ts is not None
                    else None
                )
                tier_value = or_model.get("tier", ModelTier.BALANCED_MID.value)
                try:
                    tier = ModelTier(tier_value)
                except ValueError:
                    tier = ModelTier.BALANCED_MID
                observed_at = self._last_openrouter_observed_at or datetime.now(timezone.utc).isoformat()
                discovered_provenance = {
                    "context_length": self._live_provenance(
                        "context_length", OPENROUTER_MODELS_URL, observed_at
                    ),
                    "input_cost_per_m": self._live_provenance(
                        "input_cost_per_m", OPENROUTER_MODELS_URL, observed_at
                    ),
                    "output_cost_per_m": self._live_provenance(
                        "output_cost_per_m", OPENROUTER_MODELS_URL, observed_at
                    ),
                }
                context_length = _optional_int(or_model.get("context_length"))
                cost_task = (
                    calculate_task_cost(prompt_cost_m, comp_cost_m)
                    if prompt_cost_m is not None and comp_cost_m is not None
                    else None
                )
                if cost_task is not None:
                    discovered_provenance["cost_per_task"] = self._derived_provenance(
                        "cost_per_task",
                        "Calculated from the live OpenRouter price fields.",
                    )
                if rel_date is not None:
                    discovered_provenance["release_date"] = self._live_provenance(
                        "release_date", OPENROUTER_MODELS_URL, observed_at
                    )

                new_entry = ModelBenchmarkEntry(
                    model_id=mid,
                    name=name,
                    provider=provider_prefix,
                    context_length=context_length,
                    input_cost_per_m=round(prompt_cost_m, 4) if prompt_cost_m is not None else None,
                    output_cost_per_m=round(comp_cost_m, 4) if comp_cost_m is not None else None,
                    cost_per_task=cost_task,
                    coding_score=None,
                    intelligence_score=None,
                    output_speed_tps=None,
                    latency_ttft_sec=None,
                    tokens_per_task=None,
                    efficiency_score_coding=None,
                    efficiency_score_general=None,
                    tier=tier,
                    benchmark_source="field_provenance",
                    release_date=rel_date,
                    field_provenance=discovered_provenance,
                )
                model_entries_map[mid] = new_entry

        # Recompute derived efficiencies after all live fields have been merged.
        for entry in model_entries_map.values():
            if entry.coding_score is not None and entry.cost_per_task is not None:
                entry.efficiency_score_coding = calculate_efficiency_score(
                    entry.coding_score,
                    entry.cost_per_task,
                )
                entry.field_provenance["efficiency_score_coding"] = self._derived_provenance(
                    "efficiency_score_coding",
                    "Calculated from the final known coding score and task cost.",
                )
            if entry.intelligence_score is not None and entry.cost_per_task is not None:
                entry.efficiency_score_general = calculate_efficiency_score(
                    entry.intelligence_score,
                    entry.cost_per_task,
                )
                entry.field_provenance["efficiency_score_general"] = self._derived_provenance(
                    "efficiency_score_general",
                    "Calculated from the final known intelligence score and task cost.",
                )

        # Step 4: Compute Pareto Frontiers using only sufficiently evidenced entries.
        models_list = list(model_entries_map.values())
        rankable_models = [model for model in models_list if model.is_rankable()]
        summary = build_frontier_summary(today, rankable_models)

        recommendations: Dict[str, str] = {}
        if rankable_models:
            rec_high, _ = select_best_model_for_task(rankable_models, "coding", "high")
            rec_med, _ = select_best_model_for_task(rankable_models, "coding", "medium")
            rec_low, _ = select_best_model_for_task(rankable_models, "coding", "low")
            rec_local, _ = select_best_model_for_task(rankable_models, "coding", "medium", offline=True)
            recommendations = {
                "high_complexity": rec_high.model_id,
                "medium_complexity": rec_med.model_id,
                "low_complexity": rec_low.model_id,
                "offline_local": rec_local.model_id,
            }

        # Multi-domain Pareto frontiers (SWE-bench, GAIA, LegalBench, OSWorld, AIME, AudioBench)
        domain_frontiers = compute_domain_pareto_frontiers(rankable_models)
        frontiers_by_domain = {d: [m.model_id for m in f] for d, f in domain_frontiers.items()}

        ledger = DailyBenchmarkLedger(
            date=today,
            updated_at=datetime.now(timezone.utc).isoformat(),
            total_models_scanned=len(models_list),
            frontier_models_count=len(summary.coding_frontier),
            pareto_coding_models=[m.model_id for m in summary.coding_frontier],
            pareto_general_models=[m.model_id for m in summary.general_frontier],
            frontiers_by_domain=frontiers_by_domain,
            models=model_entries_map,
            recommendations_by_tier=recommendations,
            metadata={
                "task_tokens_prompt": DEFAULT_STANDARD_INPUT_TOKENS,
                "task_tokens_completion": DEFAULT_STANDARD_OUTPUT_TOKENS,
                "most_efficient_coding": summary.most_cost_efficient_coding.model_id if summary.most_cost_efficient_coding else None,
                "highest_capability_coding": summary.highest_capability_coding.model_id if summary.highest_capability_coding else None,
                "supported_domains": list(domain_frontiers.keys()),
                "offline_fixture": offline,
                "rankable_models_count": len(rankable_models),
                "unknown_capability_models": sorted(
                    model.model_id for model in models_list if not model.is_rankable()
                ),
            }
        )

        # Step 4: Persist ledger to disk
        self.save_ledger(ledger)
        return ledger

    def save_ledger(self, ledger: DailyBenchmarkLedger) -> None:
        """Persists the daily ledger to history and updates latest.json."""
        self.benchmarks_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)

        history_file = self.history_dir / f"{ledger.date}.json"
        ledger_dict = ledger.to_dict()

        try:
            with open(history_file, "w", encoding="utf-8") as f:
                json.dump(ledger_dict, f, indent=2)

            with open(self.latest_file, "w", encoding="utf-8") as f:
                json.dump(ledger_dict, f, indent=2)

            logger.info(f"Saved daily benchmark ledger for {ledger.date} to {self.latest_file}")
        except Exception as exc:
            logger.error(f"Failed to persist benchmark ledger: {exc}")


_default_service: Optional[DailyBenchmarkService] = None


def get_daily_benchmark_service(workspace_root: Optional[Path] = None) -> DailyBenchmarkService:
    """Returns singleton DailyBenchmarkService instance."""
    global _default_service
    if _default_service is None or workspace_root is not None:
        _default_service = DailyBenchmarkService(workspace_root)
    return _default_service


def ensure_daily_benchmark(
    force: bool = False,
    workspace_root: Optional[Path] = None,
    offline: bool = False,
) -> DailyBenchmarkLedger:
    """
    Hook called by Dark Factory entry points.
    If already run today and force=False, returns cached ledger in <1ms without network calls.
    Otherwise, executes the daily update with full fail-safe error handling.
    """
    service = get_daily_benchmark_service(workspace_root)
    if not force and not service.should_run_today():
        cached = service.load_latest_ledger()
        if cached:
            return cached

    try:
        return service.build_daily_ledger(force=force, offline=offline)
    except Exception as exc:
        logger.warning(f"ensure_daily_benchmark encountered error: {exc}. Returning fallback.")
        cached = service.load_latest_ledger()
        if cached:
            return cached
        # Fallback: create ledger purely from baseline
        today = service.get_today_str()
        return DailyBenchmarkLedger(
            date=today,
            updated_at=datetime.now(timezone.utc).isoformat(),
            total_models_scanned=0,
            frontier_models_count=0,
            pareto_coding_models=[],
            pareto_general_models=[],
            models={},
            recommendations_by_tier={},
            metadata={"error": str(exc)}
        )
