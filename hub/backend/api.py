"""
FastAPI REST API router for DarkHub.
"""

import asyncio
import os
import secrets
from typing import Any, Dict, List, Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from hub.backend.webhooks import (
    CloudGatewayStatus,
    DokployDeployTrigger,
    WebhookEventRecord,
)

from hub.backend.models import (
    ExportCatalogResponse,
    GenerateLearningPackRequest,
    GenerateLearningPackResponse,
    HealthCheckResult,
    ImportCatalogRequest,
    ImportCatalogResponse,
    LearningPackSummary,
    OllamaGenerateRequest,
    OllamaGenerateResponse,
    OllamaStatusResponse,
    OpenRouterStatusResponse,
    PromptTemplate,
    ServiceCategory,
    ServiceCreate,
    ServiceItem,
    ServiceLaunchResponse,
    ServiceUpdate,
    UnifiedGenerateRequest,
    UnifiedGenerateResponse,
    BenchmarkDomainSummary,
    RouteTaskBenchmarkRequest,
    RouteTaskBenchmarkResponse,
    ContentGenerateRequest,
    ContentGenerateResponse,
    ContentLintRequest,
    ContentLintResponse,
    VisualGenerateRequest,
    VisualGenerateResponse,
    VisualIllustrateRequest,
    TaskDashboardReport,
    UsageSyncPayload,
    UsageSyncResponse,
)
from hub.backend.service import HubService
from core.usage.models import AccountUsageReport, ModelCallEvent, ModelUsageReport
from core.usage.api_credits import (
    ApiCreditsReport,
    CreditAccountUpdateRequest,
    ProviderCreditCard,
)
from core.roadmap.models import (
    ConfidenceLevel,
    DeliveryStatus,
    LifecycleStage,
    PlanningHorizon,
    RoadmapHealth,
    RoadmapItem,
    RoadmapItemType,
    RoadmapProjectSummary,
    RoadmapSnapshot,
    RoadmapSnapshotComparison,
    RoadmapSnapshotHistory,
    RoadmapSourceDocument,
)
from core.roadmap.store import RoadmapUnavailableError
from core.demands.models import (
    DemandInput,
    DemandSpecificationGuidance,
    GrillAnswersPayload,
    GrillRefinementResult,
    GrillSession,
    UserTicket,
)
from core.harness.test_subagent import (
    DistilledTestReport,
    TestExecutionInstruction,
)
from core.infra.cards import InfraCard, InfraCardsReport

router = APIRouter(prefix="/api", tags=["DarkHub API"])
roadmap_router = APIRouter(prefix="/projects", tags=["Operational Roadmap"])

# Dependency Injection for HubService singleton
_service_instance: Optional[HubService] = None


def get_hub_service() -> HubService:
    global _service_instance
    if _service_instance is None:
        _service_instance = HubService()
    return _service_instance


def _roadmap_project_or_404(service: HubService, project_id: str) -> None:
    if not any(project.id == project_id for project in service.list_roadmap_projects()):
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")


@roadmap_router.get("", response_model=List[RoadmapProjectSummary])
def list_roadmap_projects(service: HubService = Depends(get_hub_service)) -> List[RoadmapProjectSummary]:
    """List project scopes available to the isolated roadmap projection."""

    return service.list_roadmap_projects()


@roadmap_router.get("/{project_id}/roadmap", response_model=RoadmapSnapshot)
def get_project_roadmap(
    project_id: str,
    search: Optional[str] = None,
    item_type: Optional[RoadmapItemType] = None,
    lifecycle_stage: Optional[LifecycleStage] = None,
    delivery_status: Optional[DeliveryStatus] = None,
    horizon: Optional[PlanningHorizon] = None,
    confidence: Optional[ConfidenceLevel] = None,
    source_id: Optional[str] = None,
    service: HubService = Depends(get_hub_service),
) -> RoadmapSnapshot:
    """Return the selected project's current operational roadmap snapshot."""

    _roadmap_project_or_404(service, project_id)
    try:
        return service.get_roadmap(
            project_id,
            search=search,
            item_type=item_type.value if item_type else None,
            lifecycle_stage=lifecycle_stage.value if lifecycle_stage else None,
            delivery_status=delivery_status.value if delivery_status else None,
            horizon=horizon.value if horizon else None,
            confidence=confidence.value if confidence else None,
            source_id=source_id,
        )
    except RoadmapUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@roadmap_router.get("/{project_id}/roadmap/items/{item_id}", response_model=RoadmapItem)
def get_project_roadmap_item(
    project_id: str,
    item_id: str,
    service: HubService = Depends(get_hub_service),
) -> RoadmapItem:
    """Return one item and its evidence/provenance drawer payload."""

    _roadmap_project_or_404(service, project_id)
    item = service.get_roadmap_item(project_id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Roadmap item '{item_id}' not found")
    return item


@roadmap_router.get("/{project_id}/roadmap/health", response_model=RoadmapHealth)
def get_project_roadmap_health(
    project_id: str,
    service: HubService = Depends(get_hub_service),
) -> RoadmapHealth:
    """Return snapshot health, source availability and consistency counts."""

    _roadmap_project_or_404(service, project_id)
    try:
        return service.get_roadmap_health(project_id)
    except RoadmapUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@roadmap_router.get(
    "/{project_id}/roadmap/history",
    response_model=RoadmapSnapshotHistory,
)
def get_project_roadmap_history(
    project_id: str,
    limit: Optional[int] = Query(default=None, ge=1, le=50),
    service: HubService = Depends(get_hub_service),
) -> RoadmapSnapshotHistory:
    """Return bounded metadata for the selected project's retained snapshots."""

    _roadmap_project_or_404(service, project_id)
    try:
        return service.roadmap.get_history(project_id, limit=limit)
    except RoadmapUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@roadmap_router.get(
    "/{project_id}/roadmap/history/compare",
    response_model=RoadmapSnapshotComparison,
)
def compare_project_roadmap_snapshots(
    project_id: str,
    from_snapshot: str = Query(..., min_length=1),
    to_snapshot: str = Query(..., min_length=1),
    service: HubService = Depends(get_hub_service),
) -> RoadmapSnapshotComparison:
    """Compare two retained snapshots without changing roadmap state."""

    _roadmap_project_or_404(service, project_id)
    try:
        return service.roadmap.compare_snapshots(project_id, from_snapshot, to_snapshot)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RoadmapUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@roadmap_router.get(
    "/{project_id}/roadmap/sources/{source_id}",
    response_model=RoadmapSourceDocument,
)
def get_project_roadmap_source(
    project_id: str,
    source_id: str,
    service: HubService = Depends(get_hub_service),
) -> RoadmapSourceDocument:
    """Expose only the selected project's known source document for provenance links."""

    _roadmap_project_or_404(service, project_id)
    document = service.get_roadmap_source(project_id, source_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"Roadmap source '{source_id}' not found")
    return document


@router.get("/services", response_model=List[ServiceItem])
def list_services(
    category: Optional[ServiceCategory] = None,
    search: Optional[str] = None,
    favorites_only: bool = False,
    pinned_only: bool = False,
    service: HubService = Depends(get_hub_service),
) -> List[ServiceItem]:
    """List services with optional category, query search, or favorite filters."""
    return service.list_services(
        category=category,
        search=search,
        favorites_only=favorites_only,
        pinned_only=pinned_only,
    )


@router.post("/services", response_model=ServiceItem, status_code=status.HTTP_201_CREATED)
def create_service(
    payload: ServiceCreate,
    service: HubService = Depends(get_hub_service),
) -> ServiceItem:
    """Create a new service link entry."""
    return service.create_service(payload)


@router.get("/services/export", response_model=ExportCatalogResponse)
def export_services_catalog(
    service: HubService = Depends(get_hub_service),
) -> ExportCatalogResponse:
    """Export complete services catalog as JSON backup."""
    return service.export_services_data()


@router.post("/services/import", response_model=ImportCatalogResponse)
def import_services_catalog(
    payload: ImportCatalogRequest,
    service: HubService = Depends(get_hub_service),
) -> ImportCatalogResponse:
    """Import and restore services catalog from JSON."""
    try:
        return service.import_services_data(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha ao importar catálogo: {exc}")


@router.post("/services/reset-defaults")
def reset_services_defaults(
    service: HubService = Depends(get_hub_service),
) -> dict:
    """Reset catalog to original factory defaults."""
    try:
        count = service.reset_to_defaults()
        return {"message": "Catálogo restaurado com sucesso para os padrões originais!", "count": count}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/services/{service_id}", response_model=ServiceItem)
def get_service(
    service_id: str,
    service: HubService = Depends(get_hub_service),
) -> ServiceItem:
    """Get single service by ID."""
    item = service.get_service(service_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")
    return item


@router.put("/services/{service_id}", response_model=ServiceItem)
def update_service(
    service_id: str,
    payload: ServiceUpdate,
    service: HubService = Depends(get_hub_service),
) -> ServiceItem:
    """Update existing service entry."""
    updated = service.update_service(service_id, payload)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")
    return updated


@router.delete("/services/{service_id}", status_code=status.HTTP_200_OK)
def delete_service(
    service_id: str,
    service: HubService = Depends(get_hub_service),
) -> dict:
    """Delete a service entry."""
    success = service.delete_service(service_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")
    return {"message": f"Service '{service_id}' successfully deleted", "id": service_id}


@router.post("/services/{service_id}/toggle-favorite", response_model=ServiceItem)
def toggle_favorite(
    service_id: str,
    service: HubService = Depends(get_hub_service),
) -> ServiceItem:
    """Toggle favorite status for a service."""
    updated = service.toggle_favorite(service_id)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")
    return updated


@router.post("/services/{service_id}/toggle-pin", response_model=ServiceItem)
def toggle_pin(
    service_id: str,
    service: HubService = Depends(get_hub_service),
) -> ServiceItem:
    """Toggle pinned status for quick dock."""
    updated = service.toggle_pin(service_id)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' not found")
    return updated


@router.post("/services/{service_id}/launch", response_model=ServiceLaunchResponse)
def launch_service_endpoint(
    service_id: str,
    timeout: float = Query(default=5.0, ge=1.0, le=30.0, description="Max seconds to wait for service readiness"),
    service: HubService = Depends(get_hub_service),
) -> ServiceLaunchResponse:
    """Launch a configured local service in background if offline, waiting for health confirmation."""
    try:
        return service.launch_service(service_id=service_id, max_wait_sec=timeout)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to launch service: {exc}") from exc


@router.get("/services/{service_id}/open")
def open_service_endpoint(
    service_id: str,
    timeout: float = Query(default=5.0, ge=1.0, le=30.0, description="Max seconds to wait for service readiness"),
    service: HubService = Depends(get_hub_service),
) -> RedirectResponse:
    """
    Launch local service if offline, wait for readiness, and redirect browser to the target service URL.
    For non-local or non-launchable services, redirects immediately to service URL.
    """
    try:
        target_url = service.get_service_launch_target(service_id=service_id, max_wait_sec=timeout)
        return RedirectResponse(url=target_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to open service: {exc}") from exc


class SessionInfoResponse(BaseModel):
    session_token: str
    is_active: bool = True


@router.get("/session", response_model=SessionInfoResponse)
def get_session_info(
    service: HubService = Depends(get_hub_service),
) -> SessionInfoResponse:
    """Return active local session token for client authentication (DF-08)."""
    return SessionInfoResponse(session_token=service.session_token)


@router.get("/health/ping", response_model=HealthCheckResult)
def ping_service(
    service_id: str = Query(..., description="ID of the service"),
    url: Optional[str] = Query(default=None, description="Optional URL to verify against registered service"),
    service: HubService = Depends(get_hub_service),
) -> HealthCheckResult:
    """
    Ping a registered service URL to check availability and latency (DF-08).
    Strictly requires a registered service ID. Rejects arbitrary SSRF targets.
    """
    registered = service.get_service(service_id)
    if not registered:
        raise HTTPException(
            status_code=404,
            detail=f"Service '{service_id}' not found in registered catalog",
        )

    if url and url.strip().rstrip("/") != registered.url.strip().rstrip("/"):
        raise HTTPException(
            status_code=400,
            detail="Provided URL does not match registered service URL",
        )

    return service.ping_url(service_id=service_id, url=registered.url)


@router.get("/health/ping-all", response_model=List[HealthCheckResult])
async def ping_all_services(
    service: HubService = Depends(get_hub_service),
) -> List[HealthCheckResult]:
    """Ping all registered services concurrently."""
    services = service.list_services()
    loop = asyncio.get_event_loop()

    tasks = [
        loop.run_in_executor(None, service.ping_url, s.id, s.url, 2.0)
        for s in services
    ]
    results = await asyncio.gather(*tasks)
    return results


@router.get("/ollama/status", response_model=OllamaStatusResponse)
def get_ollama_status(
    service: HubService = Depends(get_hub_service),
) -> OllamaStatusResponse:
    """Check local Ollama cluster status and available models."""
    return service.get_ollama_status()


@router.post("/ollama/generate", response_model=OllamaGenerateResponse)
def generate_with_ollama(
    payload: OllamaGenerateRequest,
    service: HubService = Depends(get_hub_service),
) -> OllamaGenerateResponse:
    """Run interactive prompt directly against local Ollama model."""
    try:
        return service.generate_ollama(payload)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/prompts", response_model=List[PromptTemplate])
def list_prompts(
    search: Optional[str] = None,
    service: HubService = Depends(get_hub_service),
) -> List[PromptTemplate]:
    """List available prompt templates for developers."""
    return service.list_prompts(search=search)


@router.get("/benchmarks/latest")
def get_latest_benchmarks(
    service: HubService = Depends(get_hub_service),
) -> dict:
    """Get the latest daily LLM model benchmark ledger."""
    return service.get_latest_benchmark_ledger()


@router.get("/benchmarks/frontier")
def get_benchmark_frontier(
    service: HubService = Depends(get_hub_service),
) -> dict:
    """Get current Coding and Intelligence Pareto Efficiency Frontiers."""
    return service.get_benchmark_frontier()


@router.post("/benchmarks/refresh")
def refresh_benchmarks(
    service: HubService = Depends(get_hub_service),
) -> dict:
    """Force fresh daily benchmark evaluation."""
    return service.refresh_benchmarks()


@router.get("/benchmarks/proximity")
def get_benchmark_proximity(
    service: HubService = Depends(get_hub_service),
) -> dict:
    """Get Frontier Proximity Index (FPI), epsilon gaps and near-Pareto challenger status."""
    return service.get_benchmark_proximity()


@router.get("/benchmarks/speculative/top3")
def get_speculative_top3(
    tier: str = "high",
    service: HubService = Depends(get_hub_service),
) -> list:
    """Get the top-3 candidate models for speculative cascade in a given tier."""
    return service.get_top_candidates_for_tier(tier=tier, k=3)


@router.get("/benchmarks/empirical")
def get_empirical_benchmarks(
    service: HubService = Depends(get_hub_service),
) -> dict:
    """Get empirical Dark Factory benchmark stats, pass rates, and Elo leaderboard."""
    return service.get_empirical_ledger()


@router.post("/benchmarks/speculative/race")
def run_speculative_race(
    task_id: str = "web_race",
    prompt: str = "Test prompt",
    complexity: str = "high",
    offline: bool = False,
    service: HubService = Depends(get_hub_service),
) -> dict:
    """Execute a speculative cascade race across candidate models."""
    return service.run_speculative_race(task_id=task_id, prompt=prompt, complexity=complexity, offline=offline)


@router.get("/openrouter/status", response_model=OpenRouterStatusResponse)
def get_openrouter_status(
    service: HubService = Depends(get_hub_service),
) -> OpenRouterStatusResponse:
    """Check OpenRouter API key status, tier, usage, and available frontier models."""
    return service.get_openrouter_status()


@router.post("/playground/generate", response_model=UnifiedGenerateResponse)
def generate_unified_playground(
    payload: UnifiedGenerateRequest,
    service: HubService = Depends(get_hub_service),
) -> UnifiedGenerateResponse:
    """Run interactive prompt against Ollama (local) or OpenRouter (cloud)."""
    try:
        return service.generate_unified(payload)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# -------------------------------------------------------------
# Learning Packs & Cognitive Uplift Endpoints
# -------------------------------------------------------------
@router.get("/learning-packs", response_model=List[LearningPackSummary])
def list_learning_packs(
    service: HubService = Depends(get_hub_service),
) -> List[dict]:
    """List metadata summaries for all recorded session learning packs."""
    return service.list_learning_packs()


@router.get("/learning-packs/latest")
def get_latest_learning_pack(
    service: HubService = Depends(get_hub_service),
) -> dict:
    """Get the latest full session learning pack."""
    pack = service.get_learning_pack("latest")
    if not pack:
        raise HTTPException(status_code=404, detail="No learning packs found yet")
    return pack


@router.get("/learning-packs/{pack_id}")
def get_learning_pack(
    pack_id: str,
    service: HubService = Depends(get_hub_service),
) -> dict:
    """Get a specific session learning pack by ID."""
    pack = service.get_learning_pack(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail=f"Learning pack '{pack_id}' not found")
    return pack


@router.post("/learning-packs/generate", response_model=GenerateLearningPackResponse)
def generate_learning_pack(
    payload: GenerateLearningPackRequest,
    service: HubService = Depends(get_hub_service),
) -> dict:
    """Generate and store a new session learning pack from current codebase state."""
    try:
        return service.generate_learning_pack(
            title=payload.title,
            session_id=payload.session_id,
            files=payload.files,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/learning-packs/{pack_id}/export-anki")
def export_learning_pack_anki(
    pack_id: str,
    service: HubService = Depends(get_hub_service),
) -> Response:
    """Export learning pack flashcards as Anki TSV."""
    tsv_content = service.export_learning_pack_anki(pack_id)
    if tsv_content is None:
        raise HTTPException(status_code=404, detail=f"Learning pack '{pack_id}' not found")
    return Response(
        content=tsv_content,
        media_type="text/tab-separated-values",
        headers={"Content-Disposition": f"attachment; filename={pack_id}_anki.tsv"},
    )


@router.get("/learning-packs/{pack_id}/html")
def get_learning_pack_html(
    pack_id: str,
    service: HubService = Depends(get_hub_service),
) -> Response:
    """Render and return the standalone interactive HTML widget."""
    html_content = service.get_learning_pack_html(pack_id)
    if html_content is None:
        raise HTTPException(status_code=404, detail=f"Learning pack '{pack_id}' not found")
    return Response(content=html_content, media_type="text/html")


# ---------------------------------------------------------------------------
# Benchmark & Multi-Domain Routing Endpoints
# ---------------------------------------------------------------------------

@router.get("/benchmarks/domains", response_model=List[BenchmarkDomainSummary])
def list_benchmark_domains(
    service: HubService = Depends(get_hub_service),
) -> List[dict]:
    """List all supported canonical benchmark domains with their metadata."""
    return service.get_benchmark_domains()


@router.get("/benchmarks/domains/{domain}/frontier")
def get_domain_frontier(
    domain: str,
    service: HubService = Depends(get_hub_service),
) -> dict:
    """Get the Pareto efficiency frontier models for a specific benchmark domain."""
    frontier = service.get_domain_frontier(domain)
    if not frontier:
        raise HTTPException(
            status_code=404,
            detail=f"Domain '{domain}' not found. Valid domains: coding, deep_research, legal_contract, business_automation, formal_reasoning, multimodal_audio, image_gen, video_gen"
        )
    return frontier


@router.post("/benchmarks/route-task", response_model=RouteTaskBenchmarkResponse)
def route_task_benchmark(
    payload: RouteTaskBenchmarkRequest,
    service: HubService = Depends(get_hub_service),
) -> dict:
    """Analyze a task requirement, classify domain intent, and return optimal frontier models and speculative Top-3 candidates."""
    try:
        return service.route_task_benchmark(
            requirement=payload.requirement,
            complexity=payload.complexity,
            offline=payload.offline,
            domain_override=payload.domain_override,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Anti-AI-Slop Content Engine Endpoints
# ---------------------------------------------------------------------------

@router.post("/content/generate", response_model=ContentGenerateResponse)
def generate_content_endpoint(
    payload: ContentGenerateRequest,
    service: HubService = Depends(get_hub_service),
) -> dict:
    """Generate high-signal, anti-slop content matching specified persona and format."""
    try:
        return service.generate_content(payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/content/lint", response_model=ContentLintResponse)
def lint_content_endpoint(
    payload: ContentLintRequest,
    service: HubService = Depends(get_hub_service),
) -> dict:
    """Audit text for AI slop, clichés, and cadence monotony."""
    try:
        return service.lint_content(payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/content/presets")
def list_content_presets_endpoint(
    service: HubService = Depends(get_hub_service),
) -> list:
    """List available content format presets and their default tone specifications."""
    return service.list_content_presets()


# ---------------------------------------------------------------------------
# Context-Aware Visual Asset Studio Endpoints
# ---------------------------------------------------------------------------

@router.post("/visual/generate", response_model=VisualGenerateResponse)
def generate_visual_endpoint(
    payload: VisualGenerateRequest,
    service: HubService = Depends(get_hub_service),
) -> dict:
    """Generate a visual asset from explicit prompt specifications."""
    try:
        return service.generate_visual(payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/visual/illustrate", response_model=VisualGenerateResponse)
def illustrate_text_endpoint(
    payload: VisualIllustrateRequest,
    service: HubService = Depends(get_hub_service),
) -> dict:
    """Semantically analyze input text and render a contextually coupled visual asset."""
    try:
        return service.illustrate_text(payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/usage/accounts", response_model=AccountUsageReport)
def get_account_usage_endpoint(
    refresh: bool = Query(default=False),
    service: HubService = Depends(get_hub_service),
) -> AccountUsageReport:
    """Return all provider states; disconnected providers are normal rows, never errors."""
    return service.get_account_usage(force=refresh)


@router.post("/usage/accounts/refresh", response_model=AccountUsageReport)
def refresh_account_usage_endpoint(
    service: HubService = Depends(get_hub_service),
) -> AccountUsageReport:
    """Force provider probes while preserving partial-success semantics."""
    return service.get_account_usage(force=True)


@router.get("/usage/models", response_model=ModelUsageReport)
def get_model_usage_endpoint(
    service: HubService = Depends(get_hub_service),
) -> ModelUsageReport:
    """Return aggregate and recent model usage for this project."""
    return service.get_model_usage()


@router.post("/usage/models/events", response_model=ModelUsageReport)
def record_model_usage_endpoint(
    payload: ModelCallEvent,
    x_darkfac_telemetry_key: Optional[str] = Header(default=None),
    service: HubService = Depends(get_hub_service),
) -> ModelUsageReport:
    """Ingest a sanitized event from an authenticated external harness."""
    configured_key = os.environ.get("DARKFAC_TELEMETRY_KEY")
    if not configured_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="HTTP telemetry ingestion is disabled; set DARKFAC_TELEMETRY_KEY or use the local CLI.",
        )
    if not x_darkfac_telemetry_key or not secrets.compare_digest(
        x_darkfac_telemetry_key,
        configured_key,
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid telemetry key.")
    return service.record_model_usage(payload)


@router.post("/usage/sync", response_model=UsageSyncResponse)
def sync_usage_endpoint(
    payload: UsageSyncPayload,
    x_darkfac_telemetry_key: Optional[str] = Header(default=None, alias="X-DarkFac-Telemetry-Key"),
    service: HubService = Depends(get_hub_service),
) -> UsageSyncResponse:
    """Synchronizes account quota and credit snapshots from an authenticated local workstation or worker node."""
    configured_key = os.environ.get("DARKFAC_TELEMETRY_KEY")
    if not configured_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Usage synchronization is disabled; set DARKFAC_TELEMETRY_KEY.",
        )
    if not x_darkfac_telemetry_key or not secrets.compare_digest(
        x_darkfac_telemetry_key,
        configured_key,
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid telemetry key.")
    return service.sync_usage_data(payload)


@router.get("/visual/gallery")
def list_visual_gallery_endpoint(
    service: HubService = Depends(get_hub_service),
) -> list:
    """List all previously generated visual assets and their metadata."""
    return service.list_visual_assets()


# ==============================================================================
# API Credits & Billing Monitor ($) Endpoints
# ==============================================================================


@router.get("/credits", response_model=ApiCreditsReport)
def get_api_credits_endpoint(
    refresh: bool = Query(default=False, description="Force fresh live probes"),
    service: HubService = Depends(get_hub_service),
) -> ApiCreditsReport:
    """Returns API credit balances and monthly expenditures in USD ($)."""
    return service.get_api_credits_report(force=refresh)


@router.post("/credits/refresh", response_model=ApiCreditsReport)
def refresh_api_credits_endpoint(
    service: HubService = Depends(get_hub_service),
) -> ApiCreditsReport:
    """Forces live external refresh of API credit balances and expenditures ($)."""
    return service.refresh_api_credits_report()


@router.post("/credits/accounts/{provider_id}", response_model=ProviderCreditCard)
def update_api_credit_account_endpoint(
    provider_id: str,
    payload: CreditAccountUpdateRequest,
    service: HubService = Depends(get_hub_service),
) -> ProviderCreditCard:
    """Updates manual/synchronized credit values for a specific provider."""
    return service.update_credit_account(provider_id, payload)


# ==============================================================================
# User Demands & Backlog Endpoints
# ==============================================================================


@router.post("/demands/guide", response_model=DemandSpecificationGuidance)
def guide_user_demand(
    payload: DemandInput,
    force_heuristic: bool = Query(default=False, description="Force deterministic script guidance ($0)"),
    timeout: Optional[float] = Query(default=None, description="Timeout in seconds for local model guidance"),
    service: HubService = Depends(get_hub_service),
) -> DemandSpecificationGuidance:
    """Analyze, refine, and structure a user demand using local model or script ($0.00)."""
    return service.guide_demand(payload, force_heuristic=force_heuristic, timeout=timeout)


@router.get("/demands/next-id")
def get_next_demand_id(
    project_id: str = Query(default="darkfac", description="Project identifier"),
    service: HubService = Depends(get_hub_service),
) -> dict[str, str]:
    """Get the next sequential ticket ID for a project."""
    next_id = service.get_next_ticket_id(project_id)
    return {"project_id": project_id, "next_id": next_id}


@router.post("/demands/tickets", response_model=UserTicket, status_code=status.HTTP_201_CREATED)
def create_demand_ticket(
    payload: UserTicket,
    service: HubService = Depends(get_hub_service),
) -> UserTicket:
    """Insert a specified user demand ticket into the development backlog."""
    return service.create_demand_ticket(payload)


@router.get("/demands/tickets", response_model=List[UserTicket])
def list_demand_tickets(
    project_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    service: HubService = Depends(get_hub_service),
) -> List[UserTicket]:
    """List user demand tickets in the backlog."""
    return service.list_demand_tickets(project_id=project_id, status=status)


@router.get("/demands/tickets/{ticket_id}", response_model=UserTicket)
def get_demand_ticket(
    ticket_id: str,
    service: HubService = Depends(get_hub_service),
) -> UserTicket:
    """Retrieve details for a single demand ticket."""
    ticket = service.get_demand_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail=f"Demand ticket '{ticket_id}' not found")
    return ticket


@router.patch("/demands/tickets/{ticket_id}/status", response_model=UserTicket)
def update_demand_ticket_status(
    ticket_id: str,
    status_value: DeliveryStatus = Query(..., alias="status"),
    notes: Optional[str] = Query(default=None),
    service: HubService = Depends(get_hub_service),
) -> UserTicket:
    """Update status of a demand ticket in the backlog."""
    try:
        return service.update_demand_ticket_status(ticket_id, status_value, notes=notes)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/demands/tickets/{ticket_id}/grill", response_model=GrillSession)
def start_demand_grill(
    ticket_id: str,
    force_heuristic: bool = Query(default=False, description="Force deterministic script questions ($0)"),
    timeout: Optional[float] = Query(default=None, description="Timeout in seconds"),
    service: HubService = Depends(get_hub_service),
) -> GrillSession:
    """Start an interactive or automated Q&A Grill session to clarify a demand ticket."""
    try:
        return service.start_demand_grill(ticket_id, force_heuristic=force_heuristic, timeout=timeout)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/demands/tickets/{ticket_id}/grill/submit", response_model=GrillRefinementResult)
def submit_demand_grill(
    ticket_id: str,
    payload: GrillAnswersPayload,
    service: HubService = Depends(get_hub_service),
) -> GrillRefinementResult:
    """Submit answers from a grill session and refine the ticket in the backlog."""
    try:
        return service.submit_demand_grill(ticket_id, answers=payload.answers)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/harness/run-tests", response_model=DistilledTestReport)
def run_tests(
    instruction: TestExecutionInstruction,
    service: HubService = Depends(get_hub_service),
) -> DistilledTestReport:
    """Execute test suite via headless test subagent engine and return distilled report."""
    return service.run_tests(instruction)


# ==============================================================================
# Task Dashboard (DF-21)
# ==============================================================================


@router.get("/tasks/dashboard", response_model=TaskDashboardReport)
@router.get("/tasks/dashboard/", response_model=TaskDashboardReport, include_in_schema=False)
@router.get("/tasks", response_model=TaskDashboardReport, include_in_schema=False)
@router.get("/tasks/", response_model=TaskDashboardReport, include_in_schema=False)
def get_task_dashboard(service: HubService = Depends(get_hub_service)) -> TaskDashboardReport:
    """Return the read-only queue/run/stage/cost/evidence projection for DarkHub."""
    return service.get_task_dashboard()


# ==============================================================================
# Infrastructure Nodes & Topology Cards Endpoints (USR-15)
# ==============================================================================


@router.get("/infra/cards", response_model=InfraCardsReport)
def get_infra_cards_endpoint(
    probe: bool = Query(default=False, description="Perform live network probe"),
    timeout: float = Query(default=0.5, description="Probe timeout in seconds"),
    service: HubService = Depends(get_hub_service),
) -> InfraCardsReport:
    """Returns infrastructure nodes and system links as UI-ready cards."""
    return service.get_infra_cards_report(probe_liveness=probe, probe_timeout=timeout)


@router.post("/infra/cards/refresh", response_model=InfraCardsReport)
def refresh_infra_cards_endpoint(
    timeout: float = Query(default=1.0, description="Probe timeout in seconds"),
    service: HubService = Depends(get_hub_service),
) -> InfraCardsReport:
    """Forces live connectivity probes and returns refreshed infrastructure cards."""
    return service.get_infra_cards_report(probe_liveness=True, probe_timeout=timeout)


@router.get("/infra/cards/{node_id}", response_model=InfraCard)
def get_infra_card_endpoint(
    node_id: str,
    probe: bool = Query(default=False, description="Perform live network probe"),
    timeout: float = Query(default=0.5, description="Probe timeout in seconds"),
    service: HubService = Depends(get_hub_service),
) -> InfraCard:
    """Returns a single infrastructure card by node ID."""
    card = service.get_infra_card(node_id, probe_liveness=probe, probe_timeout=timeout)
    if not card:
        raise HTTPException(status_code=404, detail=f"Infrastructure node '{node_id}' not found")
    return card


# ==============================================================================
# Harness Test Subagent & Dedicated Worker Endpoints (USR-16)
# ==============================================================================


@router.get("/harness/workers", response_model=List[dict])
def list_test_workers(
    service: HubService = Depends(get_hub_service),
) -> List[dict]:
    """Returns real-time status, latency, and capabilities of available test worker nodes."""
    return service.get_test_workers_status()


@router.post("/harness/execute", response_model=DistilledTestReport)
def execute_test_suite(
    instruction: TestExecutionInstruction,
    service: HubService = Depends(get_hub_service),
) -> DistilledTestReport:
    """Executes a headless test suite across remote worker or local fallback."""
    return service.execute_test_run(instruction)


# ==============================================================================
# Cloud Gateway & Autonomous Webhooks Endpoints (USR-18 / INFRA-09 / DF-20)
# ==============================================================================


@router.post("/webhooks/github", response_model=WebhookEventRecord)
async def handle_github_webhook(
    request: Request,
    service: HubService = Depends(get_hub_service),
    x_github_event: Optional[str] = Header(None, alias="X-GitHub-Event"),
    x_github_delivery: Optional[str] = Header(None, alias="X-GitHub-Delivery"),
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
) -> WebhookEventRecord:
    """Receives, verifies, deduplicates, and processes incoming GitHub webhooks.

    Fail-closed security: rejects missing event or delivery headers, and enforces
    HMAC-SHA256 signature verification when GITHUB_WEBHOOK_SECRET is configured.
    """
    if not x_github_event or not x_github_delivery:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required GitHub webhook headers (X-GitHub-Event and X-GitHub-Delivery).",
        )

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON payload: {exc}",
        )

    record = service.process_github_webhook(
        event_type=x_github_event,
        delivery_id=x_github_delivery,
        payload=payload,
        signature_header=x_hub_signature_256,
    )

    if record.status == "rejected":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=record.details.get("reason", "Webhook signature verification failed."),
        )

    return record


@router.get("/webhooks/events", response_model=List[WebhookEventRecord])
def list_webhook_events(
    limit: int = Query(50, ge=1, le=200),
    event_type: Optional[str] = Query(None),
    service: HubService = Depends(get_hub_service),
) -> List[WebhookEventRecord]:
    """Returns the historical audit trail of received webhook events."""
    return service.get_webhook_events(limit=limit, event_type=event_type)


@router.post("/webhooks/test", response_model=WebhookEventRecord)
def simulate_webhook_event(
    event: Dict[str, Any],
    service: HubService = Depends(get_hub_service),
) -> WebhookEventRecord:
    """Simulates an incoming webhook event for local testing and deterministic validation."""
    event_type = event.get("event_type", "ping")
    delivery_id = event.get("delivery_id") or f"test-sim-{secrets.token_hex(6)}"
    payload = event.get("payload", {})

    return service.process_github_webhook(
        event_type=event_type,
        delivery_id=delivery_id,
        payload=payload,
        signature_header=None,
        require_secret=False,
    )


@router.get("/cloud/status", response_model=CloudGatewayStatus)
def get_cloud_gateway_status(
    service: HubService = Depends(get_hub_service),
) -> CloudGatewayStatus:
    """Returns the runtime status, allowed hosts, and webhook statistics of the Cloud Gateway."""
    return service.get_cloud_gateway_status()


@router.post("/cloud/deploy", response_model=DokployDeployTrigger)
def trigger_cloud_deploy(
    service: HubService = Depends(get_hub_service),
    body: Optional[Dict[str, Any]] = None,
) -> DokployDeployTrigger:
    """Triggers Dokploy PaaS auto-deployment webhook for DarkHub or target service (INFRA-09)."""
    service_name = (body or {}).get("service_name", "darkhub")
    custom_url = (body or {}).get("deploy_url")
    return service.trigger_dokploy_deployment(service_name=service_name, custom_url=custom_url)


# ==============================================================================
# Telegram Gateway & n8n Community Endpoints (HF-14)
# ==============================================================================


@router.post("/webhooks/telegram")
async def handle_telegram_webhook(
    request: Request,
    service: HubService = Depends(get_hub_service),
    secret_token: Optional[str] = Header(None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> Dict[str, Any]:
    """Receives and processes incoming Telegram Bot Webhook updates (HF-14)."""
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON payload: {exc}",
        )

    result = service.process_telegram_webhook(payload=payload, secret_token_header=secret_token)
    if not result.get("authorized") and result.get("error"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=result["error"],
        )
    return result


@router.get("/integrations/telegram/status")
def get_telegram_status_endpoint(
    service: HubService = Depends(get_hub_service),
) -> Dict[str, Any]:
    """Returns the operational status of the Telegram Gateway (HF-14)."""
    return service.get_telegram_gateway_status()


@router.get("/integrations/n8n/status")
def get_n8n_status_endpoint(
    url: Optional[str] = Query(None, description="n8n instance URL to probe"),
    service: HubService = Depends(get_hub_service),
) -> Dict[str, Any]:
    """Probes n8n endpoint and returns verification report (HF-14)."""
    return service.get_n8n_status(target_url=url)





