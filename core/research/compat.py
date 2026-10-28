"""Compatibility normalization for historical Knowledge Ledger records."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from core.owner_actions.models import redact
from core.research.models import (
    AuthorityTier,
    LicenseType,
    ResearchLedger,
    ResearchSource,
    ResearchTopicType,
    SourceInsight,
)


class ResearchLedgerSchemaError(ValueError):
    """A ledger record contains an unsupported or malformed schema value."""

    def __init__(self, ledger_id: str, field: str, reason: str, value: Any = None):
        safe_value = redact(str(value)) if value is not None else ""
        # URLs can contain private paths, query parameters, and credentials. Do not
        # expose a complete URL in an exception even when it has no known token shape.
        safe_value = re.sub(r"https?://[^\s\]\[,)]+", "[URL]", safe_value, flags=re.IGNORECASE)
        safe_ledger_id = redact(ledger_id)
        safe_ledger_id = re.sub(r"https?://[^\s\]\[,)]+", "[URL]", safe_ledger_id, flags=re.IGNORECASE)
        detail = f" (value: {safe_value})" if safe_value else ""
        self.ledger_id = ledger_id
        self.field = field
        self.reason = reason
        super().__init__(f"Invalid research ledger {safe_ledger_id!r}, field {field!r}: {reason}{detail}")


_AUTHORITY_ALIASES = {
    "primary": AuthorityTier.HIGH_CREDIBILITY,
    "official": AuthorityTier.HIGH_CREDIBILITY,
    "canonical": AuthorityTier.HIGH_CREDIBILITY,
    "documentation": AuthorityTier.HIGH_CREDIBILITY,
    "high_credibility": AuthorityTier.HIGH_CREDIBILITY,
    "secondary": AuthorityTier.TREND_SIGNAL,
    "community": AuthorityTier.TREND_SIGNAL,
    "trend": AuthorityTier.TREND_SIGNAL,
    "social": AuthorityTier.TREND_SIGNAL,
    "trend_signal": AuthorityTier.TREND_SIGNAL,
}

_LEDGER_FIELDS = {
    "ledger_id", "query", "question", "topic_type", "created_at", "consulted_on",
    "sources", "insights", "summary_executive", "decisions_linked", "related_tickets",
    "provenance",
}
_SOURCE_FIELDS = {
    "id", "title", "url", "canonical_url", "source_type", "application", "insight",
    "organization", "consulted_at", "accessed_on", "authors_or_maintainers",
    "published_date", "summary", "license", "license_category", "authority_tier",
    "credibility_score", "stars", "has_test_suite", "metadata",
}


def _authority(value: Any, ledger_id: str, field: str) -> AuthorityTier:
    if isinstance(value, AuthorityTier):
        return value
    if not isinstance(value, str) or value.strip().lower() not in _AUTHORITY_ALIASES:
        raise ResearchLedgerSchemaError(ledger_id, field, "unknown authority tier", value)
    return _AUTHORITY_ALIASES[value.strip().lower()]


def _source(source_data: Any, ledger_id: str, index: int) -> ResearchSource:
    field_prefix = f"sources[{index}]"
    if not isinstance(source_data, dict):
        raise ResearchLedgerSchemaError(ledger_id, field_prefix, "source must be an object", source_data)

    source = dict(source_data)
    url = source.get("url") or source.get("canonical_url")
    fallback = f"source-{index + 1}"
    if not isinstance(url, str) or not url.strip():
        url = f"urn:research-source:{index + 1}"
    url = url.strip()

    source_id = source.get("id")
    if not isinstance(source_id, str) or not source_id.strip():
        parsed = urlparse(url)
        candidate = (parsed.netloc + parsed.path).strip("/") or fallback
        source_id = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate).strip("-") or fallback

    title = source.get("title")
    if not isinstance(title, str) or not title.strip():
        parsed = urlparse(url)
        title = (parsed.path.rstrip("/").split("/")[-1] or parsed.netloc or fallback).replace("-", " ")

    metadata = dict(source.get("metadata") or {})
    accessed_at = source.get("consulted_at") or source.get("accessed_on")
    if accessed_at:
        metadata.setdefault("accessed_at", accessed_at)
    for key, value in source.items():
        if key not in _SOURCE_FIELDS:
            metadata.setdefault(key, value)
    if source.get("organization"):
        metadata.setdefault("organization", source["organization"])

    tier = _authority(source.get("authority_tier", "high_credibility"), ledger_id, f"{field_prefix}.authority_tier")
    return ResearchSource(
        id=source_id.strip(),
        title=title.strip(),
        url=url,
        source_type=str(source.get("source_type") or "unknown"),
        authors_or_maintainers=list(source.get("authors_or_maintainers") or ([source["organization"]] if source.get("organization") else [])),
        published_date=source.get("published_date"),
        summary=str(source.get("summary") or source.get("application") or source.get("insight") or ""),
        license=source.get("license"),
        license_category=LicenseType(source.get("license_category", "unknown")),
        authority_tier=tier,
        credibility_score=float(source.get("credibility_score", 1.0)),
        stars=source.get("stars"),
        has_test_suite=source.get("has_test_suite"),
        metadata=metadata,
    )


def normalize_ledger(data: dict[str, Any], requested_ledger_id: str = "") -> ResearchLedger:
    """Convert known historical ledger variants into the canonical domain model."""
    ledger_id = str(data.get("ledger_id") or requested_ledger_id or "unknown")
    query = data.get("query") or data.get("question") or ledger_id
    try:
        topic_type = ResearchTopicType(data.get("topic_type", ResearchTopicType.TOPIC_CONCEPT.value))
    except (TypeError, ValueError):
        raise ResearchLedgerSchemaError(ledger_id, "topic_type", "unknown topic type", data.get("topic_type")) from None

    sources_data = data.get("sources", [])
    if not isinstance(sources_data, list):
        raise ResearchLedgerSchemaError(ledger_id, "sources", "sources must be a list", sources_data)
    sources = [_source(item, ledger_id, index) for index, item in enumerate(sources_data)]

    insights_data = data.get("insights", [])
    insights: list[SourceInsight] = []
    if not isinstance(insights_data, list):
        raise ResearchLedgerSchemaError(ledger_id, "insights", "insights must be a list", insights_data)
    for index, item in enumerate(insights_data):
        if not isinstance(item, dict):
            raise ResearchLedgerSchemaError(ledger_id, f"insights[{index}]", "insight must be an object", item)
        insight = dict(item)
        insight["authority_tier"] = _authority(insight.get("authority_tier", "high_credibility"), ledger_id, f"insights[{index}].authority_tier")
        try:
            insights.append(SourceInsight.from_dict(insight))
        except (TypeError, ValueError, KeyError) as exc:
            raise ResearchLedgerSchemaError(ledger_id, f"insights[{index}]", "malformed insight record", type(exc).__name__) from None

    provenance = dict(data.get("provenance") or {})
    for key, value in data.items():
        if key not in _LEDGER_FIELDS:
            provenance.setdefault(key, value)
    return ResearchLedger(
        ledger_id=ledger_id,
        query=str(query),
        topic_type=topic_type,
        created_at=str(data.get("created_at") or data.get("consulted_on") or ""),
        sources=sources,
        insights=insights,
        summary_executive=str(data.get("summary_executive") or ""),
        decisions_linked=list(data.get("decisions_linked") or []),
        related_tickets=list(data.get("related_tickets") or []),
        provenance=provenance,
    )
