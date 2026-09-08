"""
ArXiv Academic Client.
Searches the public arXiv API for recent scientific papers, architectures, and theoretical foundations.
Uses Python's standard library (urllib + xml.etree.ElementTree) for zero-dependency reliability.
"""

import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List

from core.research.models import ResearchSource, LicenseType
from core.research.transport import (
    ResearchSearchResult,
    ResearchTransport,
    TransportError,
    TransportErrorKind,
    TransportFailure,
    get_ssl_context,
)

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


class ArxivClient:
    """Headless client for querying scientific preprints on arXiv."""

    def __init__(
        self,
        timeout_sec: float = 15,
        *,
        transport: ResearchTransport | None = None,
        ca_bundle: str | Path | None = None,
    ):
        self.timeout_sec = timeout_sec
        self.transport = transport or ResearchTransport(
            timeout_sec=timeout_sec,
            ca_bundle=ca_bundle,
        )

    def search_papers(self, query: str, max_results: int = 5) -> ResearchSearchResult[ResearchSource]:
        """
        Queries arXiv for papers matching the terms.
        Returns a list-compatible result with structured empty/failure status.
        """
        clean_query = query.strip()
        if not clean_query:
            return ResearchSearchResult.empty()

        # Formulate arXiv query string
        params = {
            "search_query": f"all:{clean_query}",
            "start": 0,
            "max_results": max_results,
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        url = f"{ARXIV_API_URL}?{urllib.parse.urlencode(params)}"
        try:
            response = self.transport.get(
                url,
                headers={"User-Agent": "DarkFac-ResearchEngine/1.0 (Autonomous-Agent-Pipeline)"},
            )
            sources = self._parse_atom_feed(response.body)
        except TransportError as exc:
            return ResearchSearchResult.failed(exc.failure)
        except ET.ParseError:
            return ResearchSearchResult.failed(
                TransportFailure(
                    TransportErrorKind.INVALID_RESPONSE,
                    "arXiv returned malformed Atom XML",
                )
            )

        return ResearchSearchResult(sources)

    def _parse_atom_feed(self, xml_bytes: bytes) -> List[ResearchSource]:
        """Parses Atom XML feed returned by arXiv API."""
        root = ET.fromstring(xml_bytes)
        if root.tag != "{http://www.w3.org/2005/Atom}feed":
            raise ET.ParseError("unexpected Atom root")

        sources: List[ResearchSource] = []

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
