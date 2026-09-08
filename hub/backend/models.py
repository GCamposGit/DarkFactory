"""
Data models and schemas with strict Pydantic v2 typing for DarkHub.
"""

from enum import Enum
from typing import Annotated, Any, Dict, List, Optional
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, Field, StringConstraints


def _validate_service_url(value: str) -> str:
    """Accept only absolute HTTP(S) URLs safe for browser navigation."""
    if value != value.strip() or any(character.isspace() for character in value):
        raise ValueError("service URL must not contain whitespace")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("service URL must be an absolute HTTP(S) URL")
    return value


ServiceId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9_-]{0,98}[a-z0-9])?$"),
]
ServiceUrl = Annotated[str, AfterValidator(_validate_service_url)]
ServiceColor = Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")]


class ServiceCategory(str, Enum):
    LOCAL_CLUSTER = "local_cluster"
    LLM_CHAT = "llm_chat"
    CODE_AGENTS = "code_agents"
    IMAGE_AUDIO = "image_audio"
    INFRA_APIS = "infra_apis"
    CUSTOM = "custom"


class ServiceItem(BaseModel):
    id: ServiceId = Field(..., description="Unique lowercase slug or identifier")
    name: str = Field(..., min_length=1, max_length=100, description="Display name of the tool or service")
    url: ServiceUrl = Field(..., description="Absolute HTTP(S) URL of the service or local web app")
    category: ServiceCategory = Field(default=ServiceCategory.CUSTOM, description="Primary category")
    description: str = Field(default="", max_length=500, description="Brief description of the service")
    tags: List[str] = Field(default_factory=list, description="Searchable tags")
    icon: str = Field(default="globe", description="Lucide icon name or emoji")
    color: ServiceColor = Field(default="#3b82f6", description="Six-digit accent color hex code")
    is_favorite: bool = Field(default=False, description="Whether marked as favorite")
    pinned: bool = Field(default=False, description="Whether pinned in quick dock")
    is_local: bool = Field(default=False, description="Whether hosted locally (e.g. localhost)")


class ServiceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    url: ServiceUrl
    category: ServiceCategory = Field(default=ServiceCategory.CUSTOM)
    description: str = Field(default="", max_length=500)
    tags: List[str] = Field(default_factory=list)
    icon: str = Field(default="globe")
    color: ServiceColor = Field(default="#3b82f6")
    is_favorite: bool = Field(default=False)
    pinned: bool = Field(default=False)
    is_local: bool = Field(default=False)


class ServiceUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    url: Optional[ServiceUrl] = None
    category: Optional[ServiceCategory] = None
    description: Optional[str] = Field(default=None, max_length=500)
    tags: Optional[List[str]] = None
    icon: Optional[str] = None
    color: Optional[ServiceColor] = None
    is_favorite: Optional[bool] = None
    pinned: Optional[bool] = None
    is_local: Optional[bool] = None


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



