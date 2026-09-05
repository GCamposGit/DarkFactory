"""
Expert & Community Trends Scout.
Captures market pulse, trending tool releases, and expert discussions from top engineering leaders.
Used strictly for expanding scope and feature ideation, with technical details validated against high-credibility sources.
"""

import json
import urllib.request
import urllib.parse
from typing import List, Dict, Any, Optional

from core.research.models import ResearchSource, SourceInsight, AuthorityTier, LicenseType
from core.research.github_scout import get_ssl_context

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"


class ExpertTrendsScout:
    """Headless scout for developer trends, expert discussions, and viral tools."""

    def __init__(self, timeout_sec: int = 15):
        self.timeout_sec = timeout_sec

    def search_trends(self, query: str, limit: int = 3) -> List[ResearchSource]:
        """
        Searches tech discussions and expert releases on the topic.
        Returns a list of trend signal sources.
        """
        clean_query = query.strip()
        if not clean_query:
            return []

        params = {
            "query": clean_query,
            "tags": "story",
            "hitsPerPage": limit,
        }
        url = f"{HN_SEARCH_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "DarkFac-ResearchEngine/1.0 (Autonomous-Agent-Pipeline)"}
        )

        try:
            ctx = get_ssl_context()
            with urllib.request.urlopen(req, timeout=self.timeout_sec, context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                hits = data.get("hits", [])
                return self._process_trend_hits(hits, limit=limit)
        except Exception as e:
            print(f"[WARN] Trends scout search failed for '{query}': {e}")
            return []

    def _process_trend_hits(self, hits: List[Dict[str, Any]], limit: int) -> List[ResearchSource]:
        sources: List[ResearchSource] = []
        for hit in hits:
            if len(sources) >= limit:
                break

            obj_id = hit.get("objectID", "")
            title = hit.get("title") or "Untitled Tech Discussion"
            url = hit.get("url") or f"https://news.ycombinator.com/item?id={obj_id}"
            author = hit.get("author") or "Community Expert"
            points = hit.get("points", 0)
            comments = hit.get("num_comments", 0)
            created_at = hit.get("created_at")

            source = ResearchSource(
                id=f"trend:hn:{obj_id}",
                title=title,
                url=url,
                source_type="expert_trend",
                authors_or_maintainers=[author],
                published_date=created_at,
                summary=f"Discussão de engenharia com alta repercussão ({points} pontos, {comments} comentários). Foco em ferramentas modernas e adoção por desenvolvedores.",
                license="N/A (Trend Discussion)",
                license_category=LicenseType.UNKNOWN,
                authority_tier=AuthorityTier.TREND_SIGNAL,
                credibility_score=0.75,
                metadata={
                    "points": points,
                    "comments": comments,
                    "community": "HackerNews / Elite Developers",
                }
            )
            sources.append(source)

        return sources

    def generate_trend_insights(self, sources: List[ResearchSource]) -> List[SourceInsight]:
        """Creates ideation-focused insights with explicit validation requirements."""
        insights: List[SourceInsight] = []
        for s in sources:
            insight = SourceInsight(
                source_id=s.id,
                source_title=s.title,
                key_insight=(
                    f"Recurso/discussão com forte tração na comunidade: '{s.title}'. "
                    f"Engenheiros e experts estão priorizando esta abordagem para ganho de ergonomia e velocidade."
                ),
                architectural_implications=(
                    "IDEIA DE FEATURE: Avaliar a inclusão dessa funcionalidade no roadmap. "
                    "ATENÇÃO: A arquitetura concreta, schemas e testes DEVEM ser fundamentados nas fontes canônicas de alta credibilidade."
                ),
                future_reference_value=(
                    f"Sinal de mercado e preferência de desenvolvedores documentado em {s.url}."
                ),
                authority_tier=AuthorityTier.TREND_SIGNAL,
                code_patterns_or_algorithms=[s.title[:40]]
            )
            insights.append(insight)
        return insights
