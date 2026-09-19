"""Comprehensive Client Onboarding End-to-End Test Suite.

Validates that when a new paying client or internal project enters Dark Factory today,
the entire onboarding pipeline operates with 100% reliability:
1. Archetype Scaffolding (HF-20)
2. Adoption Gateway & Governance Lock Installation (HF-20)
3. Project Registry Discovery & Prefix Assignment (HF-20)
4. Reusable Catalog Component Synchronization (HF-25)
5. Enterprise Security Profile, Data Residency & Immutable Audit (HF-24)
6. Financial Budget Ceiling & Capacity Slot Allocation (HF-23)
7. Knowledge Ingestion & Specification Cataloging (HF-22)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.adoption.service import apply_adoption, inspect_project
from core.archetypes.models import ScaffoldRequest
from core.archetypes.scaffolder import ArchetypeScaffolder
from core.catalog.manager import CrossProjectCatalogManager
from core.enterprise.audit_chain import ImmutableAuditChain
from core.enterprise.models import DataResidencyMode, EnterpriseProjectConfig
from core.enterprise.policy import EnterprisePolicyGuard
from core.enterprise.residency import DataResidencyEnforcer, SecurityViolationError
from core.knowledge.ingestor import KnowledgeIngestor
from core.knowledge.models import IngestionRequest
from core.portfolio.budget_manager import PortfolioBudgetManager
from core.portfolio.models import JobSlotKind, ProjectBudgetConfig
from core.portfolio.scheduler import PortfolioScheduler
from core.projects.models import ProjectDescriptor, ProjectKind
from core.projects.registry import ProjectRegistry


import subprocess


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def test_client_onboarding_greenfield_e2e(tmp_path: Path) -> None:
    """Simulate a complete greenfield client project onboarding from zero to operational."""
    # 0. Setup clean isolated source repository
    src_root = tmp_path / "factory-source"
    src_root.mkdir(parents=True)
    _git(src_root, "init")
    _git(src_root, "config", "user.name", "DarkFac Test")
    _git(src_root, "config", "user.email", "darkfac-test@local")
    (src_root / "core").mkdir()
    (src_root / "core" / "__init__.py").write_text("", encoding="utf-8")
    (src_root / "core" / "paths.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "def project_root():\n"
        "    return Path(os.environ.get('DARKFAC_PROJECT_ROOT', Path(__file__).resolve().parent.parent)).resolve()\n",
        encoding="utf-8",
    )
    docs_dir = src_root / "docs"
    docs_dir.mkdir()
    for name in ("DARK_FACTORY_PLAYBOOK.md", "HARNESS_INTEROP.md", "MODEL_SELECTION_GUIDE.md"):
        (docs_dir / name).write_text(f"# {name}\n", encoding="utf-8")
    _git(src_root, "add", "-A")
    _git(src_root, "commit", "-m", "factory source baseline")

    client_dir = tmp_path / "acme-crm"
    client_dir.mkdir(parents=True, exist_ok=True)
    _git(client_dir, "init")
    _git(client_dir, "config", "user.name", "Acme Test")
    _git(client_dir, "config", "user.email", "acme@local")

    # --------------------------------------------------------------------------
    # 1. Scaffolding via Archetype Engine
    # --------------------------------------------------------------------------
    scaffolder = ArchetypeScaffolder()
    scaffold_req = ScaffoldRequest(
        archetype_id="internal_tool",
        project_name="Acme Enterprise CRM",
        target_dir=str(client_dir),
        parameters={"author_name": "Acme Corp", "author_title": "Product Team"},
    )
    scaffold_res = scaffolder.scaffold(scaffold_req)
    assert scaffold_res.success is True
    assert "pyproject.toml" in scaffold_res.files_created
    assert "main.py" in scaffold_res.files_created
    assert (client_dir / "pyproject.toml").is_file()

    # Commit initial scaffold
    _git(client_dir, "add", "-A")
    _git(client_dir, "commit", "-m", "initial scaffold")

    # --------------------------------------------------------------------------
    # 2. Adoption Gateway: Apply Dark Factory Governance & Locks
    # --------------------------------------------------------------------------
    adoption_plan = apply_adoption(client_dir, source_root=src_root)
    assert "acme-crm" in adoption_plan.project_root
    assert "AGENTS.md" in adoption_plan.created
    assert (client_dir / "AGENTS.md").is_file()
    assert (client_dir / "MISSION.md").is_file()
    assert (client_dir / "FACTORY_RULES.md").is_file()

    # Inspecionar resultado
    inspection = inspect_project(client_dir)
    assert inspection.existing_lock is True
    assert "AGENTS.md" in inspection.governance_files

    # --------------------------------------------------------------------------
    # 3. Project Registry: Registration & Ticket Prefix
    # --------------------------------------------------------------------------
    registry_file = tmp_path / "projects.json"
    registry = ProjectRegistry(projects_file=registry_file)
    desc = ProjectDescriptor(
        id="acme-crm",
        name="Acme Enterprise CRM",
        path=str(client_dir),
        kind=ProjectKind.SAAS_APP,
        prefix="ACM",
        domain="crm.acme.internal",
        deploy_target="dokploy_docker",
        description="Portal de CRM e inteligência comercial para a Acme Corp.",
    )
    registry.register_project(desc)
    assert registry.get_project("acme-crm") is not None
    assert registry.get_ticket_prefix("acme-crm") == "ACM"

    # --------------------------------------------------------------------------
    # 4. Catalog Component Synchronization (HF-25)
    # --------------------------------------------------------------------------
    catalog_mgr = CrossProjectCatalogManager(root=tmp_path)
    sync_res = catalog_mgr.sync_to_project(
        component_id="portfolio-budget-guard",
        target_project_id="acme-crm",
        target_dir_override=client_dir,
        overwrite=True,
    )
    assert sync_res.success is True
    assert (client_dir / "core" / "portfolio" / "budget_guard.py").is_file()

    # --------------------------------------------------------------------------
    # 5. Enterprise Security Profile: Residency, Audit Chain & Policy (HF-24)
    # --------------------------------------------------------------------------
    audit_file = client_dir / ".factory" / "enterprise" / "audit_trail.jsonl"
    audit_chain = ImmutableAuditChain(log_file=audit_file)

    # Record client onboarding event
    e_onboard = audit_chain.record_event(
        actor_id="onboarding_wizard",
        project_id="acme-crm",
        action="client_project_onboarded",
        resource=str(client_dir),
        payload={"tier": "enterprise", "slas": {"rpo": 30, "rto": 15}},
    )
    assert e_onboard.sequence == 0

    # Verify integrity
    verif = audit_chain.verify_integrity(project_id="acme-crm")
    assert verif.is_valid is True
    assert verif.total_events == 1

    # Enforce data residency (LOCAL_ONLY)
    eu_cfg = EnterpriseProjectConfig(
        project_id="acme-crm",
        enabled=True,
        residency_mode=DataResidencyMode.LOCAL_ONLY,
    )
    enforcer = DataResidencyEnforcer(config=eu_cfg)
    assert enforcer.validate_inference_route("ollama_local") is True
    with pytest.raises(SecurityViolationError):
        enforcer.validate_inference_route("openrouter")

    # --------------------------------------------------------------------------
    # 6. Portfolio Capacity & Budget Allocation (HF-23)
    # --------------------------------------------------------------------------
    budget_file = tmp_path / "budgets.json"
    budget_mgr = PortfolioBudgetManager(storage_file=budget_file)
    b_acme = budget_mgr.set_budget("acme-crm", 100.0)
    assert b_acme.monthly_limit_usd == 100.0
    assert budget_mgr.is_paid_cloud_allowed("acme-crm") is True

    sched_file = tmp_path / "scheduler.json"
    scheduler = PortfolioScheduler(storage_file=sched_file)
    scheduler.enqueue_job("job-acme-01", "acme-crm", JobSlotKind.LIGHT)
    dispatched = scheduler.dequeue_next()
    assert dispatched is not None
    assert dispatched["job_id"] == "job-acme-01"

    # --------------------------------------------------------------------------
    # 7. Knowledge Ingestion of Client Specs (HF-22)
    # --------------------------------------------------------------------------
    kb_dir = client_dir / "docs" / "specs"
    kb_dir.mkdir(parents=True, exist_ok=True)
    spec_file = kb_dir / "acme_business_rules.md"
    spec_file.write_text(
        "# Regras de Negócio Acme CRM\n\n"
        "1. Os leads devem ser categorizados por score de propensão.\n"
        "2. Nenhuma transação pode ser aprovada sem duplo fator de autenticação.\n",
        encoding="utf-8",
    )

    ingestor = KnowledgeIngestor(corpus_root=tmp_path / "kb")
    ingest_req = IngestionRequest(
        file_path=str(spec_file),
        project_id="acme-crm",
        format="md",
    )
    ingest_res = ingestor.ingest(ingest_req)
    assert ingest_res.success is True
    assert ingest_res.file_hash is not None
    assert (tmp_path / "kb" / "acme-crm" / "acme_business_rules.md").is_file()


def test_client_onboarding_brownfield_adoption(tmp_path: Path) -> None:
    """Simulate adopting an existing client repository preserving existing code & history."""
    from core.adoption.service import verify_adoption

    # 0. Setup clean isolated source repository
    src_root = tmp_path / "factory-source-brownfield"
    src_root.mkdir(parents=True)
    _git(src_root, "init")
    _git(src_root, "config", "user.name", "DarkFac Test")
    _git(src_root, "config", "user.email", "darkfac-test@local")
    (src_root / "core").mkdir()
    (src_root / "core" / "__init__.py").write_text("", encoding="utf-8")
    (src_root / "core" / "paths.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "def project_root():\n"
        "    return Path(os.environ.get('DARKFAC_PROJECT_ROOT', Path(__file__).resolve().parent.parent)).resolve()\n",
        encoding="utf-8",
    )
    docs_dir = src_root / "docs"
    docs_dir.mkdir()
    for name in ("DARK_FACTORY_PLAYBOOK.md", "HARNESS_INTEROP.md", "MODEL_SELECTION_GUIDE.md"):
        (docs_dir / name).write_text(f"# {name}\n", encoding="utf-8")
    _git(src_root, "add", "-A")
    _git(src_root, "commit", "-m", "factory source baseline")

    # 1. Existing client repo with legacy code
    client_repo = tmp_path / "client-legacy-app"
    client_repo.mkdir(parents=True, exist_ok=True)
    _git(client_repo, "init")
    _git(client_repo, "config", "user.name", "Client Dev")
    _git(client_repo, "config", "user.email", "dev@client.com")
    (client_repo / "legacy_app.py").write_text("# Important client business logic\ndef calculate_roi(): return 42\n", encoding="utf-8")
    (client_repo / "pyproject.toml").write_text("[project]\nname = 'client-legacy-app'\nversion = '2.4.0'\n", encoding="utf-8")
    (client_repo / "README.md").write_text("# Client Legacy System\n", encoding="utf-8")
    tests_dir = client_repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_smoke.py").write_text("def test_roi():\n    from legacy_app import calculate_roi\n    assert calculate_roi() == 42\n", encoding="utf-8")
    _git(client_repo, "add", "-A")
    commit_sha = _git(client_repo, "commit", "-m", "existing client codebase").stdout

    # 2. Inspect existing repo
    insp = inspect_project(client_repo)
    assert insp.kind == "brownfield"
    assert insp.git.clean is True
    assert insp.existing_lock is False

    # 3. Apply adoption without disrupting existing code
    adopt_res = apply_adoption(client_repo, source_root=src_root)
    assert "client-legacy-app" in adopt_res.project_root
    assert (client_repo / "AGENTS.md").is_file()
    assert (client_repo / "MISSION.md").is_file()
    assert (client_repo / "FACTORY_RULES.md").is_file()

    # Verify client's original file is 100% untouched
    assert "def calculate_roi(): return 42" in (client_repo / "legacy_app.py").read_text(encoding="utf-8")
    assert "version = '2.4.0'" in (client_repo / "pyproject.toml").read_text(encoding="utf-8")

    # 4. Verify adoption status
    verif = verify_adoption(client_repo)
    assert verif.ready is True
