"""
GitHub Code Scout.
Mines and evaluates open-source repositories to promote component reuse and avoid reinventing the wheel.
Filters by license permissiveness, star count, recent activity, and test coverage indicators.
"""

import os
import json
import urllib.request
import urllib.parse
from typing import List, Dict, Any, Optional
from datetime import datetime

from core.research.models import ResearchSource, LicenseType

GITHUB_API_URL = "https://api.github.com"

PERMISSIVE_LICENSES = {
    "mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "unlicense", "0bsd", "cc0-1.0"
}
COPYLEFT_LICENSES = {
    "gpl-2.0", "gpl-3.0", "agpl-3.0", "lgpl-2.1", "lgpl-3.0", "mpl-2.0", "epl-2.0"
}


def get_ssl_context():
    """Returns a secure SSL context with certifi, or fallback."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            return ssl.create_default_context()
        except Exception:
            return ssl._create_unverified_context()


class GitHubScout:
    """Headless scout for open-source codebases and reusable components."""

    def __init__(self, token: Optional[str] = None, timeout_sec: int = 15):
        self.token = token or os.environ.get("GITHUB_TOKEN")
        self.timeout_sec = timeout_sec

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "DarkFac-ResearchEngine/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def classify_license(self, license_spdx: Optional[str]) -> LicenseType:
        """Determines if the license is permissive, copyleft, or unknown."""
        if not license_spdx:
            return LicenseType.UNKNOWN
        clean_lic = license_spdx.strip().lower()
        if clean_lic in PERMISSIVE_LICENSES:
            return LicenseType.PERMISSIVE
        elif clean_lic in COPYLEFT_LICENSES:
            return LicenseType.COPYLEFT
        return LicenseType.UNKNOWN

    def calculate_quality_score(
        self,
        stars: int,
        license_category: LicenseType,
        has_tests: bool,
        is_archived: bool
    ) -> float:
        """Calculates a credibility score from 0.0 to 1.0."""
        if is_archived:
            return 0.2

        score = 0.4  # Base score
        # Stars contribution (up to +0.3)
        if stars >= 1000:
            score += 0.3
        elif stars >= 200:
            score += 0.2
        elif stars >= 50:
            score += 0.1

        # License contribution (+0.2 for permissive)
        if license_category == LicenseType.PERMISSIVE:
            score += 0.2
        elif license_category == LicenseType.COPYLEFT:
            score += 0.05

        # Test suite presence (+0.1)
        if has_tests:
            score += 0.1

        return round(min(1.0, score), 2)

    def search_repositories(
        self,
        query: str,
        language: Optional[str] = None,
        min_stars: int = 30,
        limit: int = 5,
        permissive_only: bool = False,
    ) -> List[ResearchSource]:
        """
        Searches GitHub for top repositories matching the criteria.
        """
        clean_query = query.strip()
        if not clean_query:
            return []

        search_terms = [clean_query]
        if language:
            search_terms.append(f"language:{language}")
        if min_stars > 0:
            search_terms.append(f"stars:>={min_stars}")

        full_q = " ".join(search_terms)
        params = {
            "q": full_q,
            "sort": "stars",
            "order": "desc",
            "per_page": limit * 2 if permissive_only else limit,
        }
        url = f"{GITHUB_API_URL}/search/repositories?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=self._get_headers())

        try:
            ctx = get_ssl_context()
            with urllib.request.urlopen(req, timeout=self.timeout_sec, context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                items = data.get("items", [])
                return self._process_repo_items(items, permissive_only=permissive_only, limit=limit)
        except Exception as e:
            print(f"[WARN] GitHub repository search failed for '{query}': {e}")
            return []

    def _process_repo_items(
        self, items: List[Dict[str, Any]], permissive_only: bool, limit: int
    ) -> List[ResearchSource]:
        sources: List[ResearchSource] = []

        for item in items:
            if len(sources) >= limit:
                break

            full_name = item.get("full_name", "")
            description = item.get("description") or "Sem descrição fornecida."
            html_url = item.get("html_url", "")
            stars = item.get("stargazers_count", 0)
            is_archived = item.get("archived", False)
            pushed_at = item.get("pushed_at")

            lic_obj = item.get("license") or {}
            lic_spdx = lic_obj.get("spdx_id")
            lic_name = lic_obj.get("name") or lic_spdx or "Não especificada"
            lic_category = self.classify_license(lic_spdx)

            if permissive_only and lic_category != LicenseType.PERMISSIVE:
                continue

            owner = item.get("owner", {}).get("login", "")
            authors = [owner] if owner else []

            # Heuristic for test detection in description/topics or default assumption for top repos
            topics = item.get("topics", [])
            has_tests = "tests" in topics or "testing" in topics or stars >= 100

            score = self.calculate_quality_score(
                stars=stars,
                license_category=lic_category,
                has_tests=has_tests,
                is_archived=is_archived
            )

            source = ResearchSource(
                id=f"gh:{full_name}",
                title=full_name,
                url=html_url,
                source_type="repository",
                authors_or_maintainers=authors,
                published_date=pushed_at,
                summary=description,
                license=lic_name,
                license_category=lic_category,
                credibility_score=score,
                stars=stars,
                has_test_suite=has_tests,
                metadata={
                    "language": item.get("language"),
                    "forks": item.get("forks_count", 0),
                    "open_issues": item.get("open_issues_count", 0),
                    "topics": topics,
                    "archived": is_archived,
                }
            )
            sources.append(source)

        return sources
