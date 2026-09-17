"""Comprehensive test suite for Enterprise Marketing, CMS, CRM, SEO, and Google Ads (HF-21).

Governed by Universal Engineering Standards (AGENTS.md).
Validates deterministic behavior, Astro 5 compatibility, anti-slop enforcement,
SQLite persistence, n8n/Telegram dispatch resilience, and budget guardrails.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from core.integrations.n8n import N8nApiClient, N8nApiResult, N8nConfig
from core.integrations.telegram import TelegramConfig, TelegramGateway
from core.marketing.cli import main as cli_main
from core.marketing.crm_n8n import LeadManager
from core.marketing.google_ads import GoogleAdsManager
from core.marketing.models import (
    BlogPost,
    CaseStudy,
    GA4Event,
    GoogleAdsCampaignMetrics,
    LeadCapture,
)
from core.marketing.publisher import ContentPublisher
from core.marketing.seo_analytics import GA4Client, SEOValidator


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture
def temp_site_dir(tmp_path: Path) -> Path:
    site_dir = tmp_path / "test_site"
    (site_dir / "src" / "content" / "thinking").mkdir(parents=True, exist_ok=True)
    (site_dir / "src" / "content" / "cases").mkdir(parents=True, exist_ok=True)
    return site_dir


@pytest.fixture
def temp_leads_db(tmp_path: Path) -> Path:
    return tmp_path / "test_leads.db"


@pytest.fixture
def mock_n8n_client() -> N8nApiClient:
    def fake_http(method: str, url: str, headers: Dict[str, str], body: Any, timeout: float) -> Dict[str, Any]:
        return {"success": True, "status_code": 200, "data": {"message": "Webhook received"}}

    config = N8nConfig(base_url="http://localhost:5678", webhook_url="http://localhost:5678/webhook/lead-capture")
    return N8nApiClient(config=config, http_client=fake_http)


@pytest.fixture
def mock_telegram_gateway(tmp_path: Path) -> TelegramGateway:
    cfg = TelegramConfig(bot_token="test_token", authorized_chat_ids=[12345678])
    gw = TelegramGateway(config=cfg, state_dir=tmp_path / "tg_state")
    # Stub send_message
    gw.send_message = lambda chat_id, text, buttons=None, parse_mode="HTML": True  # type: ignore
    return gw


# ==============================================================================
# 1. CMS & Astro 5 Publisher Tests
# ==============================================================================


def test_publish_blog_post_clean(temp_site_dir: Path, tmp_path: Path) -> None:
    staging_dir = tmp_path / "staged_posts"
    publisher = ContentPublisher(target_site_dir=temp_site_dir, staging_dir=staging_dir, max_slop_score=15.0)
    post = BlogPost(
        title="Arquitetura de Agentes Autônomos",
        description="Como estruturar um harness determinístico de IA.",
        tags=["ai", "architecture", "automation"],
        date=datetime.date(2026, 9, 17),
        originalYear=2026,
        featured=True,
        lang="pt",
        content_md="Apresentamos a arquitetura do harness determinístico com validação em três etapas.",
    )

    # 1. Gate G1: Without explicit human approval, post must be staged
    result = publisher.publish_blog_post(post, auto_scrub=True, approved=False)
    assert result.success is True
    assert result.status == "staged_pending_approval"
    assert result.requires_human_approval is True
    assert Path(result.file_path).is_file()
    assert Path(result.file_path).parent == staging_dir

    # Staged post is not yet in live collection
    live_file = temp_site_dir / "src" / "content" / "thinking" / "arquitetura-de-agentes-autonomos.md"
    assert not live_file.is_file()

    # 2. Gate G1 Human Approval: Explicit approval promotes post to production
    slug = post.slug or "arquitetura-de-agentes-autonomos"
    app_result = publisher.approve_post(slug=slug, approver="guilherme")
    assert app_result.success is True
    assert app_result.status == "published"
    assert app_result.requires_human_approval is False
    assert app_result.approver == "guilherme"
    assert live_file.is_file()
    assert not Path(result.file_path).is_file()  # Cleaned up from staging

    written_text = live_file.read_text(encoding="utf-8")
    assert 'title: "Arquitetura de Agentes Autônomos"' in written_text
    assert "date: 2026-09-17" in written_text
    assert "featured: true" in written_text
    assert 'lang: "pt"' in written_text
    assert '- "architecture"' in written_text


def test_publish_blog_post_slop_rejection(temp_site_dir: Path) -> None:
    publisher = ContentPublisher(target_site_dir=temp_site_dir, max_slop_score=10.0)
    slop_content = (
        "In today's fast-paced digital world, we delve into the intricate tapestry "
        "and unleash a paradigm shift to revolutionize the industry. Look no further!"
    )
    post = BlogPost(
        title="Slop Heavy Article",
        tags=["slop"],
        content_md=slop_content,
    )

    # With auto_scrub=False, it must be rejected
    result = publisher.publish_blog_post(post, auto_scrub=False)
    assert result.success is False
    assert result.slop_score > 10.0
    assert "buzzwords" in (result.error_message or "").lower()

    # With auto_scrub=True, it scrubs and succeeds
    result_scrubbed = publisher.publish_blog_post(post, auto_scrub=True)
    assert result_scrubbed.success is True
    assert Path(result_scrubbed.file_path).is_file()


def test_publish_case_study(temp_site_dir: Path) -> None:
    publisher = ContentPublisher(target_site_dir=temp_site_dir)
    case = CaseStudy(
        title="Otimização de Supply Chain Logística",
        era="via-appia",
        tags=["operations", "logistics"],
        anchorMetric="EBITDA",
        anchorValue="+R$ 140M",
        period="2025-2026",
        featured=True,
        order=2,
        confidentiality="approved",
        content_md="Reestruturação completa da malha logística com aumento de 22% na disponibilidade.",
    )

    result = publisher.publish_case_study(case)
    assert result.success is True
    assert result.collection == "cases"
    assert Path(result.file_path).is_file()

    written_text = Path(result.file_path).read_text(encoding="utf-8")
    assert 'title: "Otimização de Supply Chain Logística"' in written_text
    assert 'era: "via-appia"' in written_text
    assert 'anchorMetric: "EBITDA"' in written_text
    assert 'anchorValue: "+R$ 140M"' in written_text
    assert 'confidentiality: "approved"' in written_text


# ==============================================================================
# 2. CRM & Lead Capture Tests
# ==============================================================================


def test_lead_capture_persistence_and_dispatch(
    temp_leads_db: Path,
    mock_n8n_client: N8nApiClient,
    mock_telegram_gateway: TelegramGateway,
) -> None:
    manager = LeadManager(
        db_path=temp_leads_db,
        n8n_client=mock_n8n_client,
        telegram_gateway=mock_telegram_gateway,
        telegram_chat_id=12345678,
    )

    lead = LeadCapture(
        lead_id="lead-test-001",
        project_id="atrium",
        name="Carlos Silva",
        email="carlos.silva@empresa.com.br",
        company="Empresa SA",
        phone="+55 11 99999-8888",
        utm_source="linkedin",
        utm_campaign="enterprise_growth",
        notes="Interesse em modernização autônoma de esteira de software.",
    )

    result = manager.capture_lead(lead, forward_n8n=True, notify_telegram=True)
    assert result.success is True
    assert result.stored_locally is True
    assert result.forwarded_to_n8n is True
    assert result.telegram_dispatched is True

    # Check query
    fetched = manager.get_lead("lead-test-001")
    assert fetched is not None
    assert fetched.name == "Carlos Silva"
    assert fetched.company == "Empresa SA"

    all_leads = manager.list_leads(project_id="atrium")
    assert len(all_leads) == 1
    assert all_leads[0].lead_id == "lead-test-001"


def test_lead_capture_resilient_on_external_failure(temp_leads_db: Path) -> None:
    # Manager with failing n8n and unconfigured telegram
    def failing_http(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise ConnectionError("n8n offline")

    failing_n8n = N8nApiClient(
        config=N8nConfig(base_url="http://localhost:5678", webhook_url="http://localhost:5678/webhook/leads"),
        http_client=failing_http,
    )
    unconfigured_tg = TelegramGateway(config=TelegramConfig())

    manager = LeadManager(
        db_path=temp_leads_db,
        n8n_client=failing_n8n,
        telegram_gateway=unconfigured_tg,
    )

    lead = LeadCapture(
        lead_id="lead-fail-002",
        name="Ana Costa",
        email="ana@tech.io",
    )

    result = manager.capture_lead(lead, forward_n8n=True, notify_telegram=True)
    # Must succeed locally despite downstream failure (fail-safe persistence)
    assert result.success is True
    assert result.stored_locally is True
    assert result.forwarded_to_n8n is False
    assert result.telegram_dispatched is False

    saved = manager.get_lead("lead-fail-002")
    assert saved is not None
    assert saved.name == "Ana Costa"


# ==============================================================================
# 3. SEO & OpenGraph Deterministic Validator Tests
# ==============================================================================


def test_seo_validator_valid_html() -> None:
    validator = SEOValidator()
    html = """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="utf-8">
        <title>Arquitetura de Software e Autonomia na Nuvem</title>
        <meta name="description" content="Descubra como construir agentes autônomos determinísticos de engenharia com foco em governança corporativa e alto rendimento.">
        <link rel="canonical" href="https://ggcampos.com/thinking/arquitetura-autonoma">
        <meta property="og:title" content="Arquitetura de Software e Autonomia na Nuvem">
        <meta property="og:description" content="Descubra como construir agentes autônomos determinísticos de engenharia com foco em governança corporativa.">
        <meta property="og:image" content="https://ggcampos.com/og/arquitetura.png">
        <meta property="og:url" content="https://ggcampos.com/thinking/arquitetura-autonoma">
    </head>
    <body>
        <h1>Arquitetura Autônoma</h1>
    </body>
    </html>
    """
    res = validator.audit_html(html, sitemap_found=True)
    assert res.valid is True
    assert res.score >= 80.0
    assert res.title == "Arquitetura de Software e Autonomia na Nuvem"
    assert len(res.og_tags) >= 4
    assert res.canonical_url == "https://ggcampos.com/thinking/arquitetura-autonoma"


def test_seo_validator_missing_tags_penalties() -> None:
    validator = SEOValidator()
    poor_html = "<html><head><title>Hi</title></head><body>No desc or OG</body></html>"
    res = validator.audit_html(poor_html, sitemap_found=False)
    assert res.valid is False
    assert res.score < 50.0
    assert any("Title too short" in issue for issue in res.issues)
    assert any("Missing meta description" in issue for issue in res.issues)
    assert any("Missing canonical link" in issue for issue in res.issues)


def test_seo_validator_file_audit(tmp_path: Path) -> None:
    validator = SEOValidator()
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "sitemap.xml").write_text("<urlset></urlset>", encoding="utf-8")
    index_html = dist_dir / "index.html"
    index_html.write_text(
        """
        <html><head>
        <title>Dark Factory Enterprise Growth and Marketing Solutions</title>
        <meta name="description" content="Plataforma líder em engenharia autônoma para transformação digital, inteligência artificial e excelência operacional.">
        <link rel="canonical" href="https://ggcampos.com/">
        <meta property="og:title" content="Dark Factory Enterprise Growth">
        <meta property="og:description" content="Plataforma líder em engenharia autônoma para transformação digital.">
        <meta property="og:image" content="https://ggcampos.com/hero.png">
        <meta property="og:url" content="https://ggcampos.com/">
        </head><body></body></html>
        """,
        encoding="utf-8",
    )

    res = validator.audit_file(index_html)
    assert res.valid is True
    assert res.sitemap_found is True
    assert res.score >= 85.0


# ==============================================================================
# 4. GA4 Client Tests
# ==============================================================================


def test_ga4_event_logging_and_report(tmp_path: Path) -> None:
    log_path = tmp_path / "ga4_test_events.jsonl"
    client = GA4Client(events_log_file=log_path)

    ev1 = GA4Event(
        event_name="page_view",
        client_id="client-123",
        params={"page_title": "Home", "page_location": "https://ggcampos.com/"},
    )
    ev2 = GA4Event(
        event_name="generate_lead",
        client_id="client-123",
        params={"lead_id": "lead-001", "value": 15000},
    )

    assert client.send_event(ev1) is True
    assert client.send_event(ev2) is True
    assert log_path.is_file()

    report = client.get_report(project_id="atrium")
    assert report.pageviews == 2
    assert report.conversions == 1
    assert report.users == 1


# ==============================================================================
# 5. Google Ads & Budget Guardrail Tests
# ==============================================================================


def test_google_ads_report_and_budget_guardrail(tmp_path: Path) -> None:
    metrics_file = tmp_path / "ads_metrics.json"
    manager = GoogleAdsManager(metrics_file=metrics_file, default_budget_usd=500.0)

    # Baseline has total cost around ~459.50
    report_safe = manager.generate_report(project_id="atrium", budget_limit_usd=600.0)
    assert report_safe.total_cost_usd == 459.50
    assert report_safe.budget_exceeded is False
    assert report_safe.total_clicks > 0
    assert report_safe.total_conversions > 0

    # Test budget exceeded trigger
    report_exceeded = manager.generate_report(project_id="atrium", budget_limit_usd=400.0)
    assert report_exceeded.budget_exceeded is True

    # Test guardrail method
    assert manager.enforce_budget_guardrail("atrium", proposed_spend_usd=50.0, budget_limit_usd=600.0) is True
    assert manager.enforce_budget_guardrail("atrium", proposed_spend_usd=200.0, budget_limit_usd=600.0) is False


# ==============================================================================
# 6. Marketing CLI End-to-End Tests
# ==============================================================================


def test_marketing_cli_publish_and_leads(temp_site_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Test CLI post publish (defaults to staging without --approve)
    code = cli_main([
        "publish-post",
        "--title", "Estratégia de Crescimento B2B",
        "--content", "Artigo detalhando governança e conversão de leads corporativos com métricas comprovadas.",
        "--site-dir", str(temp_site_dir),
        "--tags", "growth,strategy",
    ])
    assert code == 0

    # 2. Test CLI list drafts (must show staged post)
    code = cli_main(["list-drafts", "--site-dir", str(temp_site_dir)])
    assert code == 0

    # 3. Test CLI approve-post (human gate approval)
    code = cli_main([
        "approve-post",
        "--slug", "estrategia-de-crescimento-b2b",
        "--approver", "owner",
        "--site-dir", str(temp_site_dir),
    ])
    assert code == 0
    assert (temp_site_dir / "src" / "content" / "thinking" / "estrategia-de-crescimento-b2b.md").is_file()

    # Test CLI case publish
    code = cli_main([
        "publish-case",
        "--title", "Reestruturação Industrial",
        "--era", "darkfac",
        "--anchor-metric", "Disponibilidade OEE",
        "--anchor-value", "98.5%",
        "--period", "2026",
        "--content", "Caso de implantação de agentes no chão de fábrica digital.",
        "--site-dir", str(temp_site_dir),
    ])
    assert code == 0

    # Test CLI lead capture
    test_db = tmp_path / "cli_leads.db"
    monkeypatch.setattr("core.marketing.crm_n8n.DEFAULT_LEADS_DB_PATH", test_db)
    code = cli_main([
        "lead-capture",
        "--name", "Mariana Duarte",
        "--email", "mariana@consulting.com",
        "--company", "Consultoria Alpha",
        "--source", "google",
        "--campaign", "perf_max",
        "--no-n8n",
        "--no-telegram",
    ])
    assert code == 0

    # Test CLI list leads
    code = cli_main(["list-leads", "--project-id", "atrium"])
    assert code == 0

    # Test CLI ads report
    code = cli_main(["ads-report", "--project-id", "atrium", "--budget-limit", "1000"])
    assert code == 0
