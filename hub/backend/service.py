"""
Headless business logic layer for DarkHub.
Decoupled from Web UI and HTTP frameworks (Reachability standard).
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Dict, List, Optional, Tuple, Any

from hub.backend.models import (
    ExportCatalogResponse,
    HealthCheckResult,
    HealthStatus,
    ImportCatalogRequest,
    ImportCatalogResponse,
    OllamaGenerateRequest,
    OllamaGenerateResponse,
    OllamaModelInfo,
    OllamaStatusResponse,
    OpenRouterModelInfo,
    OpenRouterStatusResponse,
    PlaygroundProvider,
    PromptTemplate,
    ServiceCategory,
    ServiceCreate,
    ServiceItem,
    ServiceUpdate,
    UnifiedGenerateRequest,
    UnifiedGenerateResponse,
    LearningPackSummary,
    GenerateLearningPackRequest,
    GenerateLearningPackResponse,
    ContentGenerateRequest,
    ContentLintRequest,
    VisualGenerateRequest,
    VisualIllustrateRequest,
)
from core.content import (
    ContentEngine,
    AntiSlopLinter,
    ContentRequest,
    ContentType,
    CONTENT_PRESETS,
)
from core.visual import (
    VisualStudio,
    VisualPromptSpec,
    AssetType,
    VisualTheme,
    AspectRatio,
)
from core.learning_pack import (
    LearningPackStore,
    LearningPackGenerator,
    LearningPackRenderer,
)
from core.benchmarks import (
    BenchmarkDomain,
    DOMAIN_METADATA,
    DailyBenchmarkLedger,
    ensure_daily_benchmark,
    get_benchmark_router,
    compute_pareto_frontier,
    is_production_interactive_model,
)
from core.usage.ledger import ModelUsageLedger, infer_model_tier
from core.usage.models import ModelCallEvent, ModelModality, ModelTier
from core.usage.monitor import AccountUsageMonitor
from core.roadmap.models import (
    RoadmapHealth,
    RoadmapItem,
    RoadmapProjectSummary,
    RoadmapSnapshot,
    RoadmapSourceDocument,
)
from core.roadmap.service import build_repository_roadmap_service

logger = logging.getLogger("darkhub.service")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


class HubService:
    def __init__(
        self,
        data_dir: Optional[Path] = None,
        ollama_base_url: str = "http://localhost:11434",
        usage_dir: Optional[Path] = None,
        roadmap_root: Optional[Path] = None,
    ) -> None:
        if data_dir is None:
            # Default to hub/data relative to this file
            self.data_dir = Path(__file__).resolve().parent.parent / "data"
        else:
            self.data_dir = Path(data_dir)

        self.services_file = self.data_dir / "services.json"
        self.default_services_file = self.data_dir / "default_services.json"
        self.prompts_file = self.data_dir / "default_prompts.json"
        self.ollama_base_url = ollama_base_url
        if usage_dir is not None:
            self.usage_dir = Path(usage_dir)
        elif data_dir is not None:
            self.usage_dir = self.data_dir / "usage"
        else:
            self.usage_dir = Path(__file__).resolve().parents[2] / ".factory" / "usage"
        self.model_usage_ledger = ModelUsageLedger(self.usage_dir)
        self.account_usage_monitor = AccountUsageMonitor(self.usage_dir / "providers")
        repository_root = roadmap_root or Path(__file__).resolve().parents[2]
        self.roadmap = build_repository_roadmap_service(repository_root)

        self._ensure_storage()

    def list_roadmap_projects(self) -> List[RoadmapProjectSummary]:
        """List projects with an isolated roadmap source."""

        return self.roadmap.list_projects()

    def get_roadmap(
        self,
        project_id: str,
        *,
        search: Optional[str] = None,
        item_type: Optional[str] = None,
        lifecycle_stage: Optional[str] = None,
        delivery_status: Optional[str] = None,
        horizon: Optional[str] = None,
        confidence: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> RoadmapSnapshot:
        """Return a filtered, read-only operational roadmap snapshot."""

        return self.roadmap.get_snapshot(
            project_id,
            search=search,
            item_type=item_type,
            lifecycle_stage=lifecycle_stage,
            delivery_status=delivery_status,
            horizon=horizon,
            confidence=confidence,
            source_id=source_id,
        )

    def get_roadmap_item(self, project_id: str, item_id: str) -> Optional[RoadmapItem]:
        return self.roadmap.get_item(project_id, item_id)

    def get_roadmap_health(self, project_id: str) -> RoadmapHealth:
        return self.roadmap.get_health(project_id)

    def get_roadmap_source(self, project_id: str, source_id: str) -> Optional[RoadmapSourceDocument]:
        return self.roadmap.get_source_document(project_id, source_id)

    def _ensure_storage(self) -> None:
        """Ensures the storage directory and initial files exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.services_file.exists():
            if self.default_services_file.exists():
                logger.info("Initializing services.json from default_services.json")
                content = self.default_services_file.read_text(encoding="utf-8")
                self.services_file.write_text(content, encoding="utf-8")
            else:
                logger.warning("Default services file missing. Creating empty list.")
                self.services_file.write_text("[]", encoding="utf-8")

    def _record_model_call(
        self,
        *,
        provider: str,
        model: str,
        harness: str = "darkhub",
        modality: ModelModality = ModelModality.TEXT,
        success: bool = True,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        cost_usd: Optional[float] = None,
        latency_ms: Optional[float] = None,
        source: str,
    ) -> None:
        """Persist telemetry fail-open so observability never breaks inference."""
        try:
            self.model_usage_ledger.record(ModelCallEvent(
                provider=provider,
                model=model,
                tier=ModelTier(infer_model_tier(provider, model)),
                harness=harness,
                modality=modality,
                success=success,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                source=source,
            ))
        except Exception as exc:
            logger.warning("Model telemetry write failed: %s", exc)

    def get_account_usage(self, force: bool = False) -> Any:
        """Return a partial-success report for every known AI platform."""
        return self.account_usage_monitor.inspect(force=force)

    def get_model_usage(self) -> Any:
        """Return project-wide model counters and recent attempts."""
        return self.model_usage_ledger.report()

    def record_model_usage(self, event: ModelCallEvent) -> Any:
        """Allow Codex, Grok, Gemini and other harnesses to report calls."""
        self.model_usage_ledger.record(event)
        return self.model_usage_ledger.report()

    def _load_services_raw(self) -> List[Dict]:
        try:
            with open(self.services_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception as exc:
            logger.error(f"Failed to read services from {self.services_file}: {exc}")
            return []

    def _save_services_raw(self, items: List[Dict]) -> None:
        temp_file = self.services_file.with_suffix(".tmp")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(items, f, indent=2, ensure_ascii=False)
            temp_file.replace(self.services_file)
            logger.info(f"Successfully saved {len(items)} services to {self.services_file}")
        except Exception as exc:
            if temp_file.exists():
                temp_file.unlink()
            logger.error(f"Error saving services: {exc}")
            raise

    def list_services(
        self,
        category: Optional[ServiceCategory] = None,
        search: Optional[str] = None,
        favorites_only: bool = False,
        pinned_only: bool = False,
    ) -> List[ServiceItem]:
        raw_items = self._load_services_raw()
        results: List[ServiceItem] = []

        search_term = search.strip().lower() if search else None

        for raw in raw_items:
            try:
                item = ServiceItem(**raw)
            except Exception as exc:
                logger.warning(f"Skipping malformed service item {raw.get('id', 'unknown')}: {exc}")
                continue

            if category and item.category != category:
                continue

            if favorites_only and not item.is_favorite:
                continue

            if pinned_only and not item.pinned:
                continue

            if search_term:
                match_name = search_term in item.name.lower()
                match_desc = search_term in item.description.lower()
                match_tags = any(search_term in t.lower() for t in item.tags)
                match_url = search_term in item.url.lower()
                if not (match_name or match_desc or match_tags or match_url):
                    continue

            results.append(item)

        return results

    def get_service(self, service_id: str) -> Optional[ServiceItem]:
        raw_items = self._load_services_raw()
        for raw in raw_items:
            if raw.get("id") == service_id:
                try:
                    return ServiceItem(**raw)
                except Exception as exc:
                    logger.error(f"Malformed service {service_id}: {exc}")
                    return None
        return None

    def _generate_slug(self, name: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
        return slug or "service"

    def create_service(self, create_data: ServiceCreate) -> ServiceItem:
        raw_items = self._load_services_raw()

        base_id = self._generate_slug(create_data.name)
        new_id = base_id
        counter = 1
        existing_ids = {r.get("id") for r in raw_items}

        while new_id in existing_ids:
            new_id = f"{base_id}-{counter}"
            counter += 1

        is_local = "localhost" in create_data.url or "127.0.0.1" in create_data.url

        new_item = ServiceItem(
            id=new_id,
            name=create_data.name,
            url=create_data.url,
            category=create_data.category,
            description=create_data.description,
            tags=create_data.tags,
            icon=create_data.icon,
            color=create_data.color,
            is_favorite=create_data.is_favorite,
            pinned=create_data.pinned,
            is_local=create_data.is_local or is_local,
        )

        raw_items.append(new_item.model_dump())
        self._save_services_raw(raw_items)
        return new_item

    def update_service(self, service_id: str, update_data: ServiceUpdate) -> Optional[ServiceItem]:
        raw_items = self._load_services_raw()
        updated_item: Optional[ServiceItem] = None

        for i, raw in enumerate(raw_items):
            if raw.get("id") == service_id:
                item = ServiceItem(**raw)
                update_dict = update_data.model_dump(exclude_unset=True)

                if "url" in update_dict:
                    url_val = update_dict["url"]
                    if "is_local" not in update_dict:
                        update_dict["is_local"] = "localhost" in url_val or "127.0.0.1" in url_val

                updated_item = item.model_copy(update=update_dict)
                raw_items[i] = updated_item.model_dump()
                break

        if updated_item:
            self._save_services_raw(raw_items)
        return updated_item

    def delete_service(self, service_id: str) -> bool:
        raw_items = self._load_services_raw()
        initial_count = len(raw_items)
        filtered = [r for r in raw_items if r.get("id") != service_id]

        if len(filtered) < initial_count:
            self._save_services_raw(filtered)
            return True
        return False

    def toggle_favorite(self, service_id: str) -> Optional[ServiceItem]:
        service = self.get_service(service_id)
        if not service:
            return None
        return self.update_service(service_id, ServiceUpdate(is_favorite=not service.is_favorite))

    def toggle_pin(self, service_id: str) -> Optional[ServiceItem]:
        service = self.get_service(service_id)
        if not service:
            return None
        return self.update_service(service_id, ServiceUpdate(pinned=not service.pinned))

    def ping_url(self, service_id: str, url: str, timeout_sec: float = 2.5) -> HealthCheckResult:
        """Pings a service URL and measures round-trip latency."""
        start = time.perf_counter()
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "DarkHub-Ping/1.0 (Headless Health Probe)"},
            method="HEAD",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as response:
                latency = round((time.perf_counter() - start) * 1000, 1)
                return HealthCheckResult(
                    service_id=service_id,
                    url=url,
                    status=HealthStatus.ONLINE,
                    latency_ms=latency,
                    status_code=response.getcode(),
                )
        except urllib.error.HTTPError as http_err:
            latency = round((time.perf_counter() - start) * 1000, 1)
            # HTTP errors (e.g. 401, 403, 405) still mean the server is reachable and online
            status = HealthStatus.ONLINE if http_err.code < 500 else HealthStatus.DEGRADED
            return HealthCheckResult(
                service_id=service_id,
                url=url,
                status=status,
                latency_ms=latency,
                status_code=http_err.code,
            )
        except Exception as exc:
            # Fallback retry with GET if HEAD was disallowed by remote
            try:
                start_get = time.perf_counter()
                req_get = urllib.request.Request(
                    url,
                    headers={"User-Agent": "DarkHub-Ping/1.0"},
                    method="GET",
                )
                with urllib.request.urlopen(req_get, timeout=timeout_sec) as response:
                    latency = round((time.perf_counter() - start_get) * 1000, 1)
                    return HealthCheckResult(
                        service_id=service_id,
                        url=url,
                        status=HealthStatus.ONLINE,
                        latency_ms=latency,
                        status_code=response.getcode(),
                    )
            except Exception as get_exc:
                return HealthCheckResult(
                    service_id=service_id,
                    url=url,
                    status=HealthStatus.OFFLINE,
                    latency_ms=None,
                    error=str(get_exc),
                )

    def get_ollama_status(self) -> OllamaStatusResponse:
        """Inspects local Ollama instance and lists installed models."""
        url = f"{self.ollama_base_url}/api/tags"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})

        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                raw_models = data.get("models", [])
                models: List[OllamaModelInfo] = []
                for m in raw_models:
                    details = m.get("details", {})
                    models.append(
                        OllamaModelInfo(
                            name=m.get("name", "unknown"),
                            parameter_size=details.get("parameter_size"),
                            quantization=details.get("quantization_level"),
                            format=details.get("format"),
                            modified_at=m.get("modified_at"),
                        )
                    )

                return OllamaStatusResponse(
                    is_online=True,
                    base_url=self.ollama_base_url,
                    model_count=len(models),
                    models=models,
                )
        except Exception as exc:
            logger.info(f"Ollama local ping offline or unreachable: {exc}")
            return OllamaStatusResponse(
                is_online=False,
                base_url=self.ollama_base_url,
                model_count=0,
                models=[],
                error=str(exc),
            )

    def generate_ollama(self, req_data: OllamaGenerateRequest) -> OllamaGenerateResponse:
        """Executes a prompt against local Ollama instance without streaming."""
        url = f"{self.ollama_base_url}/api/generate"
        payload = {
            "model": req_data.model,
            "prompt": req_data.prompt,
            "stream": False,
            "options": {"temperature": req_data.temperature},
        }
        if req_data.system:
            payload["system"] = req_data.system

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=60.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                duration_ms = round((time.perf_counter() - start) * 1000, 1)
                returned_model = data.get("model", req_data.model)
                self._record_model_call(
                    provider="ollama",
                    model=returned_model,
                    input_tokens=data.get("prompt_eval_count"),
                    output_tokens=data.get("eval_count"),
                    cost_usd=0.0,
                    latency_ms=duration_ms,
                    source="hub.generate_ollama",
                )
                return OllamaGenerateResponse(
                    response=data.get("response", ""),
                    model=returned_model,
                    done=data.get("done", True),
                    total_duration_ms=duration_ms,
                )
        except Exception as exc:
            self._record_model_call(
                provider="ollama",
                model=req_data.model,
                success=False,
                latency_ms=round((time.perf_counter() - start) * 1000, 1),
                source="hub.generate_ollama",
            )
            logger.error(f"Error querying Ollama model {req_data.model}: {exc}")
            raise RuntimeError(f"Ollama generation failed: {exc}")

    def list_prompts(self, search: Optional[str] = None) -> List[PromptTemplate]:
        """Loads prompt templates catalog."""
        if not self.prompts_file.exists():
            return []

        try:
            with open(self.prompts_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                templates = [PromptTemplate(**item) for item in data]

                if search:
                    term = search.strip().lower()
                    templates = [
                        t
                        for t in templates
                        if term in t.title.lower()
                        or term in t.description.lower()
                        or any(term in tag.lower() for tag in t.tags)
                    ]
                return templates
        except Exception as exc:
            logger.error(f"Failed to read prompt templates: {exc}")
            return []

    def get_latest_benchmark_ledger(self) -> Dict[str, Any]:
        """Loads or builds latest daily benchmark ledger."""
        from core.benchmarks.fetcher import ensure_daily_benchmark
        ledger = ensure_daily_benchmark()
        return ledger.to_dict()

    def get_benchmark_frontier(self) -> Dict[str, Any]:
        """Builds Pareto efficiency frontier summary from latest ledger."""
        from core.benchmarks.fetcher import ensure_daily_benchmark
        from core.benchmarks.frontier import build_frontier_summary
        ledger = ensure_daily_benchmark()
        all_models = list(ledger.models.values())
        summary = build_frontier_summary(ledger.date, all_models)
        return summary.to_dict()

    def refresh_benchmarks(self) -> Dict[str, Any]:
        """Forces an on-demand refresh of daily model benchmarks."""
        from core.benchmarks.fetcher import ensure_daily_benchmark
        ledger = ensure_daily_benchmark(force=True)
        return ledger.to_dict()

    def get_benchmark_proximity(self) -> Dict[str, Any]:
        """Calculates Frontier Proximity Index (FPI), epsilon gaps and challenger status."""
        from core.benchmarks.fetcher import ensure_daily_benchmark
        from core.benchmarks.frontier import compute_frontier_proximity_indices
        ledger = ensure_daily_benchmark()
        all_models = list(ledger.models.values())
        compute_frontier_proximity_indices(all_models, metric="coding_score")
        return {
            "date": ledger.date,
            "models": [m.to_dict() for m in sorted(all_models, key=lambda m: (-m.frontier_proximity_index, -m.coding_score))]
        }

    def get_top_candidates_for_tier(self, tier: str = "high", k: int = 3) -> List[Dict[str, Any]]:
        """Returns the top-3 speculative candidate models for a given tier."""
        from core.benchmarks.fetcher import ensure_daily_benchmark
        from core.benchmarks.frontier import get_top_candidates_for_tier
        ledger = ensure_daily_benchmark()
        all_models = list(ledger.models.values())
        candidates = get_top_candidates_for_tier(all_models, tier=tier, k=k)
        return [c.to_dict() for c in candidates]

    def get_empirical_ledger(self) -> Dict[str, Any]:
        """Returns cumulative empirical Dark Factory benchmark stats and Elo leaderboard."""
        from core.benchmarks.racing import EmpiricalBenchmarkLedger
        ledger = EmpiricalBenchmarkLedger()
        return {
            "total_models_tracked": len(ledger.stats),
            "total_races_recorded": len(ledger.history),
            "leaderboard": {
                mid: stat.to_dict()
                for mid, stat in sorted(
                    ledger.stats.items(),
                    key=lambda item: item[1].elo_rating,
                    reverse=True,
                )
            },
            "recent_races": [r.to_dict() for r in ledger.history[-20:]],
        }

    def run_speculative_race(self, task_id: str, prompt: str, complexity: str = "high", offline: bool = False) -> Dict[str, Any]:
        """Executes a speculative cascade race."""
        from core.benchmarks.racing import SpeculativeRacingEngine
        engine = SpeculativeRacingEngine()
        result = engine.execute_speculative_race(
            task_id=task_id,
            task_prompt=prompt,
            complexity=complexity,
            offline=offline,
        )
        return result.to_dict()

    def get_openrouter_key(self) -> Optional[str]:
        """Recovers OpenRouter API key from environment variable or Windows registry."""
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key and sys.platform == "win32":
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
                    key, _ = winreg.QueryValueEx(k, "OPENROUTER_API_KEY")
            except Exception:
                pass
        return key.strip() if (key and key.strip()) else None

    def get_openrouter_status(self) -> OpenRouterStatusResponse:
        """Inspects OpenRouter authentication, balance/usage, and curated frontier models."""
        key = self.get_openrouter_key()
        curated_models = [
            OpenRouterModelInfo(
                id="nvidia/nemotron-3.5-lightning:free",
                name="Nvidia Nemotron 3.5 (Free)",
                context_length=128000,
                input_cost_per_m=0.0,
                output_cost_per_m=0.0,
                is_pareto=True,
            ),
            OpenRouterModelInfo(
                id="anthropic/claude-3.7-sonnet",
                name="Claude 3.7 Sonnet (Thinking)",
                context_length=200000,
                input_cost_per_m=3.0,
                output_cost_per_m=15.0,
                is_pareto=True,
            ),
            OpenRouterModelInfo(
                id="deepseek/deepseek-r1",
                name="DeepSeek R1 (Full Reasoning)",
                context_length=128000,
                input_cost_per_m=0.55,
                output_cost_per_m=2.19,
                is_pareto=True,
            ),
            OpenRouterModelInfo(
                id="google/gemini-2.0-flash-001",
                name="Google Gemini 2.0 Flash",
                context_length=1000000,
                input_cost_per_m=0.10,
                output_cost_per_m=0.40,
                is_pareto=True,
            ),
            OpenRouterModelInfo(
                id="meta-llama/llama-3.3-70b-instruct",
                name="Meta Llama 3.3 70B Instruct",
                context_length=128000,
                input_cost_per_m=0.13,
                output_cost_per_m=0.40,
                is_pareto=True,
            ),
            OpenRouterModelInfo(
                id="qwen/qwen-2.5-coder-32b-instruct",
                name="Qwen 2.5 Coder 32B Instruct",
                context_length=32768,
                input_cost_per_m=0.07,
                output_cost_per_m=0.16,
                is_pareto=True,
            ),
        ]

        if not key:
            return OpenRouterStatusResponse(
                has_key=False,
                is_authenticated=False,
                models=curated_models,
                error="Chave OPENROUTER_API_KEY não configurada no sistema.",
            )

        req_auth = urllib.request.Request(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {key}", "HTTP-Referer": "https://github.com/DarkFac"},
        )
        try:
            with urllib.request.urlopen(req_auth, timeout=6.0) as resp:
                data = json.loads(resp.read().decode("utf-8")).get("data", {})
                return OpenRouterStatusResponse(
                    has_key=True,
                    is_authenticated=True,
                    key_label=data.get("label"),
                    is_free_tier=bool(data.get("is_free_tier", False)),
                    usage_usd=float(data.get("usage", 0.0)),
                    models=curated_models,
                )
        except Exception as exc:
            logger.warning(f"Failed to authenticate with OpenRouter: {exc}")
            return OpenRouterStatusResponse(
                has_key=True,
                is_authenticated=False,
                models=curated_models,
                error=f"Falha na autenticação com OpenRouter: {exc}",
            )

    def generate_openrouter(self, req_data: UnifiedGenerateRequest) -> UnifiedGenerateResponse:
        """Executes prompt against OpenRouter API with full latency, token and cost calculation."""
        key = self.get_openrouter_key()
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY não encontrada no ambiente ou registro.")

        messages = []
        if req_data.system:
            messages.append({"role": "system", "content": req_data.system})
        messages.append({"role": "user", "content": req_data.prompt})

        payload = {
            "model": req_data.model,
            "messages": messages,
            "temperature": req_data.temperature,
            "max_tokens": req_data.max_tokens or 1024,
        }

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/DarkFac",
                "X-Title": "DarkHub AI Playground",
            },
            method="POST",
        )

        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=90.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                duration_ms = round((time.perf_counter() - start) * 1000, 1)

                choices = data.get("choices", [])
                response_text = choices[0].get("message", {}).get("content", "") if choices else ""
                usage = data.get("usage", {})
                tokens_used = usage.get("total_tokens")

                # Estimate cost if available from benchmark catalog
                cost_usd = None
                try:
                    from core.benchmarks.fetcher import ensure_daily_benchmark
                    ledger = ensure_daily_benchmark()
                    model_entry = ledger.models.get(req_data.model)
                    if model_entry and usage:
                        prompt_tokens = usage.get("prompt_tokens", 0)
                        completion_tokens = usage.get("completion_tokens", 0)
                        cost_usd = round(
                            (prompt_tokens * model_entry.input_cost_per_m + completion_tokens * model_entry.output_cost_per_m) / 1_000_000,
                            6
                        )
                except Exception:
                    pass

                returned_model = data.get("model", req_data.model)
                self._record_model_call(
                    provider="openrouter",
                    model=returned_model,
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                    cost_usd=cost_usd,
                    latency_ms=duration_ms,
                    source="hub.generate_openrouter",
                )
                return UnifiedGenerateResponse(
                    response=response_text,
                    provider=PlaygroundProvider.OPENROUTER,
                    model=returned_model,
                    done=True,
                    total_duration_ms=duration_ms,
                    tokens_used=tokens_used,
                    cost_usd=cost_usd,
                )
        except Exception as exc:
            self._record_model_call(
                provider="openrouter",
                model=req_data.model,
                success=False,
                latency_ms=round((time.perf_counter() - start) * 1000, 1),
                source="hub.generate_openrouter",
            )
            logger.error(f"OpenRouter generate failed for {req_data.model}: {exc}")
            raise RuntimeError(f"OpenRouter generation failed: {exc}")

    def generate_unified(self, req_data: UnifiedGenerateRequest) -> UnifiedGenerateResponse:
        """Dispatches generation to Ollama or OpenRouter."""
        if req_data.provider == PlaygroundProvider.OPENROUTER:
            return self.generate_openrouter(req_data)

        # Provider == Ollama
        ollama_req = OllamaGenerateRequest(
            model=req_data.model,
            prompt=req_data.prompt,
            system=req_data.system,
            temperature=req_data.temperature,
        )
        res = self.generate_ollama(ollama_req)
        return UnifiedGenerateResponse(
            response=res.response,
            provider=PlaygroundProvider.OLLAMA,
            model=res.model,
            done=res.done,
            total_duration_ms=res.total_duration_ms,
            tokens_used=None,
            cost_usd=0.0,
        )

    def export_services_data(self) -> ExportCatalogResponse:
        """Exports full catalog of services with timestamp and version."""
        services = self.list_services()
        now_iso = datetime.now(timezone.utc).isoformat()
        return ExportCatalogResponse(
            exported_at=now_iso,
            version="1.0",
            count=len(services),
            services=services,
        )

    def import_services_data(self, req: ImportCatalogRequest) -> ImportCatalogResponse:
        """Imports and saves catalog with automatic backup."""
        # Create backup of current file
        if self.services_file.exists():
            backup_file = self.services_file.with_suffix(".backup.json")
            try:
                backup_file.write_text(self.services_file.read_text(encoding="utf-8"), encoding="utf-8")
                logger.info(f"Created catalog backup at {backup_file}")
            except Exception as exc:
                logger.warning(f"Failed to create backup: {exc}")

        current_items = self._load_services_raw()
        if req.merge:
            existing_map = {item.get("id"): item for item in current_items}
            for new_service in req.services:
                existing_map[new_service.id] = new_service.model_dump()
            final_items = list(existing_map.values())
        else:
            final_items = [s.model_dump() for s in req.services]

        self._save_services_raw(final_items)
        return ImportCatalogResponse(
            imported_count=len(req.services),
            total_count=len(final_items),
            message="Catálogo importado com sucesso!",
        )

    def reset_to_defaults(self) -> int:
        """Restores catalog from default_services.json."""
        if not self.default_services_file.exists():
            raise FileNotFoundError("Arquivo default_services.json não encontrado.")

        content = self.default_services_file.read_text(encoding="utf-8")
        self.services_file.write_text(content, encoding="utf-8")
        items = json.loads(content)
        return len(items) if isinstance(items, list) else 0

    # -------------------------------------------------------------
    # Learning Packs & Cognitive Uplift Engine
    # -------------------------------------------------------------
    def list_learning_packs(self) -> List[Dict[str, Any]]:
        """List metadata summaries for all recorded session learning packs."""
        store = LearningPackStore()
        return store.list_packs()

    def get_learning_pack(self, pack_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve full structured JSON for a specific pack or 'latest'."""
        store = LearningPackStore()
        pack = store.load_pack(pack_id)
        return pack.to_dict() if pack else None

    def generate_learning_pack(
        self,
        title: Optional[str] = None,
        session_id: Optional[str] = None,
        files: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Synthesizes a fresh session learning pack, stores all artifacts, and returns summary."""
        generator = LearningPackGenerator()
        store = LearningPackStore()
        pack = generator.generate_pack(title=title, session_id=session_id, files_analyzed=files)
        saved = store.save_pack(pack)
        rendered_md = LearningPackRenderer.render_markdown(pack)
        return {
            "pack_id": pack.pack_id,
            "title": pack.title,
            "executive_summary": pack.executive_summary,
            "concepts_count": len(pack.concepts),
            "flashcards_count": len(pack.flashcards),
            "saved_paths": saved,
            "rendered_md": rendered_md,
        }

    def export_learning_pack_anki(self, pack_id: str) -> Optional[str]:
        """Returns Anki TSV formatted deck for a learning pack."""
        store = LearningPackStore()
        pack = store.load_pack(pack_id)
        if not pack:
            return None
        return LearningPackRenderer.render_anki_tsv(pack)

    def get_learning_pack_html(self, pack_id: str) -> Optional[str]:
        """Returns standalone interactive HTML content for a learning pack."""
        store = LearningPackStore()
        pack = store.load_pack(pack_id)
        if not pack:
            return None
        return LearningPackRenderer.render_html(pack)

    # -------------------------------------------------------------
    # Task-Adaptive Benchmark Router & Multi-Domain Engine
    # -------------------------------------------------------------
    def get_benchmark_domains(self) -> List[Dict[str, Any]]:
        """List metadata summaries for all 6 canonical benchmark domains."""
        domains: List[Dict[str, Any]] = []
        for meta in DOMAIN_METADATA.values():
            domains.append({
                "domain_key": meta.domain.value,
                "name": meta.display_name,
                "canonical_benchmark": meta.canonical_benchmark,
                "evaluates": meta.evaluates,
                "source_authority": meta.source_authority,
                "target_metric_scale": meta.target_metric_scale,
            })
        return domains

    def get_domain_frontier(self, domain_key: str) -> Optional[Dict[str, Any]]:
        """Retrieve Pareto efficiency frontier for a specific domain."""
        meta = DOMAIN_METADATA.get(domain_key)
        if not meta:
            return None

        ledger = ensure_daily_benchmark()
        frontier_ids = ledger.frontiers_by_domain.get(domain_key, [])

        frontier_models = []
        for fid in frontier_ids:
            if fid in ledger.models:
                frontier_models.append(ledger.models[fid])

        # If empty or not yet cached, compute on the fly
        if not frontier_models:
            prod_models = [m for m in ledger.models.values() if is_production_interactive_model(m)]
            frontier_models = compute_pareto_frontier(prod_models or list(ledger.models.values()), metric=domain_key)

        return {
            "domain": domain_key,
            "domain_name": meta.display_name,
            "canonical_benchmark": meta.canonical_benchmark,
            "evaluates": meta.evaluates,
            "frontier_count": len(frontier_models),
            "models": [m.to_dict() for m in frontier_models],
        }


    def route_task_benchmark(
        self,
        requirement: str,
        complexity: str = "medium",
        offline: bool = False,
        domain_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Route a task requirement to the optimal benchmark domain and frontier models."""
        router = get_benchmark_router()
        decision = router.route_task(
            requirement=requirement,
            complexity=complexity,
            offline=offline,
            domain_override=domain_override,
        )
        return decision.to_dict()

    # ---------------------------------------------------------------------------
    # Anti-AI-Slop Content Engine Service Methods
    # ---------------------------------------------------------------------------

    def generate_content(self, payload: ContentGenerateRequest) -> Dict[str, Any]:
        """Generates content via ContentEngine and enforces Anti-Slop thresholds."""
        engine = ContentEngine()
        req = ContentRequest(
            topic=payload.topic,
            content_type=ContentType(payload.content_type),
            target_audience=payload.target_audience,
            key_points=payload.key_points,
            raw_context=payload.raw_context,
            offline=payload.offline,
            model_override=payload.model_override,
        )
        resp = engine.generate(req)
        return {
            "content_id": resp.content_id,
            "title": resp.title,
            "content_type": resp.content_type.value,
            "final_content": resp.final_content,
            "initial_slop_score": resp.initial_slop_score,
            "final_slop_score": resp.final_slop_score,
            "cleanliness_rating": resp.slop_report.cleanliness_rating.value,
            "word_count": resp.word_count,
            "provider": resp.provider,
            "model_used": resp.model_used,
            "created_at": resp.created_at,
        }

    def lint_content(self, payload: ContentLintRequest) -> Dict[str, Any]:
        """Audits text content for AI slop and returns structured report."""
        linter = AntiSlopLinter(custom_banned_words=payload.custom_banned_words)
        report = linter.audit(payload.text)
        return {
            "slop_score": report.slop_score,
            "cleanliness_rating": report.cleanliness_rating.value,
            "violations_count": report.violations_count,
            "cadence_rating": report.cadence_rating,
            "sentence_length_variance": report.sentence_length_variance,
            "violations": [v.to_dict() for v in report.violations],
            "top_fixes": report.top_fixes,
        }

    def list_content_presets(self) -> List[Dict[str, Any]]:
        """Lists all supported content presets and their defaults."""
        presets = []
        for ctype, conf in CONTENT_PRESETS.items():
            tone = conf["default_tone"]
            presets.append({
                "type": ctype.value,
                "title": conf["title"],
                "description": conf["description"],
                "default_tone": tone.to_dict(),
            })
        return presets

    # ---------------------------------------------------------------------------
    # Context-Aware Visual Asset Studio Service Methods
    # ---------------------------------------------------------------------------

    def generate_visual(self, payload: VisualGenerateRequest) -> Dict[str, Any]:
        """Generates a visual asset from explicit prompt specifications."""
        studio = VisualStudio()
        spec = VisualPromptSpec(
            title=payload.title,
            subtitle=payload.subtitle,
            asset_type=AssetType(payload.asset_type),
            theme=VisualTheme(payload.theme),
            aspect_ratio=AspectRatio(payload.aspect_ratio),
            high_res=payload.high_res,
            offline=payload.offline,
        )
        res = studio.create_asset(spec)
        return res.to_dict()

    def illustrate_text(self, payload: VisualIllustrateRequest) -> Dict[str, Any]:
        """Semantically analyzes input text and renders matching visual asset."""
        studio = VisualStudio()
        theme_enum = VisualTheme(payload.theme) if payload.theme else None
        ratio_enum = AspectRatio(payload.aspect_ratio) if payload.aspect_ratio else None
        res = studio.illustrate_text(
            text_content=payload.text,
            asset_type=AssetType(payload.asset_type),
            theme=theme_enum,
            aspect_ratio=ratio_enum,
            offline=payload.offline,
        )
        return res.to_dict()

    def list_visual_assets(self) -> List[Dict[str, Any]]:
        """Returns catalog of all saved visual assets."""
        studio = VisualStudio()
        return studio.list_assets()



