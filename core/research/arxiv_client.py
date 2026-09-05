"""
ArXiv Academic Client.
Searches the public arXiv API for recent scientific papers, architectures, and theoretical foundations.
Uses Python's standard library (urllib + xml.etree.ElementTree) for zero-dependency reliability.
"""

import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from typing import List, Optional
from datetime import datetime

from core.research.models import ResearchSource, LicenseType

ARXIV_API_URL = "http://export.arxiv.org/api/query"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def get_ssl_context():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            return ssl.create_default_context()
        except Exception:
            return ssl._create_unverified_context()


class ArxivClient:
    """Headless client for querying scientific preprints on arXiv."""

    def __init__(self, timeout_sec: int = 15):
        self.timeout_sec = timeout_sec

    def search_papers(self, query: str, max_results: int = 5) -> List[ResearchSource]:
        """
        Queries arXiv for papers matching the terms.
        Returns a list of structured ResearchSource objects.
        """
        clean_query = query.strip()
        if not clean_query:
            return []

        # Formulate arXiv query string
        params = {
            "search_query": f"all:{clean_query}",
            "start": 0,
            "max_results": max_results,
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        url = f"{ARXIV_API_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "DarkFac-ResearchEngine/1.0 (Autonomous-Agent-Pipeline)"}
        )

        try:
            ctx = get_ssl_context()
            with urllib.request.urlopen(req, timeout=self.timeout_sec, context=ctx) as resp:
                xml_data = resp.read()
                return self._parse_atom_feed(xml_data)
        except Exception as e:
            # Resilient fallback: return empty list on network or parsing failure
            print(f"[WARN] arXiv search failed for '{query}': {e}")
            return []



    def _parse_atom_feed(self, xml_bytes: bytes) -> List[ResearchSource]:
        """Parses Atom XML feed returned by arXiv API."""
        sources: List[ResearchSource] = []
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError:
            return sources

        for entry in root.findall("atom:entry", ATOM_NS):
            id_elem = entry.find("atom:id", ATOM_NS)
            raw_id = id_elem.text.strip() if id_elem is not None and id_elem.text else ""
            arxiv_id = raw_id.split("/abs/")[-1] if "/abs/" in raw_id else raw_id

            title_elem = entry.find("atom:title", ATOM_NS)
            title = " ".join(title_elem.text.split()) if title_elem is not None and title_elem.text else "Untitled Paper"

            summary_elem = entry.find("atom:summary", ATOM_NS)
            summary = " ".join(summary_elem.text.split()) if summary_elem is not None and summary_elem.text else ""

            published_elem = entry.find("atom:published", ATOM_NS)
            published = published_elem.text.strip() if published_elem is not None and published_elem.text else None

            # Authors
            authors: List[str] = []
            for author in entry.findall("atom:author", ATOM_NS):
                name_elem = author.find("atom:name", ATOM_NS)
                if name_elem is not None and name_elem.text:
                    authors.append(name_elem.text.strip())

            # URL
            url = raw_id if raw_id.startswith("http") else f"https://arxiv.org/abs/{arxiv_id}"

            source = ResearchSource(
                id=f"arxiv:{arxiv_id}",
                title=title,
                url=url,
                source_type="paper",
                authors_or_maintainers=authors,
                published_date=published,
                summary=summary,
                license="arXiv Open Access / Creative Commons",
                license_category=LicenseType.PERMISSIVE,
                credibility_score=0.95,
                metadata={"arxiv_id": arxiv_id}
            )
            sources.append(source)

        return sources
