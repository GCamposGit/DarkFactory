"""
Data models and schemas with strict Pydantic v2 typing for DarkHub.
"""

from datetime import datetime, timezone
from enum import Enum
import re
from typing import List, Optional, Dict, Any
import urllib.parse
from pydantic import BaseModel, Field, field_validator

COLOR_HEX_REGEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
ID_SLUG_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")


def validate_safe_url(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError("URL must be a string.")
    cleaned = v.strip()
    if not cleaned:
        raise ValueError("URL cannot be empty.")
    if any(ord(c) < 32 or ord(c) == 127 for c in cleaned):
        raise ValueError("URL contains illegal control characters.")
    
    parsed = urllib.parse.urlparse(cleaned)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(
            f"Invalid URL protocol '{scheme}'. Only 'http' and 'https' protocols are permitted."
        )
    if not parsed.netloc:
        raise ValueError("URL must contain a valid network location/host.")
    return cleaned


def validate_safe_color(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    if not isinstance(v, str):
        raise ValueError("Color must be a string.")
    cleaned = v.strip()
    if not COLOR_HEX_REGEX.match(cleaned):
        raise ValueError(
            f"Invalid color '{v}'. Expected valid hexadecimal color code (e.g. #3b82f6 or #fff)."
        )
    return cleaned


def validate_safe_id(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError("ID must be a string.")
    cleaned = v.strip()
    if not ID_SLUG_REGEX.match(cleaned):
        raise ValueError(
            f"Invalid ID '{v}'. ID must be 1-100 characters and contain only letters, numbers, dashes, or underscores."
        )
    return cleaned


class ServiceCategory(str, Enum):
    LOCAL_CLUSTER = "local_cluster"
    LLM_CHAT = "llm_chat"
    CODE_AGENTS = "code_agents"
    IMAGE_AUDIO = "image_audio"
    INFRA_APIS = "infra_apis"
    CUSTOM = "custom"


class ServiceItem(BaseModel):
    id: str = Field(..., description="Unique slug or identifier")
    name: str = Field(..., min_length=1, max_length=100, description="Display name of the tool or service")
    url: str = Field(..., description="Target URL of the service or local web app")
    category: ServiceCategory = Field(default=ServiceCategory.CUSTOM, description="Primary category")
    description: str = Field(default="", max_length=500, description="Brief description of the service")
    tags: List[str] = Field(default_factory=list, description="Searchable tags")
    icon: str = Field(default="globe", description="Lucide icon name or emoji")
    color: str = Field(default="#3b82f6", description="Accent color hex code")
    is_favorite: bool = Field(default=False, description="Whether marked as favorite")
    pinned: bool = Field(default=False, description="Whether pinned in quick dock")
    is_local: bool = Field(default=False, description="Whether hosted locally (e.g. localhost)")
    launch_script: Optional[str] = Field(default=None, description="Optional relative or absolute python script to launch service if offline")
    fallback_urls: List[str] = Field(
        default_factory=list,
        description="Optional additional URLs to probe when the primary URL is unavailable",
    )

    @field_validator("id")
    @classmethod
    def check_id(cls, v: str) -> str:
        return validate_safe_id(v)

    @field_validator("url")
    @classmethod
    def check_url(cls, v: str) -> str:
        return validate_safe_url(v)

    @field_validator("fallback_urls")
    @classmethod
    def check_fallback_urls(cls, values: List[str]) -> List[str]:
        return [validate_safe_url(value) for value in values]

    @field_validator("color")
    @classmethod
    def check_color(cls, v: str) -> str:
        res = validate_safe_color(v)
        return res or "#3b82f6"


class ServiceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    url: str = Field(..., min_length=1)
    category: ServiceCategory = Field(default=ServiceCategory.CUSTOM)
    description: str = Field(default="", max_length=500)
    tags: List[str] = Field(default_factory=list)
    icon: str = Field(default="globe")
    color: str = Field(default="#3b82f6")
    is_favorite: bool = Field(default=False)
    pinned: bool = Field(default=False)
    is_local: bool = Field(default=False)
    launch_script: Optional[str] = Field(default=None)
    fallback_urls: List[str] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def check_url(cls, v: str) -> str:
        return validate_safe_url(v)

    @field_validator("fallback_urls")
    @classmethod
    def check_fallback_urls(cls, values: List[str]) -> List[str]:
        return [validate_safe_url(value) for value in values]

    @field_validator("color")
    @classmethod
    def check_color(cls, v: str) -> str:
        res = validate_safe_color(v)
        return res or "#3b82f6"


class ServiceUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    url: Optional[str] = Field(default=None, min_length=1)
    category: Optional[ServiceCategory] = None
    description: Optional[str] = Field(default=None, max_length=500)
    tags: Optional[List[str]] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    is_favorite: Optional[bool] = None
    pinned: Optional[bool] = None
    is_local: Optional[bool] = None
    launch_script: Optional[str] = None
    fallback_urls: Optional[List[str]] = None

    @field_validator("url")
    @classmethod
    def check_url(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return validate_safe_url(v)
        return v

    @field_validator("fallback_urls")
    @classmethod
    def check_fallback_urls(cls, values: Optional[List[str]]) -> Optional[List[str]]:
        if values is None:
            return values
        return [validate_safe_url(value) for value in values]

    @field_validator("color")
    @classmethod
    def check_color(cls, v: Optional[str]) -> Optional[str]:
        return validate_safe_color(v)


class ServiceLaunchResponse(BaseModel):
    service_id: str = Field(..., description="ID of the service")
    url: str = Field(..., description="Destination URL of the service")
    status: str = Field(..., description="Status after launch attempt ('online', 'already_running', 'starting', 'external', 'failed')")
    launched: bool = Field(..., description="Whether a new background process was spawned")
    message: str = Field(default="", description="Descriptive status message")


class HealthStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class HealthCheckResult(BaseModel):
    service_id: str
    url: str
    status: HealthStatus
    latency_ms: Optional[float] = None
    status_code: Optional[int] = None
    error: Optional[str] = None


class PromptTemplate(BaseModel):
    id: str
    title: str
    description: str
    tags: List[str] = Field(default_factory=list)
    content: str


class OllamaModelInfo(BaseModel):
    name: str
    parameter_size: Optional[str] = None
    quantization: Optional[str] = None
    format: Optional[str] = None
    modified_at: Optional[str] = None


class OllamaStatusResponse(BaseModel):
    is_online: bool
    base_url: str
    model_count: int
    models: List[OllamaModelInfo] = Field(default_factory=list)
    error: Optional[str] = None


class OllamaGenerateRequest(BaseModel):
    model: str = Field(..., min_length=1, description="Ollama model name (e.g. qwen-fast:latest)")
    prompt: str = Field(..., min_length=1, description="Prompt text")
    system: Optional[str] = Field(default=None, description="Optional system prompt")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)


class OllamaGenerateResponse(BaseModel):
    response: str
    model: str
    done: bool
    total_duration_ms: Optional[float] = None


class PlaygroundProvider(str, Enum):
    OLLAMA = "ollama"
    OPENROUTER = "openrouter"


class OpenRouterModelInfo(BaseModel):
    id: str
    name: str
    context_length: Optional[int] = None
    input_cost_per_m: Optional[float] = None
    output_cost_per_m: Optional[float] = None
    is_pareto: bool = False


class OpenRouterStatusResponse(BaseModel):
    has_key: bool
    is_authenticated: bool
    key_label: Optional[str] = None
    is_free_tier: bool = False
    usage_usd: Optional[float] = 0.0
    models: List[OpenRouterModelInfo] = Field(default_factory=list)
    error: Optional[str] = None


class UnifiedGenerateRequest(BaseModel):
    provider: PlaygroundProvider = Field(default=PlaygroundProvider.OLLAMA)
    model: str = Field(..., min_length=1, description="Model identifier (e.g. qwen-fast:latest or anthropic/claude-3.7-sonnet)")
    prompt: str = Field(..., min_length=1, description="Prompt text")
    system: Optional[str] = Field(default=None, description="Optional system prompt")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=1024, ge=1, le=8192)


class UnifiedGenerateResponse(BaseModel):
    response: str
    provider: PlaygroundProvider
    model: str
    done: bool = True
    total_duration_ms: Optional[float] = None
    tokens_used: Optional[int] = None
    cost_usd: Optional[float] = None
    error: Optional[str] = None


class ExportCatalogResponse(BaseModel):
    exported_at: str
    version: str = "1.0"
    count: int
    services: List[ServiceItem]


class ImportCatalogRequest(BaseModel):
    services: List[ServiceItem]
    merge: bool = Field(default=False, description="If True, merge with existing; if False, overwrite entire catalog")


class ImportCatalogResponse(BaseModel):
    imported_count: int
    total_count: int
    message: str


class LearningPackSummary(BaseModel):
    pack_id: str
    session_id: str
    timestamp: str
    title: str
    executive_summary: str
    concepts_count: int
    flashcards_count: int
    concept_names: List[str] = Field(default_factory=list)
    file_json: str
    file_md: str
    file_html: str
    file_anki: str


class GenerateLearningPackRequest(BaseModel):
    title: Optional[str] = Field(default=None, description="Optional custom title for the session pack")
    session_id: Optional[str] = Field(default=None, description="Associated session ID")
    files: Optional[List[str]] = Field(default=None, description="Specific modified files to analyze")


class GenerateLearningPackResponse(BaseModel):
    pack_id: str
    title: str
    executive_summary: str
    concepts_count: int
    flashcards_count: int
    saved_paths: Dict[str, str]
    rendered_md: str


# ---------------------------------------------------------------------------
# Benchmark & Multi-Domain Routing Models
# ---------------------------------------------------------------------------

class BenchmarkDomainSummary(BaseModel):
    domain_key: str
    name: str
    canonical_benchmark: str
    evaluates: str
    source_authority: str
    target_metric_scale: str



class RouteTaskBenchmarkRequest(BaseModel):
    requirement: str = Field(..., description="Task description or development roadmap requirement to route")
    complexity: str = Field(default="medium", description="Task complexity level: low, medium, high, critical")
    offline: bool = Field(default=False, description="Whether to route strictly to $0 local offline cluster")
    domain_override: Optional[str] = Field(default=None, description="Optional manual domain override")


class RouteTaskBenchmarkResponse(BaseModel):
    requirement: str
    detected_domain: str
    confidence: float
    intent_explanation: str
    canonical_benchmark: str
    optimal_model_id: str
    optimal_model_name: str
    domain_score: float
    cost_per_task: float
    speculative_candidates: List[Dict[str, Any]]
    local_empirical_stats: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Anti-AI-Slop Content Engine Models
# ---------------------------------------------------------------------------

class ContentGenerateRequest(BaseModel):
    topic: str = Field(..., description="Topic or title of the content")
    content_type: str = Field(default="linkedin_post", description="Target format: linkedin_post, technical_blog, commercial_proposal, release_notes, executive_memo, thought_leadership, documentation")
    target_audience: str = Field(default="software engineers, technical leaders, CTOs", description="Target audience profile")
    key_points: List[str] = Field(default_factory=list, description="Key bullet points or technical arguments")
    raw_context: Optional[str] = Field(default=None, description="Optional background context or reference text")
    offline: bool = Field(default=False, description="Whether to force $0 local procedural synthesis")
    model_override: Optional[str] = Field(default=None, description="Cloud model override")


class ContentGenerateResponse(BaseModel):
    content_id: str
    title: str
    content_type: str
    final_content: str
    initial_slop_score: float
    final_slop_score: float
    cleanliness_rating: str
    word_count: int
    provider: str
    model_used: str
    created_at: str


class ContentLintRequest(BaseModel):
    text: str = Field(..., description="Raw text content to audit for AI slop")
    custom_banned_words: Optional[List[str]] = Field(default=None, description="User-specific words to treat as violations")


class ContentLintResponse(BaseModel):
    slop_score: float
    cleanliness_rating: str
    violations_count: int
    cadence_rating: str
    sentence_length_variance: float
    violations: List[Dict[str, Any]]
    top_fixes: List[str]


# ---------------------------------------------------------------------------
# Context-Aware Visual Asset Studio Models
# ---------------------------------------------------------------------------

class VisualGenerateRequest(BaseModel):
    title: str = Field(..., description="Title or message to render")
    subtitle: Optional[str] = Field(default=None, description="Optional subtitle or descriptor")
    asset_type: str = Field(default="social_banner", description="Format: social_banner, blog_hero, architecture_diagram, ui_mockup, app_icon, editorial_illustration")
    theme: str = Field(default="modern_minimalist_dark", description="Visual theme: modern_minimalist_dark, cyberpunk_terminal, blueprint_technical, clean_vector_3d, glassmorphism")
    aspect_ratio: str = Field(default="16:9", description="Ratio: 1:1, 16:9, 4:5, 9:16, 21:9")
    high_res: bool = Field(default=False, description="Whether to render at full 1080p/4K resolution")
    offline: bool = Field(default=False, description="Whether to force $0 local procedural rendering")


class VisualGenerateResponse(BaseModel):
    asset_id: str
    title: str
    asset_type: str
    theme: str
    aspect_ratio: str
    width: int
    height: int
    file_path: str
    provider: str
    model_used: str
    generation_time_ms: int
    cost_usd: float
    created_at: str


class VisualIllustrateRequest(BaseModel):
    text: str = Field(..., description="Text or article to semantically illustrate")
    asset_type: str = Field(default="blog_hero", description="Visual asset format")
    theme: Optional[str] = Field(default=None, description="Optional visual theme override")
    aspect_ratio: Optional[str] = Field(default=None, description="Optional aspect ratio override")
    offline: bool = Field(default=False, description="Whether to force $0 local procedural rendering")


# ---------------------------------------------------------------------------
# Task Dashboard Models (DF-21)
# ---------------------------------------------------------------------------


class TaskDashboardEvidence(BaseModel):
    """Small, safe-to-render evidence reference for the task cockpit."""

    label: str = Field(min_length=1, max_length=160)
    value: str = Field(default="", max_length=500)
    source: Optional[str] = Field(default=None, max_length=160)


class TaskDashboardItem(BaseModel):
    """Read-only task projection assembled from lifecycle, run and usage ledgers."""

    task_id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=240)
    status: str = Field(min_length=1, max_length=40)
    stage: str = Field(min_length=1, max_length=120)
    priority: int = 0
    queue_position: int = Field(ge=1)
    run_id: Optional[str] = None
    run_status: Optional[str] = None
    step_index: Optional[int] = Field(default=None, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    updated_at: Optional[str] = None
    evidence: List[TaskDashboardEvidence] = Field(default_factory=list)
    exceptions: List[str] = Field(default_factory=list)
    cause_code: Optional[str] = None
    diagnostic: Optional[str] = None


class TaskDashboardReport(BaseModel):
    """Stable API response for the queue/run/stage/cost evidence journey."""

    generated_at: str
    queue: List[TaskDashboardItem] = Field(default_factory=list)
    queued_count: int = Field(default=0, ge=0)
    running_count: int = Field(default=0, ge=0)
    exception_count: int = Field(default=0, ge=0)
    total_cost_usd: float = Field(default=0.0, ge=0.0)
    sources: Dict[str, str] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Usage & Telemetry Synchronization Models (USR-01)
# ---------------------------------------------------------------------------


class UsageSyncPayload(BaseModel):
    """Payload sent by a client workstation or worker node to synchronize account quotas and credits."""

    client_node_id: str = Field(default="workstation", description="Identifier of origin node (e.g. predator-neo-16)")
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(), description="RFC3339 timestamp")
    accounts: List[Dict[str, Any]] = Field(default_factory=list, description="List of raw ProviderAccountUsage payloads")
    credits: List[Dict[str, Any]] = Field(default_factory=list, description="List of raw ProviderCreditCard payloads")


class UsageSyncResponse(BaseModel):
    """Response returned by DarkHub upon successful synchronization."""

    status: str = "synchronized"
    client_node_id: str
    accounts_updated: int
    credits_updated: int
    synced_at: str
    message: str


# ---------------------------------------------------------------------------
# Continuous Autonomy Progress & Stagnation Models (HF-13-02)
# ---------------------------------------------------------------------------


class IncidentAlert(BaseModel):
    """Deduplicated operational alert for an active incident."""

    incident_id: str = Field(description="Unique deduplication key for the incident, e.g. stalled:darkfac")
    severity: str = Field(default="warning", description="Severity level: info, warning, error, critical")
    message: str = Field(description="Human-readable incident summary")
    detected_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(), description="ISO 8601 UTC timestamp")
    details: Dict[str, Any] = Field(default_factory=dict, description="Structured diagnostics context")


class ProgressProjection(BaseModel):
    """Analytical projection of portfolio execution progress and stagnation indicators."""

    project_id: str = Field(description="Identifier of target project")
    oldest_eligible_age: float = Field(default=0.0, ge=0.0, description="Age in seconds of oldest ready/pending job")
    last_dispatch: Optional[str] = Field(default=None, description="ISO 8601 timestamp of most recent job dispatch")
    last_reconcile: Optional[str] = Field(default=None, description="ISO 8601 timestamp of last supervisor reconciliation")
    heartbeats_count: int = Field(default=0, ge=0, description="Count of currently active heartbeats / leases")
    ready_count: int = Field(default=0, ge=0, description="Count of jobs currently in pending/ready state")
    running_count: int = Field(default=0, ge=0, description="Count of jobs currently being executed")
    blocked_count: int = Field(default=0, ge=0, description="Count of jobs waiting on dependencies or human input")
    wait_reasons: Dict[str, str] = Field(default_factory=dict, description="Mapping from job/ticket ID to cause or waiting reason")
    next_wakeup: Optional[str] = Field(default=None, description="Scheduled ISO 8601 timestamp for next supervisor tick")
    reservations: List[Dict[str, Any]] = Field(default_factory=list, description="Active portfolio reservations")
    evidence_chain: List[str] = Field(default_factory=list, description="Recent evidence URIs produced by completed stages")
    stalled: bool = Field(default=False, description="True if ready jobs wait >30s with available worker capacity")
    alerts: List[IncidentAlert] = Field(default_factory=list, description="Deduplicated operational alerts")
    healthy: bool = Field(default=True, description="Overall health of the project execution pipeline")
    capacity_available: bool = Field(default=True, description="Whether worker capacity is available for new dispatches")


# ---------------------------------------------------------------------------
# Multi-Project Portfolio Models (DH-08)
# ---------------------------------------------------------------------------


class PortfolioProjectSummary(BaseModel):
    """Aggregated portfolio summary of an adopted project across 5 core dimensions."""

    id: str = Field(description="Unique project slug identifier")
    name: str = Field(description="Human-readable project name")
    description: str = Field(default="", description="Project scope and description")
    path: Optional[str] = Field(default=None, description="Absolute workspace filesystem path")
    kind: str = Field(description="Architectural kind: core, client_portfolio, internal_product, saas_app")
    prefix: str = Field(default="PRJ", description="Ticket prefix for demand management")
    domain: Optional[str] = Field(default=None, description="Public primary domain")
    deploy_target: Optional[str] = Field(default=None, description="Deployment target identifier")
    repo_url: Optional[str] = Field(default=None, description="Git remote repository URL")
    default_branch: str = Field(default="main", description="Default branch name")
    created_at: str = Field(description="Registration timestamp")

    # 5 Key Dimensions (DH-08)
    dev_stage: str = Field(default="active_development", description="Lifecycle stage: production, active_development, prototyping, governed")
    health_status: str = Field(default="healthy", description="Aggregated health status: healthy, warning, stale, unknown")
    health_details: Dict[str, Any] = Field(default_factory=dict, description="Detailed health diagnostics")
    last_deploy: Dict[str, Any] = Field(default_factory=dict, description="Last deployment details and smoke check results")
    roadmap_summary: Dict[str, Any] = Field(default_factory=dict, description="Compiled roadmap metrics (total, delivered, in_progress, completion_pct)")
    budget_summary: Dict[str, Any] = Field(default_factory=dict, description="Monthly spending limit, spent amount, utilization and status")
    adoption_summary: Dict[str, Any] = Field(default_factory=dict, description="Adoption lock status, autonomy level and file verification")
    archetype_summary: Optional[Dict[str, Any]] = Field(default=None, description="Matched archetype blueprint info")
    pilots_summary: Dict[str, Any] = Field(default_factory=dict, description="Active pilot specs and evaluation verdicts")
    line_summary: Dict[str, Any] = Field(default_factory=dict, description="Autonomous production line stages and affinity")
    game_summary: Dict[str, Any] = Field(default_factory=dict, description="Deterministic game engine status / simulation")


class PortfolioProjectDetailResponse(BaseModel):
    """Complete deep-dive inspection response for a single adopted project."""

    project: PortfolioProjectSummary
    commands: Dict[str, List[str]] = Field(default_factory=dict, description="Resolved setup, validate, build, smoke commands")
    smoke_checks: List[Dict[str, Any]] = Field(default_factory=list, description="Configured smoke check endpoints")
    verification: Dict[str, Any] = Field(default_factory=dict, description="Adoption verification report (lock, drift, managed files)")
    roadmap_health: Dict[str, Any] = Field(default_factory=dict, description="Detailed roadmap health report")


class PortfolioOverviewResponse(BaseModel):
    """Multi-project portfolio cockpit overview response."""

    total_projects: int = Field(ge=0, description="Total adopted projects count")
    healthy_projects: int = Field(ge=0, description="Projects with healthy operational status")
    warning_projects: int = Field(ge=0, description="Projects with warnings or blockers")
    total_budget_limit_usd: float = Field(ge=0.0, description="Sum of monthly USD budgets")
    total_spent_usd: float = Field(ge=0.0, description="Sum of spent USD this billing cycle")
    active_heavy_slots: int = Field(ge=0, description="Active heavy execution slots")
    max_heavy_slots: int = Field(ge=1, description="Maximum heavy execution slots")
    active_light_slots: int = Field(ge=0, description="Active light execution slots")
    max_light_slots: int = Field(ge=1, description="Maximum light execution slots")
    projects: List[PortfolioProjectSummary] = Field(default_factory=list, description="Summary list of adopted projects")
    archetypes: List[Dict[str, Any]] = Field(default_factory=list, description="Available project archetypes in the factory catalog")
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(), description="Generation timestamp")




