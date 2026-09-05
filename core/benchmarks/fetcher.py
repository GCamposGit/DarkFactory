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


class DailyBenchmarkService:
    """Orchestrates once-per-day benchmarking of LLM frontier models."""

    def __init__(self, workspace_root: Optional[Path] = None):
        self.root = workspace_root or Path.cwd()
        self.benchmarks_dir = self.root / BENCHMARKS_DIR
        self.latest_file = self.root / LATEST_LEDGER_FILE
        self.history_dir = self.root / HISTORY_DIR

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
                return data.get("models", [])
        except Exception as exc:
            logger.error(f"Error loading baseline catalog: {exc}")
            return []

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
                data = json.loads(resp.read().decode("utf-8"))
                logger.info(f"Artificial Analysis API returned {len(data)} models.")
                return data if isinstance(data, list) else data.get("models", [])
        except Exception as exc:
            logger.warning(f"Artificial Analysis API query failed: {exc}")
            return []

    def build_daily_ledger(self, force: bool = False) -> DailyBenchmarkLedger:
        """
        Executes daily benchmark run, calculates Pareto efficiency frontier, and persists ledger.
        """
        today = self.get_today_str()

        # Step 1: Start from calibrated baseline catalog
        baseline_entries = self.load_baseline_catalog()
        model_entries_map: Dict[str, ModelBenchmarkEntry] = {}

        for b in baseline_entries:
            input_cost = float(b.get("input_cost_per_m", 0.0))
            output_cost = float(b.get("output_cost_per_m", 0.0))
            coding_score = float(b.get("coding_score", 70.0))
            intelligence_score = float(b.get("intelligence_score", 70.0))
            if "cost_per_task" in b:
                cost_per_task = float(b["cost_per_task"])
            else:
                cost_per_task = calculate_task_cost(input_cost, output_cost)

            eff_coding = calculate_efficiency_score(coding_score, cost_per_task)
            eff_general = calculate_efficiency_score(intelligence_score, cost_per_task)

            entry = ModelBenchmarkEntry(
                model_id=b["model_id"],
                name=b.get("name", b["model_id"]),
                provider=b.get("provider", "other"),
                context_length=int(b.get("context_length", 128000)),
                input_cost_per_m=input_cost,
                output_cost_per_m=output_cost,
                cost_per_task=cost_per_task,
                coding_score=coding_score,
                intelligence_score=intelligence_score,
                output_speed_tps=float(b.get("output_speed_tps", 80.0)),
                latency_ttft_sec=float(b.get("latency_ttft_sec", 0.8)),
                tokens_per_task=int(b.get("tokens_per_task", 2400)),
                efficiency_score_coding=eff_coding,
                efficiency_score_general=eff_general,
                tier=ModelTier(b.get("tier", "balanced_mid")),
                benchmark_source="artificial_analysis_calibrated",
                release_date=b.get("release_date"),
                cache_read_cost_per_m=b.get("cache_read_cost_per_m"),
                has_subscription_plan=b.get("has_subscription_plan", False),
                subscription_name=b.get("subscription_name"),
                marginal_cost_plan=0.0 if b.get("has_subscription_plan", False) else cost_per_task,
                domain_scores=b.get("domain_scores", {}),
                metadata=b.get("metadata", {}),
            )
            model_entries_map[entry.model_id] = entry

        # Step 2: Query OpenRouter live API to enrich and update token prices & new models
        openrouter_models = self.fetch_openrouter_catalog()
        for or_model in openrouter_models:
            mid = or_model.get("id", "")
            if not mid or "/" not in mid:
                continue

            provider_prefix = mid.split("/")[0].lower()
            if provider_prefix not in TRACKED_PROVIDERS:
                continue

            pricing = or_model.get("pricing", {})
            try:
                prompt_cost_m = float(pricing.get("prompt", 0.0)) * 1_000_000.0
                comp_cost_m = float(pricing.get("completion", 0.0)) * 1_000_000.0
            except (ValueError, TypeError):
                continue

            if mid in model_entries_map:
                entry = model_entries_map[mid]
                # Protect media-specific pricing (image/video models are priced per generation, not per 25k text tokens)
                if entry.metadata.get("modality") in ["image", "video"]:
                    continue

                # Update with real-time live prices
                entry.input_cost_per_m = round(prompt_cost_m, 4)
                entry.output_cost_per_m = round(comp_cost_m, 4)
                entry.cost_per_task = calculate_task_cost(entry.input_cost_per_m, entry.output_cost_per_m)
                entry.efficiency_score_coding = calculate_efficiency_score(entry.coding_score, entry.cost_per_task)
                entry.efficiency_score_general = calculate_efficiency_score(entry.intelligence_score, entry.cost_per_task)
                entry.benchmark_source = "openrouter_live_pricing"
                entry.marginal_cost_plan = 0.0 if entry.has_subscription_plan else entry.cost_per_task
            else:
                # Discovered newly released model on OpenRouter!
                # Estimate baseline scores using provider/model family heuristics
                name = or_model.get("name", mid)
                created_ts = or_model.get("created")
                rel_date = datetime.fromtimestamp(created_ts, timezone.utc).strftime("%Y-%m-%d") if created_ts else today

                # Infer capability estimation
                base_coding = 80.0
                base_intel = 82.0
                tier = ModelTier.BALANCED_MID
                if any(k in mid for k in ["pro", "opus", "astra", "max", "large"]):
                    base_coding = 86.0
                    base_intel = 88.0
                    tier = ModelTier.FRONTIER_HIGH
                elif any(k in mid for k in ["flash", "mini", "haiku", "small"]):
                    base_coding = 74.0
                    base_intel = 76.0
                    tier = ModelTier.FAST_ECONOMY

                cost_task = calculate_task_cost(prompt_cost_m, comp_cost_m)
                eff_c = calculate_efficiency_score(base_coding, cost_task)
                eff_g = calculate_efficiency_score(base_intel, cost_task)

                new_entry = ModelBenchmarkEntry(
                    model_id=mid,
                    name=name,
                    provider=provider_prefix,
                    context_length=int(or_model.get("context_length", 128000)),
                    input_cost_per_m=round(prompt_cost_m, 4),
                    output_cost_per_m=round(comp_cost_m, 4),
                    cost_per_task=cost_task,
                    coding_score=base_coding,
                    intelligence_score=base_intel,
                    output_speed_tps=100.0,
                    latency_ttft_sec=0.7,
                    tokens_per_task=2400,
                    efficiency_score_coding=eff_c,
                    efficiency_score_general=eff_g,
                    tier=tier,
                    benchmark_source="openrouter_discovered",
                    release_date=rel_date,
                )
                model_entries_map[mid] = new_entry

        # Step 3: Compute Pareto Frontiers & Archetypes
        models_list = list(model_entries_map.values())
        summary = build_frontier_summary(today, models_list)

        # Generate tier recommendations
        rec_high, _ = select_best_model_for_task(models_list, "coding", "high")
        rec_med, _ = select_best_model_for_task(models_list, "coding", "medium")
        rec_low, _ = select_best_model_for_task(models_list, "coding", "low")
        rec_local, _ = select_best_model_for_task(models_list, "coding", "medium", offline=True)

        recommendations = {
            "high_complexity": rec_high.model_id,
            "medium_complexity": rec_med.model_id,
            "low_complexity": rec_low.model_id,
            "offline_local": rec_local.model_id,
        }

        # Multi-domain Pareto frontiers (SWE-bench, GAIA, LegalBench, OSWorld, AIME, AudioBench)
        domain_frontiers = compute_domain_pareto_frontiers(models_list)
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


def ensure_daily_benchmark(force: bool = False, workspace_root: Optional[Path] = None) -> DailyBenchmarkLedger:
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
        return service.build_daily_ledger(force=force)
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
