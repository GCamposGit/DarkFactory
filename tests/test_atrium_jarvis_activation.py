"""Integrated practical activation test suite for ATRIUM and JARVIS projects.

Governed by Universal Engineering Standards (AGENTS.md).
Validates real integration readiness, governance locks, Anti-Slop CMS publishing with
mandatory Gate G1, cross-project component syncing, Segundo Cérebro MCP provenance,
paid-first model routing with fail-closed budget guards, and cryptographic audit chaining.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

from core.adoption.service import inspect_project
from core.catalog.manager import CrossProjectCatalogManager
from core.catalog.models import ComponentKind
from core.content.anti_slop_linter import AntiSlopLinter
from core.enterprise.audit_chain import ImmutableAuditChain
from core.knowledge.models import KnowledgeCitation, KnowledgeQuery
from core.knowledge.segundo_cerebro_client import SegundoCerebroClient
from core.marketing.models import BlogPost, SEOAuditResult
from core.marketing.publisher import ContentPublisher
from core.marketing.seo_analytics import SEOValidator
from core.portfolio.budget_manager import PortfolioBudgetManager
from core.portfolio.models import ModelTier, ProjectBudgetConfig
from core.portfolio.router_optimizer import PortfolioModelRouter
from core.projects.models import ProjectKind
from core.projects.registry import get_project_registry


# ==============================================================================
# 1. ATRIUM Activation Tests
# ==============================================================================


def test_atrium_adoption_and_governance_inspection() -> None:
    """Validate ATRIUM (Site_ggcampos) is registered and structurally compliant."""
    registry = get_project_registry()
    atrium_desc = registry.get_project("site-ggcampos")
    assert atrium_desc is not None, "site-ggcampos must be registered in ProjectRegistry"
    assert atrium_desc.kind == ProjectKind.CLIENT_PORTFOLIO
    assert atrium_desc.prefix == "SIT"
    assert atrium_desc.domain == "ggcampos.com"
    assert atrium_desc.deploy_target == "hostinger_ftp"

    atrium_path = Path(atrium_desc.path)
    if atrium_path.is_dir():
        inspection = inspect_project(atrium_path)
        assert inspection.kind == "brownfield"
        assert inspection.existing_lock is True
        assert "AGENTS.md" in inspection.governance_files
        assert "MISSION.md" in inspection.governance_files
        assert "FACTORY_RULES.md" in inspection.governance_files
        assert "node" in inspection.stack.ecosystems


def test_atrium_anti_slop_and_gate_g1_lifecycle(tmp_path: Path) -> None:
    """Validate content authoring with Anti-Slop (Skill 15) and Gate G1 staging/promotion."""
    site_dir = tmp_path / "site"
    staging_dir = tmp_path / "staged"

    publisher = ContentPublisher(
        target_site_dir=site_dir,
        staging_dir=staging_dir,
        max_slop_score=20.0,
        telegram_gateway=None,
    )

    clean_content = (
        "Neste ensaio técnico, analisamos a transição de arquiteturas distribuídas "
        "para agentes de software autônomos operando em fábricas de software. "
        "A separação estrita de contratos de domínio e I/O garante que cada módulo "
        "possa ser inspecionado deterministicamente por oráculos de teste."
    )

    post = BlogPost(
        title="Arquitetura de Agentes Autônomos",
        slug="arquitetura-agentes-autonomos",
        description="Ensaio prático sobre orquestração autônoma e contratos estritos.",
        date=datetime(2026, 9, 18, tzinfo=timezone.utc),
        tags=["arquitetura", "agentes", "engenharia"],
        content_md=clean_content,
        lang="pt",
    )

    # 1. Publish without approval -> Must be STAGED awaiting Gate G1
    stage_res = publisher.publish_blog_post(post, approved=False)
    assert stage_res.success is True
    assert stage_res.status == "staged_pending_approval"
    assert stage_res.requires_human_approval is True
    assert stage_res.slop_score <= 20.0

    staged_file = staging_dir / "arquitetura-agentes-autonomos.md"
    assert staged_file.is_file(), "Draft must be staged in staging directory"

    # Production file must NOT exist yet
    prod_file = site_dir / "src" / "content" / "thinking" / "arquitetura-agentes-autonomos.md"
    assert not prod_file.exists(), "Unapproved post must never reach production directory"

    # 2. Simulate Gate G1 Human Approval -> Promotes to production and clears staging
    approve_res = publisher.publish_blog_post(post, approved=True, approver="Guilherme Campos")
    assert approve_res.success is True
    assert approve_res.status == "published"
    assert approve_res.requires_human_approval is False
    assert approve_res.approver == "Guilherme Campos"

    assert prod_file.is_file(), "Approved post must be written to Astro thinking collection"
    assert not staged_file.exists(), "Staged file must be cleaned up after human approval"

    # Verify Astro frontmatter
    prod_text = prod_file.read_text(encoding="utf-8")
    assert 'title: "Arquitetura de Agentes Autônomos"' in prod_text
    assert "date: 2026-09-18" in prod_text
    assert 'lang: "pt"' in prod_text
    assert '  - "arquitetura"' in prod_text


def test_atrium_seo_metadata_audit() -> None:
    """Validate deterministic SEO auditing on HTML structures."""
    validator = SEOValidator(target_domain="https://ggcampos.com")

    # Audit compliant HTML
    valid_html = """<!DOCTYPE html>
    <html lang="pt">
    <head>
        <title>Guilherme Campos - Engenharia e Software Autônomo</title>
        <meta name="description" content="Portfólio executivo e ensaios técnicos de Guilherme Campos cobrindo inteligência artificial, engenharia e arquitetura de software." />
        <link rel="canonical" href="https://ggcampos.com/thinking/agentes" />
        <meta property="og:title" content="Guilherme Campos - Engenharia e Software Autônomo" />
        <meta property="og:description" content="Portfólio executivo e ensaios técnicos de Guilherme Campos." />
        <meta property="og:url" content="https://ggcampos.com/thinking/agentes" />
        <meta property="og:image" content="https://ggcampos.com/og.jpg" />
        <meta property="og:type" content="article" />
    </head>
    <body><h1>Conteúdo</h1></body>
    </html>"""

    result = validator.audit_html(valid_html, url_or_path="https://ggcampos.com/thinking/agentes")
    assert result.valid is True
    assert result.score >= 80.0
    assert result.meta_description is not None
    assert result.canonical_url == "https://ggcampos.com/thinking/agentes"
    assert result.og_tags.get("og:type") == "article"

    # Audit defective HTML (missing title and canonical)
    bad_html = "<html><head><meta name='description' content='Curto'></head><body>Vazio</body></html>"
    bad_result = validator.audit_html(bad_html, url_or_path="inline")
    assert bad_result.valid is False
    assert any("Missing <title>" in issue for issue in bad_result.issues)
    assert any("Missing canonical" in issue for issue in bad_result.issues)


def test_atrium_catalog_component_sync(tmp_path: Path) -> None:
    """Validate reusable component synchronization from DarkFac catalog into Atrium."""
    catalog_mgr = CrossProjectCatalogManager(root=tmp_path)
    components = catalog_mgr.list_components()
    assert len(components) >= 4

    target_proj_dir = tmp_path / "Site_ggcampos"
    target_proj_dir.mkdir(parents=True, exist_ok=True)

    # Sync atrium-anti-slop-linter
    res = catalog_mgr.sync_to_project(
        component_id="atrium-anti-slop-linter",
        target_project_id="site-ggcampos",
        target_dir_override=target_proj_dir,
        overwrite=True,
    )
    assert res.success is True
    assert "core/content/anti_slop.py" in res.files_synced

    synced_file = target_proj_dir / "core" / "content" / "anti_slop.py"
    assert synced_file.is_file()
    assert "class AntiSlopHook" in synced_file.read_text(encoding="utf-8")


# ==============================================================================
# 2. JARVIS Activation Tests
# ==============================================================================


def test_jarvis_adoption_and_governance_inspection() -> None:
    """Validate JARVIS is registered and structurally compliant."""
    registry = get_project_registry()
    jarvis_desc = registry.get_project("jarvis")
    assert jarvis_desc is not None, "jarvis must be registered in ProjectRegistry"
    assert jarvis_desc.kind == ProjectKind.INTERNAL_PRODUCT
    assert jarvis_desc.prefix == "JRV"
    assert jarvis_desc.deploy_target == "local_service"

    jarvis_path = Path(jarvis_desc.path)
    if jarvis_path.is_dir():
        inspection = inspect_project(jarvis_path)
        assert inspection.kind == "brownfield"
        assert inspection.existing_lock is True
        assert "AGENTS.md" in inspection.governance_files
        assert "MISSION.md" in inspection.governance_files
        assert "FACTORY_RULES.md" in inspection.governance_files
        assert "python" in inspection.stack.ecosystems


def test_jarvis_segundo_cerebro_provenance_and_isolation(tmp_path: Path) -> None:
    """Validate Segundo Cérebro MCP client with provenance hashes and multi-tenant segregation."""
    client = SegundoCerebroClient(mock_mode=True)

    corpus = [
        {
            "id": "jrv-01",
            "arquivo": "jarvis/architecture/pipeline.md",
            "secao": "Audio Pipeline",
            "onde": "p.3",
            "texto": "O pipeline de áudio neural do Jarvis utiliza processamento assíncrono com VAD local.",
            "score": 0.95,
        },
        {
            "id": "sh-01",
            "arquivo": "shared/protocols/mcp.md",
            "secao": "Tool Protocols",
            "onde": "p.1",
            "texto": "Protocolo de ferramentas MCP padronizado para troca estruturada de contexto.",
            "score": 0.88,
        },
        {
            "id": "atr-01",
            "arquivo": "atrium/cases/bain.md",
            "secao": "Private Atrium Case",
            "onde": "p.1",
            "texto": "Dados confidenciais exclusivos do projeto Atrium.",
            "score": 0.92,
        },
    ]
    client.set_mock_corpus(corpus)

    # 1. Query for jarvis tenant: Must retrieve jarvis and shared docs, but REJECT atrium doc
    q_jarvis = KnowledgeQuery(
        query="pipeline áudio neural",
        project_id="jarvis",
        min_score=0.5,
    )
    result = client.search(q_jarvis)
    assert result.status == "FOUND"
    assert len(result.citations) >= 1

    cited_paths = [c.file_path for c in result.citations]
    assert "jarvis/architecture/pipeline.md" in cited_paths
    assert "atrium/cases/bain.md" not in cited_paths, "Tenant segregation: jarvis must not see atrium docs"

    # Verify SHA-256 provenance hash on every citation
    for cit in result.citations:
        expected_hash = hashlib.sha256(cit.content.encode("utf-8")).hexdigest()
        assert cit.provenance_hash == expected_hash

    # 2. Fail-Closed Anti-Hallucination: min_score above available results -> INSUFFICIENT_EVIDENCE
    q_strict = KnowledgeQuery(
        query="algo inexistente",
        project_id="jarvis",
        min_score=0.999,
    )
    strict_res = client.search(q_strict)
    assert strict_res.status == "INSUFFICIENT_EVIDENCE"
    assert len(strict_res.citations) == 0


def test_jarvis_paid_first_model_routing_and_budget_cutoff(tmp_path: Path) -> None:
    """Validate paid-account-first model routing and fail-closed cutoff when budget is reached."""
    budget_file = tmp_path / "budgets.json"
    b_mgr = PortfolioBudgetManager(storage_file=budget_file)

    # Jarvis has $50 budget, initially $0 spent
    b_mgr.set_budget("jarvis", 50.0)

    # Active paid balances
    balances = {"openai": 25.0, "antigravity": 50.0, "xai": 10.0}
    router = PortfolioModelRouter(budget_manager=b_mgr, account_balances=balances)

    # 1. Normal routing -> routes to Tier 1 paid direct model
    r_high = router.route_task("coding_high", project_id="jarvis")
    assert r_high.tier == ModelTier.PAID_DIRECT
    assert r_high.selected_model == "luna-xhigh"
    assert r_high.provider == "openai"

    r_res = router.route_task("research", project_id="jarvis")
    assert r_res.tier == ModelTier.PAID_DIRECT
    assert r_res.selected_model == "gemini-3.8-flash"

    # 2. Project budget reaches 100% cap -> Fail-Closed cutoff to Tier 3 local $0
    b_mgr.record_spend("jarvis", 55.0)  # Exceeds $50 limit
    assert b_mgr.is_paid_cloud_allowed("jarvis") is False

    r_cutoff = router.route_task("coding_high", project_id="jarvis")
    assert r_cutoff.tier == ModelTier.LOCAL_ZERO
    assert r_cutoff.provider == "ollama"
    assert r_cutoff.estimated_cost_usd == 0.0
    assert "qwen" in r_cutoff.selected_model


def test_jarvis_enterprise_audit_trail_integrity(tmp_path: Path) -> None:
    """Validate ImmutableAuditChain (HF-24) records tamper-evident events for Jarvis tenant."""
    audit_file = tmp_path / "jarvis_audit.jsonl"
    chain = ImmutableAuditChain(log_file=audit_file)

    # Record 3 events for jarvis
    e1 = chain.record_event(
        actor_id="operator_gui",
        project_id="jarvis",
        action="project_bootstrap",
        resource="jarvis/pyproject.toml",
        payload={"version": "1.0.0"},
    )
    e2 = chain.record_event(
        actor_id="mcp_daemon",
        project_id="jarvis",
        action="knowledge_query",
        resource="segundo_cerebro/index",
        payload={"query": "neural audio", "k": 3},
    )
    e3 = chain.record_event(
        actor_id="model_router",
        project_id="jarvis",
        action="model_dispatch",
        resource="openai/luna-xhigh",
        payload={"tier": "PAID_DIRECT", "tokens": 1200},
    )

    assert e1.sequence == 0
    assert e2.sequence == 1
    assert e3.sequence == 2
    assert e2.prev_hash == e1.event_hash
    assert e3.prev_hash == e2.event_hash

    # Verify integrity
    verif = chain.verify_integrity(project_id="jarvis")
    assert verif.is_valid is True
    assert verif.total_events == 3
    assert verif.tampered_event_id is None

    # Corrupt event e2 by modifying resource field in raw JSONL
    lines = audit_file.read_text(encoding="utf-8").splitlines()
    assert "segundo_cerebro/index" in lines[1]
    lines[1] = lines[1].replace("segundo_cerebro/index", "tampered/unauthorized_resource")
    audit_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tampered_verif = chain.verify_integrity(project_id="jarvis")
    assert tampered_verif.is_valid is False
    assert tampered_verif.tampered_event_id == e2.event_id
