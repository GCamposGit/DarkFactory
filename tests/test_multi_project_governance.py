"""Deterministic verification for multi-project governance, segregation, and DarkHub integration."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

from core.projects.models import ProjectDescriptor, ProjectKind
from core.projects.registry import ProjectRegistry
from core.demands.store import DemandsStore
from core.demands.models import UserTicket
from core.usage.models import ModelCallEvent, ModelTier, ModelModality
from core.usage.ledger import ModelUsageLedger
from core.infra.models import InfraInventory, InfraNode, NodeRole, NodeStatus
from core.infra.cards import build_infra_cards_report
from core.roadmap.service import RoadmapQueryService
from hub.backend.main import app


def test_project_registry_loading_and_prefixes():
    with TemporaryDirectory() as tmpdir:
        proj_file = Path(tmpdir) / "projects.json"
        registry = ProjectRegistry(projects_file=proj_file)
        
        # Test defaults
        assert len(registry.list_projects()) >= 1
        assert registry.get_ticket_prefix("darkfac") == "USR"

        # Register custom client project
        client_proj = ProjectDescriptor(
            id="client-acme",
            name="Acme Corp Portal",
            description="Client portal for Acme Corp",
            path=str(Path(tmpdir) / "acme"),
            kind=ProjectKind.CLIENT_PORTFOLIO,
            prefix="ACM",
        )
        registry.register_project(client_proj)

        assert registry.get_project("client-acme") is not None
        assert registry.get_ticket_prefix("client-acme") == "ACM"
        assert registry.get_ticket_prefix("unknown-proj") == "USR"

        # Verify persistence
        reloaded = ProjectRegistry(projects_file=proj_file)
        assert reloaded.get_project("client-acme") is not None
        assert reloaded.get_ticket_prefix("client-acme") == "ACM"


def test_demands_store_project_prefixes():
    with TemporaryDirectory() as tmpdir:
        store_path = Path(tmpdir) / "demands.json"
        store = DemandsStore(path=store_path)

        # site-ggcampos should start with SIT-01
        next_id_site = store.next_ticket_id(project_id="site-ggcampos")
        assert next_id_site == "SIT-01"

        # Create ticket for site-ggcampos
        ticket_site = UserTicket(
            id=next_id_site,
            project_id="site-ggcampos",
            title="Update bio header",
            problem_statement="Bio header needs update",
        )
        store.save_ticket(ticket_site)

        # Next ticket for site-ggcampos should be SIT-02
        assert store.next_ticket_id(project_id="site-ggcampos") == "SIT-02"

        # segundo-cerebro should start with SC-01
        assert store.next_ticket_id(project_id="segundo-cerebro") == "SC-01"

        # darkfac preserves USR prefix
        assert store.next_ticket_id(project_id="darkfac") == "USR-01"


def test_roadmap_query_service_discovers_all_projects():
    service = RoadmapQueryService()
    projects = service.list_projects()
    project_ids = [p.id for p in projects]

    assert "darkfac" in project_ids
    assert "site-ggcampos" in project_ids
    assert "segundo-cerebro" in project_ids


def test_model_usage_ledger_project_segregation():
    with TemporaryDirectory() as tmpdir:
        ledger = ModelUsageLedger(storage_dir=Path(tmpdir))

        # Record call for darkfac
        ledger.record(ModelCallEvent(
            invocation_id="inv-df-01",
            ticket_id="USR-01",
            project_id="darkfac",
            provider="ollama",
            model="qwen2.5-coder:7b",
            tier=ModelTier.LOCAL,
            cost_usd=0.0,
            input_tokens=100,
            output_tokens=50,
        ))

        # Record call for site-ggcampos
        ledger.record(ModelCallEvent(
            invocation_id="inv-sit-01",
            ticket_id="SIT-01",
            project_id="site-ggcampos",
            provider="openrouter",
            model="anthropic/claude-3.7-sonnet",
            tier=ModelTier.FRONTIER,
            cost_usd=0.025,
            input_tokens=1500,
            output_tokens=600,
        ))

        # Filtered report for site-ggcampos
        rep_site = ledger.report(project_id="site-ggcampos")
        assert rep_site.total_calls == 1
        assert rep_site.total_cost_usd == pytest.approx(0.025)
        assert len(rep_site.aggregates) == 1
        assert rep_site.aggregates[0].model == "anthropic/claude-3.7-sonnet"
        assert len(rep_site.recent_events) == 1
        assert rep_site.recent_events[0].project_id == "site-ggcampos"

        # Filtered report for darkfac
        rep_df = ledger.report(project_id="darkfac")
        assert rep_df.total_calls == 1
        assert rep_df.total_cost_usd == 0.0
        assert len(rep_df.aggregates) == 1
        assert rep_df.aggregates[0].model == "qwen2.5-coder:7b"

        # Global report
        rep_global = ledger.report()
        assert rep_global.total_calls == 2
        assert rep_global.total_cost_usd == pytest.approx(0.025)


def test_infra_cards_project_filtering():
    inv = InfraInventory(
        version="1.0.0",
        nodes=[
            InfraNode(
                id="hostinger-web",
                name="Hostinger Web",
                role=NodeRole.MANAGED_SERVICE,
                status=NodeStatus.ACTIVE,
                provider="Hostinger",
                tags=["hosting", "project:site-ggcampos"],
                cost_monthly_usd=4.0,
            ),
            InfraNode(
                id="hetzner-vps",
                name="Hetzner VPS",
                role=NodeRole.CLOUD_VPS,
                status=NodeStatus.ACTIVE,
                provider="Hetzner",
                tags=["cloud", "vps", "project:darkfac", "shared"],
                cost_monthly_usd=7.19,
            ),
            InfraNode(
                id="storage-server",
                name="On-Prem Storage",
                role=NodeRole.ON_PREM_SERVER,
                status=NodeStatus.ACTIVE,
                provider="Local",
                tags=["storage", "project:segundo-cerebro"],
                cost_monthly_usd=0.0,
            ),
        ],
    )

    # Filter by site-ggcampos: should get hostinger-web and hetzner-vps (because it's shared)
    report_site = build_infra_cards_report(inv, project_id="site-ggcampos")
    site_card_ids = [c.id for c in report_site.cards]
    assert "hostinger-web" in site_card_ids
    assert "hetzner-vps" in site_card_ids
    assert "storage-server" not in site_card_ids

    # Filter by segundo-cerebro
    report_sc = build_infra_cards_report(inv, project_id="segundo-cerebro")
    sc_card_ids = [c.id for c in report_sc.cards]
    assert "storage-server" in sc_card_ids
    assert "hetzner-vps" in sc_card_ids
    assert "hostinger-web" not in sc_card_ids


def test_darkhub_api_multi_project_integration():
    client = TestClient(app)

    # 1. Projects listing
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    data = resp.json()
    project_ids = [p["id"] for p in data]
    assert "darkfac" in project_ids
    assert "site-ggcampos" in project_ids
    assert "segundo-cerebro" in project_ids

    # 2. Demand next-id with project_id
    resp_site_id = client.get("/api/demands/next-id?project_id=site-ggcampos")
    assert resp_site_id.status_code == 200
    assert resp_site_id.json()["next_id"].startswith("SIT-")

    resp_sc_id = client.get("/api/demands/next-id?project_id=segundo-cerebro")
    assert resp_sc_id.status_code == 200
    assert resp_sc_id.json()["next_id"].startswith("SC-")

    # 3. Infra cards filtering
    resp_infra = client.get("/api/infra/cards?project_id=site-ggcampos")
    assert resp_infra.status_code == 200
    infra_data = resp_infra.json()
    card_ids = [c["id"] for c in infra_data["cards"]]
    assert "hostinger-web" in card_ids

    # 4. Model usage filtering
    resp_usage = client.get("/api/usage/models?project_id=site-ggcampos")
    assert resp_usage.status_code == 200
    usage_data = resp_usage.json()
    assert "total_calls" in usage_data
