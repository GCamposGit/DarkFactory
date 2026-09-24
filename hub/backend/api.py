"""
FastAPI REST API router for DarkHub.
"""

import asyncio
import os
import secrets
from typing import Any, Dict, List, Optional
from pydantic import BaseModel
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from hub.backend.webhooks import (
    CloudGatewayStatus,
    DokployDeployTrigger,
    WebhookEventRecord,
)
from hub.backend.coverage import CoverageSummaryResponse, get_coverage_summary

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
    ProgressProjection,
    PortfolioOverviewResponse,
    PortfolioProjectDetailResponse,
)
from hub.backend.service import HubService
from core.portfolio.models import PortfolioEfficiencyReport
from core.usage.models import AccountUsageReport, ModelCallEvent, ModelUsageReport
from core.telemetry.models import (
    ExecutionMode,
    TelemetryFilters,
    TelemetryQueryResult,
    TelemetryRecord,
    TelemetryRecordCreate,
    TelemetryStats,
)
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


def require_owner_session(
    x_hub_session: Optional[str] = Header(default=None, alias="X-Hub-Session"),
    service: HubService = Depends(get_hub_service),
) -> HubService:
    """Require the active local owner session for state-changing integrations."""
    if not service.validate_session(x_hub_session):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return service


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


@roadmap_router.get("/{project_id}/roadmap/progress", response_model=ProgressProjection)
def get_project_roadmap_progress(
    project_id: str,
    service: HubService = Depends(get_hub_service),
) -> ProgressProjection:
    """Return the selected project's continuous progress and stagnation projection (HF-13-02)."""
    return service.get_progress_projection(project_id)


# ---------------------------------------------------------------------------
# Multi-Project Portfolio Endpoints (DH-08)
# ---------------------------------------------------------------------------


@router.get("/portfolio", response_model=PortfolioOverviewResponse)
def get_portfolio_overview_endpoint(
    service: HubService = Depends(get_hub_service),
) -> PortfolioOverviewResponse:
    """Return consolidated multi-project portfolio overview across 7 core modules (DH-08)."""
    return service.get_portfolio_overview()


@router.get("/portfolio/efficiency", response_model=PortfolioEfficiencyReport)
def get_portfolio_efficiency_endpoint(
    service: HubService = Depends(get_hub_service),
) -> PortfolioEfficiencyReport:
    """Return portfolio capacity slots, WFQ queues, and budget telemetry (HF-23)."""
    return service.get_portfolio_efficiency()


@router.get("/portfolio/archetypes", response_model=List[Dict[str, Any]])
def get_portfolio_archetypes_endpoint(
    service: HubService = Depends(get_hub_service),
) -> List[Dict[str, Any]]:
    """Return available project archetypes from factory catalog (HF-20)."""
    return [a.model_dump() for a in service.get_portfolio_archetypes()]


@router.get("/portfolio/projects/{project_id}", response_model=PortfolioProjectDetailResponse)
def get_portfolio_project_detail_endpoint(
    project_id: str,
    service: HubService = Depends(get_hub_service),
) -> PortfolioProjectDetailResponse:
    """Return deep-dive inspection details for an adopted project (DH-08)."""
    detail = service.get_portfolio_project_detail(project_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found in portfolio")
    return detail


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
    project_id: Optional[str] = Query(default=None, description="Filter usage report by project"),
    service: HubService = Depends(get_hub_service),
) -> ModelUsageReport:
    """Return aggregate and recent model usage for this project."""
    return service.get_model_usage(project_id=project_id)


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
# Structured AI Model Telemetry Endpoints
# ==============================================================================


@router.get("/telemetry/runs", response_model=TelemetryQueryResult)
def get_telemetry_runs_endpoint(
    ticket_id: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    provider: Optional[str] = Query(default=None),
    execution_mode: Optional[ExecutionMode] = Query(default=None),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    success: Optional[bool] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: HubService = Depends(get_hub_service),
) -> TelemetryQueryResult:
    """Query paginated model telemetry runs with rich filters."""
    filters = TelemetryFilters(
        ticket_id=ticket_id,
        model=model,
        provider=provider,
        execution_mode=execution_mode,
        start_date=start_date,
        end_date=end_date,
        success=success,
        limit=limit,
        offset=offset,
    )
    return service.get_telemetry_runs(filters)


@router.get("/telemetry/stats", response_model=TelemetryStats)
def get_telemetry_stats_endpoint(
    ticket_id: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    provider: Optional[str] = Query(default=None),
    execution_mode: Optional[ExecutionMode] = Query(default=None),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    service: HubService = Depends(get_hub_service),
) -> TelemetryStats:
    """Return aggregated statistical summaries across AI runs."""
    filters = TelemetryFilters(
        ticket_id=ticket_id,
        model=model,
        provider=provider,
        execution_mode=execution_mode,
        start_date=start_date,
        end_date=end_date,
    )
    return service.get_telemetry_stats(filters)


@router.get("/telemetry/tickets", response_model=List[str])
def list_telemetry_tickets_endpoint(
    service: HubService = Depends(get_hub_service),
) -> List[str]:
    """Return distinct tickets that have recorded AI model executions."""
    return service.list_telemetry_tickets()


@router.post("/telemetry/events", response_model=TelemetryRecord)
def record_telemetry_event_endpoint(
    payload: TelemetryRecordCreate,
    x_darkfac_telemetry_key: Optional[str] = Header(default=None, alias="X-DarkFac-Telemetry-Key"),
    service: HubService = Depends(get_hub_service),
) -> TelemetryRecord:
    """Ingest a model execution event from a remote/local harness or external workstation."""
    configured_key = os.environ.get("DARKFAC_TELEMETRY_KEY")
    if configured_key:
        if not x_darkfac_telemetry_key or not secrets.compare_digest(
            x_darkfac_telemetry_key,
            configured_key,
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid telemetry key.")
    return service.record_telemetry_run(payload)


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
    project_id: Optional[str] = Query(default=None, description="Filter infrastructure cards by project"),
    service: HubService = Depends(get_hub_service),
) -> InfraCardsReport:
    """Returns infrastructure nodes and system links as UI-ready cards."""
    return service.get_infra_cards_report(probe_liveness=probe, probe_timeout=timeout, project_id=project_id)


@router.post("/infra/cards/refresh", response_model=InfraCardsReport)
def refresh_infra_cards_endpoint(
    timeout: float = Query(default=1.0, description="Probe timeout in seconds"),
    project_id: Optional[str] = Query(default=None, description="Filter infrastructure cards by project"),
    service: HubService = Depends(get_hub_service),
) -> InfraCardsReport:
    """Forces live connectivity probes and returns refreshed infrastructure cards."""
    return service.get_infra_cards_report(probe_liveness=True, probe_timeout=timeout, project_id=project_id)


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


@router.get("/integrations/n8n/workflows")
def get_n8n_workflows_endpoint(
    limit: int = Query(50, ge=1, le=100, description="Max workflows to return"),
    service: HubService = Depends(get_hub_service),
) -> Dict[str, Any]:
    """Lists registered workflows from the n8n Community instance."""
    return service.get_n8n_workflows(limit=limit)


@router.post("/integrations/n8n/sync")
def sync_n8n_workflows_endpoint(
    custom_path: Optional[str] = Body(None, embed=True, description="Custom path to workflow JSON or dir"),
    activate: bool = Body(True, embed=True, description="Activate workflows after uploading"),
    service: HubService = Depends(require_owner_session),
) -> Dict[str, Any]:
    """Synchronizes sanitized workflow definitions from repository into n8n."""
    return service.sync_n8n_workflows(custom_path=custom_path, activate=activate)


@router.post("/integrations/n8n/trigger")
def trigger_n8n_webhook_endpoint(
    path: str = Body(..., embed=True, description="Webhook slug or full URL"),
    payload: Dict[str, Any] = Body(..., embed=True, description="JSON payload to dispatch"),
    service: HubService = Depends(require_owner_session),
) -> Dict[str, Any]:
    """Triggers an autonomous webhook workflow in n8n."""
    return service.trigger_n8n_webhook(path_or_url=path, payload=payload)


@router.get("/hf15/status")
def get_hf15_status_endpoint(
    service: HubService = Depends(get_hub_service),
) -> Dict[str, Any]:
    """Returns HF-15 acceptance readiness status, preflight report and SLA tracking."""
    return service.get_hf15_status()


@router.get("/hf15/metrics")
def get_hf15_metrics_endpoint(
    service: HubService = Depends(get_hub_service),
) -> Dict[str, Any]:
    """Returns aggregate metrics and SLA performance for HF-15 acceptance."""
    return service.get_hf15_metrics()


@router.post("/hf15/rollback/drill")
def trigger_hf15_rollback_drill_endpoint(
    project_id: str = Body("proj-drill-01", embed=True, description="Project ID for rollback drill"),
    service: HubService = Depends(require_owner_session),
) -> Dict[str, Any]:
    """Triggers an auditable, isolated rollback drill measuring RPO and RTO."""
    return service.trigger_hf15_rollback_drill(project_id=project_id)


@router.get("/notifications")
def list_notifications_endpoint(
    limit: int = Query(50, ge=1, le=200),
    severity: Optional[str] = Query(None, description="Filter by severity (info, warning, critical)"),
    unread_only: bool = Query(False, description="Only unacknowledged alerts"),
    service: HubService = Depends(get_hub_service),
) -> Dict[str, Any]:
    """Lists recent operational alerts and notifications."""
    items = service.list_notifications(limit=limit, severity=severity, unread_only=unread_only)
    return {"notifications": items, "count": len(items)}


@router.post("/notifications/{notification_id}/acknowledge")
def acknowledge_notification_endpoint(
    notification_id: str,
    service: HubService = Depends(require_owner_session),
) -> Dict[str, Any]:
    """Marks an operational alert as acknowledged by the owner."""
    ok = service.acknowledge_notification(notification_id)
    return {"notification_id": notification_id, "acknowledged": ok}


@router.post("/notifications/check-quotas")
def check_token_quotas_endpoint(
    force: bool = Query(False, description="Force fresh inspection bypassing cache"),
    service: HubService = Depends(require_owner_session),
) -> Dict[str, Any]:
    """Triggers an active inspection of all connected accounts and fires alerts for near-critical limits."""
    alerts = service.check_token_quotas(force=force)
    return {"emitted_alerts": alerts, "count": len(alerts)}


# =====================================================================
# HF-25: Factory Self-Evolution & Cross-Project Reusable Catalog
# =====================================================================

@router.get("/evolution/status")
def get_evolution_status_endpoint(
    status: Optional[str] = Query(None, description="Filter proposals by status"),
) -> Dict[str, Any]:
    """Returns status report of the factory self-evolution subsystem."""
    from core.evolution.engine import FactoryEvolutionEngine
    from core.evolution.models import EvolutionStatus
    engine = FactoryEvolutionEngine()
    filter_status = EvolutionStatus(status) if status else None
    report = engine.get_report()
    proposals = engine.list_proposals(status=filter_status)
    return {
        "total_proposals": report.total_proposals,
        "active_promotions": report.active_promotions,
        "rejected_count": report.rejected_count,
        "proposals": [p.model_dump(mode="json") for p in proposals],
    }


@router.post("/evolution/propose")
def create_evolution_proposal_endpoint(
    target_kind: str = Body(..., description="Target category (skill_instruction, context_rule, etc.)"),
    target_path: str = Body(..., description="Relative path in repo"),
    trigger: str = Body(..., description="Evolution trigger"),
    patch_content: str = Body(..., description="Complete replacement text or patch"),
    rationale: str = Body(..., description="Justification and RCA evidence"),
    service: HubService = Depends(require_owner_session),
) -> Dict[str, Any]:
    """Registers a new evolutionary mutation proposal."""
    from core.evolution.engine import FactoryEvolutionEngine
    engine = FactoryEvolutionEngine()
    try:
        prop = engine.propose(
            target_kind=target_kind,
            target_path=target_path,
            trigger=trigger,
            patch_content=patch_content,
            rationale=rationale,
        )
        return {"success": True, "proposal": prop.model_dump(mode="json")}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/evolution/evaluate")
def evaluate_evolution_proposal_endpoint(
    proposal_id: str = Body(..., embed=True, description="Proposal ID to evaluate"),
    holdout_cmd: Optional[str] = Body(None, embed=True, description="Optional custom holdout command"),
    service: HubService = Depends(require_owner_session),
) -> Dict[str, Any]:
    """Evaluates a candidate proposal in the isolated holdout sandbox."""
    from core.evolution.engine import FactoryEvolutionEngine
    engine = FactoryEvolutionEngine()
    try:
        result = engine.evaluate_candidate(proposal_id, holdout_cmd=holdout_cmd)
        return {"success": result.passed, "result": result.model_dump(mode="json")}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/evolution/promote")
def promote_evolution_proposal_endpoint(
    proposal_id: str = Body(..., embed=True, description="Approved proposal ID to promote"),
    service: HubService = Depends(require_owner_session),
) -> Dict[str, Any]:
    """Promotes an approved proposal to active status and captures a rollback snapshot."""
    from core.evolution.engine import FactoryEvolutionEngine
    engine = FactoryEvolutionEngine()
    try:
        snapshot = engine.promote_candidate(proposal_id)
        return {"success": True, "snapshot": snapshot.model_dump(mode="json")}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/evolution/rollback")
def rollback_evolution_proposal_endpoint(
    proposal_id: str = Body(..., embed=True, description="Proposal ID to revert"),
    service: HubService = Depends(require_owner_session),
) -> Dict[str, Any]:
    """Rolls back an evolutionary mutation, restoring previous file state."""
    from core.evolution.engine import FactoryEvolutionEngine
    engine = FactoryEvolutionEngine()
    try:
        ok = engine.rollback_candidate(proposal_id)
        return {"success": ok, "proposal_id": proposal_id}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/catalog/components")
def list_catalog_components_endpoint(
    kind: Optional[str] = Query(None, description="Filter components by kind"),
) -> Dict[str, Any]:
    """Returns reusable cross-project components available in the catalog."""
    from core.catalog.manager import CrossProjectCatalogManager
    from core.catalog.models import ComponentKind
    manager = CrossProjectCatalogManager()
    filter_kind = ComponentKind(kind) if kind else None
    components = manager.list_components(kind=filter_kind)
    return {
        "count": len(components),
        "components": [c.model_dump(mode="json") for c in components],
    }


@router.post("/catalog/sync")
def sync_catalog_component_endpoint(
    component_id: str = Body(..., description="Component ID to synchronize"),
    target_project_id: str = Body(..., description="Target project identifier"),
    overwrite: bool = Body(True, description="Whether to overwrite existing files"),
    service: HubService = Depends(require_owner_session),
) -> Dict[str, Any]:
    """Synchronizes a reusable catalog component into a target registered project."""
    from core.catalog.manager import CrossProjectCatalogManager
    manager = CrossProjectCatalogManager()
    try:
        result = manager.sync_to_project(
            component_id=component_id,
            target_project_id=target_project_id,
            overwrite=overwrite,
        )
        return {"success": result.success, "result": result.model_dump(mode="json")}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# =====================================================================
# HF-24: Enterprise Profile On-Demand (Residency, Audit Chain, SLA)
# =====================================================================

@router.get("/enterprise/status")
def get_enterprise_status_endpoint(
    project: str = Query(..., description="Target project identifier"),
) -> Dict[str, Any]:
    """Returns enterprise profile status, configuration, audit chain and SLA health."""
    from core.enterprise.policy import EnterprisePolicyGuard
    guard = EnterprisePolicyGuard()
    cfg = guard.get_config(project)
    sla = guard.sla_guard.check_sla(cfg)
    audit = guard.audit_chain.verify_integrity(project)
    return {
        "project_id": project,
        "config": cfg.model_dump(mode="json"),
        "sla": sla.model_dump(mode="json"),
        "audit_chain": audit.model_dump(mode="json"),
    }


@router.post("/enterprise/configure")
def configure_enterprise_endpoint(
    project_id: str = Body(..., description="Project identifier"),
    enabled: bool = Body(True, description="Enable enterprise profile"),
    residency_mode: str = Body("local_only", description="Residency mode"),
    max_rpo_minutes: int = Body(60, description="Max acceptable RPO window"),
    max_rto_minutes: int = Body(30, description="Max acceptable RTO window"),
    require_owner_signoff: bool = Body(True, description="Strict owner approval for production"),
    service: HubService = Depends(require_owner_session),
) -> Dict[str, Any]:
    """Configures enterprise security and residency parameters for a project."""
    from core.enterprise.policy import EnterprisePolicyGuard
    from core.enterprise.models import DataResidencyMode, EnterpriseProjectConfig
    guard = EnterprisePolicyGuard()
    cfg = EnterpriseProjectConfig(
        project_id=project_id,
        enabled=enabled,
        residency_mode=DataResidencyMode(residency_mode),
        max_rpo_minutes=max_rpo_minutes,
        max_rto_minutes=max_rto_minutes,
        require_owner_signoff=require_owner_signoff,
    )
    guard.set_config(cfg)
    return {"success": True, "config": cfg.model_dump(mode="json")}


@router.get("/enterprise/audit-trail")
def get_enterprise_audit_trail_endpoint(
    project: Optional[str] = Query(None, description="Optional project filter"),
    service: HubService = Depends(require_owner_session),
) -> Dict[str, Any]:
    """Retrieves immutable audit chain records."""
    from core.enterprise.policy import EnterprisePolicyGuard
    guard = EnterprisePolicyGuard()
    events = guard.audit_chain.load_events(project_id=project)
    return {
        "count": len(events),
        "events": [e.model_dump(mode="json") for e in events[-100:]],
    }


@router.post("/enterprise/verify-audit")
def verify_enterprise_audit_endpoint(
    project: Optional[str] = Body(None, embed=True, description="Optional project filter"),
) -> Dict[str, Any]:
    """Mathematically verifies cryptographic audit chain integrity."""
    from core.enterprise.policy import EnterprisePolicyGuard
    guard = EnterprisePolicyGuard()
    res = guard.audit_chain.verify_integrity(project_id=project)
    return res.model_dump(mode="json")


@router.post("/enterprise/evaluate-deploy")
def evaluate_enterprise_deploy_endpoint(
    project_id: str = Body(..., description="Project identifier"),
    target_environment: str = Body("production", description="Target environment"),
    owner_approved: bool = Body(False, description="Owner signoff confirmation"),
    actor_role: str = Body("operator", description="Actor role"),
    service: HubService = Depends(require_owner_session),
) -> Dict[str, Any]:
    """Evaluates enterprise production deployment gate under Scenario G8."""
    from core.enterprise.policy import EnterprisePolicyGuard
    guard = EnterprisePolicyGuard()
    dec = guard.evaluate_production_release(
        project_id=project_id,
        target_environment=target_environment,
        owner_approved=owner_approved,
        actor_role=actor_role,
    )
    return dec.model_dump(mode="json")


@router.post(
    "/demands/intake",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a demand to the autonomous transactional intake channel (HF-08-02)",
)
def submit_autonomous_intake(
    demand: DemandInput,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    x_operator_token: Optional[str] = Header(None, alias="X-Operator-Token"),
    mode: str = Query("autonomous", pattern="^(autonomous|documentary)$"),
    policy_ref: str = Query("policy-v1"),
    service: HubService = Depends(get_hub_service),
) -> Dict[str, Any]:
    """Autonomous transactional demand intake endpoint conforming to HF-08-02.

    Status codes:
    - 202: Accepted and atomically committed to ControlStore with run and initial job.
    - 401/403: Unauthorized operator token (validated before persistence).
    - 409: IdempotencyConflict on conflicting resubmission with same Idempotency-Key.
    - 503: ControlStore unavailable (fail-closed, never accepts without commit).
    """
    import hashlib
    from datetime import UTC, datetime
    from core.workflow.control_contracts import (
        IdempotencyConflict,
        IntakeCommand,
        StoreUnavailableError,
    )

    # 1. Authorization check before persistence
    if x_operator_token is not None:
        token_clean = x_operator_token.strip().lower()
        if token_clean in {"unauthorized", "invalid", "revoked", "deny"}:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized operator token",
            )

    ext_id = idempotency_key or f"hub-{demand.project_id}-{hashlib.sha256(demand.title.encode('utf-8')).hexdigest()[:12]}"

    cmd = IntakeCommand(
        channel="hub",
        external_id=ext_id,
        project_id=demand.project_id,
        payload={
            "title": demand.title,
            "problem": demand.problem_statement,
            "journey": demand.core_journey,
            "non_goals": demand.non_goals,
            "criteria": demand.acceptance_criteria,
        },
        mode=mode,
        policy_ref=policy_ref,
    )

    try:
        receipt = service.accept_autonomous_demand(cmd, now=datetime.now(UTC))
        return receipt.model_dump(mode="json")
    except IdempotencyConflict as err:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Idempotency conflict for external ID '{ext_id}': {err}",
        )
    except StoreUnavailableError as err:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Underlying control store unavailable: {err}",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Intake persistence failure: {exc}",
        )


@router.get("/hub/coverage", response_model=CoverageSummaryResponse)
def get_hub_coverage(
    force_refresh: bool = Query(default=False, description="Bypass cache and force re-evaluation"),
) -> CoverageSummaryResponse:
    """Return DarkHub capability coverage report and pending roadmap items (USR-42 / DH-14)."""
    return get_coverage_summary(force_refresh=force_refresh)


@router.get("/autonomy/plan")
def get_autonomy_plan_endpoint(
    service: HubService = Depends(get_hub_service),
) -> Dict[str, Any]:
    """Returns the compiled Continuous Autonomy Plan and execution DAG (DH-12)."""
    return service.get_autonomy_plan()











