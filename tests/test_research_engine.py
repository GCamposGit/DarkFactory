"""
Unit & Integration Tests for DarkFac Research Engine.
Verifies models, intent classification, arXiv parsing, GitHub scout scoring, and Knowledge Ledger persistence.
Runs deterministically without network dependencies.
"""

import json
import tempfile
import urllib.error
from pathlib import Path
import pytest

from core.research.models import (
    ResearchTopicType,
    LicenseType,
    AuthorityTier,
    ResearchSource,
    SourceInsight,
    ResearchLedger,
)
from core.research.classifier import (
    classify_research_intent,
    extract_search_keywords,
    detect_programming_language,
)
from core.research.arxiv_client import ArxivClient
from core.research.github_scout import GitHubScout
from core.research.trends_scout import ExpertTrendsScout
from core.research.ledger import KnowledgeLedgerManager
from core.research.transport import (
    ResearchTransport,
    SearchStatus,
    TlsConfigurationError,
    TransportError,
    TransportErrorKind,
    TransportFailure,
    TransportResponse,
    get_ssl_context,
)




# 1. Models & Serialization Tests
def test_models_serialization_and_markdown():
    source = ResearchSource(
        id="arxiv:2301.0001",
        title="Attention Is All You Need Revisited",
        url="https://arxiv.org/abs/2301.0001",
        source_type="paper",
        authors_or_maintainers=["Vaswani et al."],
        published_date="2023-01-01",
        summary="A review of attention mechanisms in software engineering.",
        license="CC-BY-4.0",
        license_category=LicenseType.PERMISSIVE,
        credibility_score=0.98,
        metadata={"category": "cs.SE"}
    )
    assert source.to_dict()["license_category"] == "permissive"

    insight = SourceInsight(
        source_id=source.id,
        source_title=source.title,
        key_insight="Usar multi-head attention em janelas deslizantes.",
        architectural_implications="Implementar buffer circular no worker de streaming.",
        future_reference_value="Serve de baseline para o módulo de embeddings v2.",
        code_patterns_or_algorithms=["SlidingWindowAttention"]
    )

    ledger = ResearchLedger(
        ledger_id="test_ledger",
        query="Attention mechanisms",
        topic_type=ResearchTopicType.TOPIC_CONCEPT,
        summary_executive="Pesquisa sobre atenção para streaming."
    )
    ledger.add_source(source)
    ledger.add_insight(insight)

    # Convert to dict and reload
    d = ledger.to_dict()
    reloaded = ResearchLedger.from_dict(d)
    assert reloaded.ledger_id == "test_ledger"
    assert len(reloaded.sources) == 1
    assert len(reloaded.insights) == 1
    assert reloaded.insights[0].code_patterns_or_algorithms == ["SlidingWindowAttention"]

    # Markdown format check
    md = ledger.to_markdown()
    assert "# Dossiê de Pesquisa & Knowledge Ledger: test_ledger" in md
    assert "arxiv:2301.0001" in md
    assert "SlidingWindowAttention" in md
    assert "Usar multi-head attention" in md


# 2. Classifier Tests
@pytest.mark.parametrize("query,expected_type", [
    ("Quais as melhores práticas e arquitetura para replicação Raft?", ResearchTopicType.TOPIC_CONCEPT),
    ("Buscar papers recentes no arxiv sobre otimização de context window em LLMs", ResearchTopicType.TOPIC_CONCEPT),
    ("Qual o estado da arte e teoria de transações distribuídas 2PC?", ResearchTopicType.TOPIC_CONCEPT),
    ("Procurar repositório com código já escrito de cliente Redis com pool", ResearchTopicType.CODE_REUSE),
    ("Biblioteca pronta python com testes para validação de CPF e CNPJ não reinventar a roda", ResearchTopicType.CODE_REUSE),
    ("Reaproveitar componentes testados de auth JWT fastapi no github", ResearchTopicType.CODE_REUSE),
])
def test_classifier_intent(query, expected_type):
    result = classify_research_intent(query)
    assert result.topic_type == expected_type
    assert result.confidence >= 0.5


def test_classifier_explicit_override():
    res_code = classify_research_intent("pesquisa sobre papers de IA", explicit_override="code")
    assert res_code.topic_type == ResearchTopicType.CODE_REUSE

    res_topic = classify_research_intent("repositório github com lib", explicit_override="concept")
    assert res_topic.topic_type == ResearchTopicType.TOPIC_CONCEPT


def test_keyword_extraction_and_language_detection():
    query_complex = "Procurar repositório com código já existente para voice activity detection em Python"
    lang = detect_programming_language(query_complex)
    assert lang == "python"

    keywords = extract_search_keywords(query_complex)
    assert "voice" in keywords
    assert "activity" in keywords
    assert "detection" in keywords
    # Stopwords should be stripped
    assert "procurar" not in keywords
    assert "repositório" not in keywords



# 3. Arxiv Client Parsing (Offline XML Fixture)
def test_arxiv_atom_feed_parser():
    atom_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>http://arxiv.org/abs/2405.12345v1</id>
        <published>2024-05-15T10:00:00Z</published>
        <title>Deterministic Validation in Autonomous Software Engineering</title>
        <summary>This paper proposes a 5-tier test harness for self-coding agents.</summary>
        <author>
          <name>Alice Smith</name>
        </author>
        <author>
          <name>Bob Jones</name>
        </author>
      </entry>
    </feed>
    """
    client = ArxivClient()
    sources = client._parse_atom_feed(atom_xml)
    assert len(sources) == 1
    s = sources[0]
    assert s.id == "arxiv:2405.12345v1"
    assert s.title == "Deterministic Validation in Autonomous Software Engineering"
    assert s.source_type == "paper"
    assert s.authors_or_maintainers == ["Alice Smith", "Bob Jones"]
    assert "Alice Smith" in s.authors_or_maintainers
    assert s.license_category == LicenseType.PERMISSIVE


class _FakeResearchTransport:
    def __init__(self, response=None, failure=None):
        self.response = response
        self.failure = failure
        self.calls = []

    def get(self, url, *, headers=None):
        self.calls.append((url, headers or {}))
        if self.failure is not None:
            raise TransportError(self.failure)
        return self.response


def test_research_clients_distinguish_empty_results_from_transport_failures():
    empty_feed = b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    transport = _FakeResearchTransport(TransportResponse(200, empty_feed))
    result = ArxivClient(transport=transport).search_papers("tls transport")

    assert result.status == SearchStatus.EMPTY
    assert result.is_empty
    assert result.failure is None
    assert transport.calls[0][0].startswith("https://export.arxiv.org/")

    failed_transport = _FakeResearchTransport(
        failure=TransportFailure(
            TransportErrorKind.TIMEOUT,
            "remote service request timed out",
            retryable=True,
        )
    )
    failed = ArxivClient(transport=failed_transport).search_papers("tls transport")

    assert failed.status == SearchStatus.FAILED
    assert failed.failure is not None
    assert failed.failure.kind == TransportErrorKind.TIMEOUT
    assert failed.failure.retryable is True


def test_research_transport_structured_failure_categories(monkeypatch):
    monkeypatch.setattr(
        "core.research.transport.get_ssl_context",
        lambda _ca_bundle=None: object(),
    )

    def raise_timeout(*_args, **_kwargs):
        raise urllib.error.URLError(TimeoutError("internal detail"))

    monkeypatch.setattr("core.research.transport.urllib.request.urlopen", raise_timeout)
    with pytest.raises(TransportError) as timeout_error:
        ResearchTransport().get("https://example.test/search")
    assert timeout_error.value.failure.kind == TransportErrorKind.TIMEOUT
    assert "internal detail" not in str(timeout_error.value)

    def raise_http_error(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://example.test/search?token=secret",
            503,
            "server detail",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr("core.research.transport.urllib.request.urlopen", raise_http_error)
    with pytest.raises(TransportError) as http_error:
        ResearchTransport().get("https://example.test/search?token=secret")
    assert http_error.value.failure.kind == TransportErrorKind.HTTP_STATUS
    assert http_error.value.failure.status_code == 503
    assert "secret" not in str(http_error.value)
    assert "secret" not in repr(http_error.value.failure)

    class InvalidStatusResponse:
        status = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return "200"

    monkeypatch.setattr(
        "core.research.transport.urllib.request.urlopen",
        lambda *_args, **_kwargs: InvalidStatusResponse(),
    )
    with pytest.raises(TransportError) as invalid_response_error:
        ResearchTransport().get("https://example.test/search")
    assert invalid_response_error.value.failure.kind == TransportErrorKind.INVALID_RESPONSE


def test_research_transport_rejects_empty_body_and_unverified_fallback(monkeypatch):
    monkeypatch.setattr(
        "core.research.transport.get_ssl_context",
        lambda _ca_bundle=None: object(),
    )

    class EmptyResponse:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b""

    monkeypatch.setattr(
        "core.research.transport.urllib.request.urlopen",
        lambda *_args, **_kwargs: EmptyResponse(),
    )
    with pytest.raises(TransportError) as empty_error:
        ResearchTransport().get("https://example.test/search")
    assert empty_error.value.failure.kind == TransportErrorKind.EMPTY_RESPONSE

    with tempfile.TemporaryDirectory() as tmpdir:
        missing_bundle = Path(tmpdir) / "missing-ca.pem"
        with pytest.raises(TlsConfigurationError) as tls_error:
            get_ssl_context(missing_bundle)
    assert tls_error.value.failure.kind == TransportErrorKind.TLS_ERROR
    assert "unverified" not in str(tls_error.value).lower()


def test_github_search_does_not_expose_token_on_failure():
    token = "ghp-secret-token"
    transport = _FakeResearchTransport(
        failure=TransportFailure(
            TransportErrorKind.TRANSPORT_ERROR,
            "remote service transport failed",
            retryable=True,
        )
    )
    result = GitHubScout(token=token, transport=transport).search_repositories("secure search")

    assert result.status == SearchStatus.FAILED
    assert result.failure is not None
    assert token not in repr(result.failure)
    assert token not in str(result.failure.message)


def test_github_search_marks_malformed_payload_as_invalid_response():
    transport = _FakeResearchTransport(TransportResponse(200, b"not-json"))
    result = GitHubScout(transport=transport).search_repositories("secure search")

    assert result.status == SearchStatus.FAILED
    assert result.failure is not None
    assert result.failure.kind == TransportErrorKind.INVALID_RESPONSE


# 4. GitHub Scout Quality and Licensing
def test_github_scout_license_and_scoring():
    scout = GitHubScout()

    # Permissive licenses
    assert scout.classify_license("MIT") == LicenseType.PERMISSIVE
    assert scout.classify_license("Apache-2.0") == LicenseType.PERMISSIVE
    assert scout.classify_license("BSD-3-Clause") == LicenseType.PERMISSIVE

    # Copyleft licenses
    assert scout.classify_license("GPL-3.0") == LicenseType.COPYLEFT
    assert scout.classify_license("AGPL-3.0") == LicenseType.COPYLEFT

    # Unknown / none
    assert scout.classify_license(None) == LicenseType.UNKNOWN

    # Quality scoring
    score_high = scout.calculate_quality_score(
        stars=1500,
        license_category=LicenseType.PERMISSIVE,
        has_tests=True,
        is_archived=False
    )
    assert score_high >= 0.9

    score_archived = scout.calculate_quality_score(
        stars=2000,
        license_category=LicenseType.PERMISSIVE,
        has_tests=True,
        is_archived=True
    )
    assert score_archived == 0.2


def test_github_scout_item_processing():
    scout = GitHubScout()
    mock_items = [
        {
            "full_name": "awesome-org/test-runner",
            "description": "Blazing fast deterministic test runner",
            "html_url": "https://github.com/awesome-org/test-runner",
            "stargazers_count": 850,
            "archived": False,
            "pushed_at": "2026-08-01T00:00:00Z",
            "license": {"spdx_id": "MIT", "name": "MIT License"},
            "owner": {"login": "awesome-org"},
            "topics": ["testing", "python"],
            "language": "Python"
        },
        {
            "full_name": "legacy/copyleft-tool",
            "description": "Strict GPL tool",
            "html_url": "https://github.com/legacy/copyleft-tool",
            "stargazers_count": 50,
            "archived": False,
            "pushed_at": "2026-05-01T00:00:00Z",
            "license": {"spdx_id": "GPL-3.0", "name": "GNU General Public License v3.0"},
            "owner": {"login": "legacy"},
            "topics": [],
            "language": "C"
        }
    ]

    # Process all
    sources = scout._process_repo_items(mock_items, permissive_only=False, limit=5)
    assert len(sources) == 2
    assert sources[0].id == "gh:awesome-org/test-runner"
    assert sources[0].license_category == LicenseType.PERMISSIVE
    assert sources[1].license_category == LicenseType.COPYLEFT

    # Permissive only
    perm_sources = scout._process_repo_items(mock_items, permissive_only=True, limit=5)
    assert len(perm_sources) == 1
    assert perm_sources[0].id == "gh:awesome-org/test-runner"


# 5. Knowledge & Insight Ledger Persistence
def test_knowledge_ledger_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        mgr = KnowledgeLedgerManager(base_dir=base_dir)

        ledger = mgr.create_ledger(
            query="Otimização de Transcrição Whisper",
            topic_type=ResearchTopicType.CODE_REUSE,
            ledger_id="whisper_opt"
        )
        ledger.add_source(ResearchSource(
            id="gh:SYSTRAN/faster-whisper",
            title="SYSTRAN/faster-whisper",
            url="https://github.com/SYSTRAN/faster-whisper",
            source_type="repository",
            license="MIT",
            license_category=LicenseType.PERMISSIVE,
            stars=12000
        ))
        ledger.add_insight(SourceInsight(
            source_id="gh:SYSTRAN/faster-whisper",
            source_title="SYSTRAN/faster-whisper",
            key_insight="CTranslate2 reduz consumo de VRAM em 4x.",
            architectural_implications="Permite rodar whisper grande em paralelo com LLM local.",
            future_reference_value="Base para escalonamento multi-stream.",
            code_patterns_or_algorithms=["CTranslate2", "float16"]
        ))

        saved_path = mgr.save_ledger(ledger)
        assert saved_path.exists()
        assert (saved_path / "ledger.json").exists()
        assert (saved_path / "INSIGHTS.md").exists()

        # Check content of JSON
        with open(saved_path / "ledger.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            assert data["ledger_id"] == "whisper_opt"
            assert data["topic_type"] == "code_reuse"

        # Check content of Markdown
        with open(saved_path / "INSIGHTS.md", "r", encoding="utf-8") as f:
            md_content = f.read()
            assert "CTranslate2 reduz consumo" in md_content
            assert "SYSTRAN/faster-whisper" in md_content

        # Reload from manager
        loaded = mgr.load_ledger("whisper_opt")
        assert loaded is not None
        assert loaded.query == "Otimização de Transcrição Whisper"
        assert len(loaded.sources) == 1
        assert len(loaded.insights) == 1

        # List ledgers
        ledger_list = mgr.list_ledgers()
        assert len(ledger_list) == 1
        assert ledger_list[0]["ledger_id"] == "whisper_opt"
        assert ledger_list[0]["sources_count"] == 1

        # Header reference generator
        header = mgr.generate_code_header_reference("whisper_opt")
        assert "Ledger ID: whisper_opt" in header
        assert ".factory/research/whisper_opt/INSIGHTS.md" in header


# 6. Expert Trends & Multi-Tier Authority Tests
def test_expert_trends_scout_and_authority_tiers():
    scout = ExpertTrendsScout()
    mock_hits = [
        {
            "objectID": "99901",
            "title": "Show HN: Ultra-fast local ASR with WebGPU",
            "url": "https://example.com/webgpu-asr",
            "author": "alex_expert",
            "points": 185,
            "num_comments": 42,
            "created_at": "2026-09-01T12:00:00Z"
        }
    ]

    sources = scout._process_trend_hits(mock_hits, limit=2)
    assert len(sources) == 1
    s = sources[0]
    assert s.id == "trend:hn:99901"
    assert s.authority_tier == AuthorityTier.TREND_SIGNAL
    assert s.authors_or_maintainers == ["alex_expert"]
    assert s.credibility_score == 0.75

    insights = scout.generate_trend_insights(sources)
    assert len(insights) == 1
    ins = insights[0]
    assert ins.authority_tier == AuthorityTier.TREND_SIGNAL
    assert "IDEIA DE FEATURE" in ins.architectural_implications
    assert "DEVEM ser fundamentados nas fontes canônicas" in ins.architectural_implications

    # Test multi-tier rendering in Ledger
    ledger = ResearchLedger(
        ledger_id="multi_tier_test",
        query="Audio ASR streaming",
        topic_type=ResearchTopicType.TOPIC_CONCEPT,
        summary_executive="Pesquisa multi-tier combinando papers e tendências."
    )
    # High credibility source
    ledger.add_source(ResearchSource(
        id="arxiv:2401.0001",
        title="Streaming ASR Formulations",
        url="https://arxiv.org/abs/2401.0001",
        source_type="paper",
        authority_tier=AuthorityTier.HIGH_CREDIBILITY,
        credibility_score=0.95
    ))
    # Trend source
    ledger.add_source(s)
    ledger.add_insight(ins)

    md = ledger.to_markdown()
    assert "## 2. Fontes de Alta Credibilidade (Papers, RFCs, Repositórios Testados)" in md
    assert "## 3. Radar de Tendências & Experts da Comunidade (Ideação de Features)" in md
    assert "🔥 [TENDÊNCIA / EXPERT - IDEAÇÃO DE FEATURE]" in md
    assert "Diretriz DarkFac" in md
