"""Pydantic v2 data models for Enterprise Growth, CMS/Blog, CRM and Marketing (HF-21)."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator


class BlogPost(BaseModel):
    """Specification for a long-form article or thinking essay (Astro 5 schema compliant)."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(..., min_length=3, description="Title of the article.")
    slug: Optional[str] = Field(None, description="URL slug. Derived from title if omitted.")
    description: Optional[str] = Field(None, description="Meta description / synopsis.")
    date: datetime.date = Field(default_factory=datetime.date.today, description="Publication date.")
    originalYear: int = Field(2026, description="Year of origination.")
    image: Optional[str] = Field(None, description="Hero image path or URL.")
    tags: List[str] = Field(default_factory=list, description="Categorization tags.")
    featured: bool = Field(False, description="Whether to display on featured grid.")
    url: Optional[str] = Field(None, description="External canonical URL if cross-posted.")
    readTime: Optional[str] = Field(None, description="Estimated reading time.")
    lang: Literal["pt", "en"] = Field("pt", description="Language of publication.")
    content_md: str = Field(..., min_length=10, description="Markdown body of the article.")
    project_id: str = Field("atrium", description="Project identifier (e.g. atrium, darkfac).")


class CaseStudy(BaseModel):
    """Specification for an impact case deliverable (Astro 5 schema compliant)."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(..., min_length=3, description="Title of the case.")
    slug: Optional[str] = Field(None, description="URL slug.")
    era: Literal["bain", "starboard", "via-appia", "darkfac"] = Field(
        ...,
        description="Career era or enterprise organization.",
    )
    tags: List[str] = Field(default_factory=list, description="Taxonomy tags.")
    anchorMetric: str = Field(..., description="Primary KPI name (e.g. 'EBITDA', 'Throughput').")
    anchorValue: Union[str, int, float] = Field(..., description="Quantified value (e.g. '+R$ 140M', '99.9%').")
    period: str = Field(..., description="Time period of engagement (e.g. '2025-2026').")
    featured: bool = Field(False, description="Hero case flag.")
    order: int = Field(1, description="Sort order index.")
    confidentiality: Literal["draft", "pending-rights", "approved"] = Field(
        "approved",
        description="Publication confidentiality clearance.",
    )
    evidence: Optional[List[Dict[str, str]]] = Field(
        default=None,
        description="Verifiable links/citations backing metrics.",
    )
    content_md: str = Field(..., min_length=10, description="Markdown case study body.")
    project_id: str = Field("atrium", description="Project namespace.")


class PublishResult(BaseModel):
    """Result of injecting a post or case into a project's content collection."""

    success: bool
    file_path: str
    collection: Literal["thinking", "cases"]
    title: str
    slop_score: float = 0.0
    error_message: Optional[str] = None


class LeadCapture(BaseModel):
    """Captured inbound lead from contact forms or landing page CTAs."""

    lead_id: str
    project_id: str = "atrium"
    name: str = Field(..., min_length=2)
    email: str = Field(..., min_length=5)
    company: Optional[str] = None
    phone: Optional[str] = None
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    utm_campaign: Optional[str] = None
    notes: Optional[str] = None
    created_at: str = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )


class LeadDeliveryResult(BaseModel):
    """Outcome of storing and dispatching a captured lead."""

    success: bool
    lead_id: str
    stored_locally: bool
    forwarded_to_n8n: bool
    telegram_dispatched: bool
    error_message: Optional[str] = None


class SEOAuditResult(BaseModel):
    """Deterministic SEO and meta tags verification audit result."""

    url_or_path: str
    valid: bool
    score: float = Field(..., ge=0.0, le=100.0)
    title: Optional[str] = None
    meta_description: Optional[str] = None
    og_tags: Dict[str, str] = Field(default_factory=dict)
    canonical_url: Optional[str] = None
    sitemap_found: bool = False
    issues: List[str] = Field(default_factory=list)


class GA4Event(BaseModel):
    """Event payload for Google Analytics 4 Measurement Protocol."""

    event_name: str
    client_id: str
    user_id: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)


class GA4Report(BaseModel):
    """Aggregated traffic and conversion report from Google Analytics 4."""

    project_id: str
    period: str
    sessions: int
    pageviews: int
    users: int
    conversions: int
    bounce_rate: float


class GoogleAdsCampaignMetrics(BaseModel):
    """Metrics snapshot for an individual Google Ads campaign."""

    campaign_id: str
    campaign_name: str
    status: str
    impressions: int
    clicks: int
    ctr: float
    average_cpc_usd: float
    cost_usd: float
    conversions: int


class GoogleAdsReport(BaseModel):
    """Comprehensive performance and budget guardrail report for Google Ads."""

    project_id: str
    total_cost_usd: float
    budget_limit_usd: float
    budget_exceeded: bool
    total_clicks: int
    total_impressions: int
    total_conversions: int
    campaigns: List[GoogleAdsCampaignMetrics] = Field(default_factory=list)
