"""
Deterministic integration and reachability test suite for DarkHub Group 2 modules (USR-55):
- DH-04: Catálogo e Evolução (catalog/sync, evolution/propose, evaluate, promote, rollback)
- DH-07: Estúdio de Conteúdo Anti-Slop e Ateliê Visual (presets, generate, lint, gallery, visual generate, illustrate)
- DH-09: Governança Enterprise e Deploy Dokploy (audit-trail, configure, evaluate-deploy, cloud/deploy)
- DH-11: Validação sob Demanda e Harness Remoto (harness/run-tests, harness/execute)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hub.backend.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_headers(client: TestClient) -> dict[str, str]:
    """Obtain a valid owner session token from /api/session."""
    res = client.get("/api/session")
    assert res.status_code == 200
    token = res.json()["session_token"]
    return {"X-Hub-Session": token}


# =====================================================================
# DH-04: Catálogo Cross-Projeto & Auto-Evolução
# =====================================================================

def test_dh04_catalog_sync_and_evolution_lifecycle(client: TestClient, auth_headers: dict[str, str]) -> None:
    # 1. Catalog Sync
    comp_res = client.get("/api/catalog/components")
    assert comp_res.status_code == 200
    comp_data = comp_res.json()
    assert "components" in comp_data
    import uuid
    from pathlib import Path
    unique_rule = f".agents/rules/test_rule_{uuid.uuid4().hex[:6]}.md"
    try:
        if comp_data["components"]:
            target_comp = comp_data["components"][0]["id"]
            sync_res = client.post(
                "/api/catalog/sync",
                headers=auth_headers,
                json={
                    "component_id": target_comp,
                    "target_project_id": "darkfac",
                    "overwrite": True,
                },
            )
            assert sync_res.status_code in {200, 400}
            if sync_res.status_code == 200:
                assert sync_res.json()["success"] is True

        # 2. Evolution Proposal Lifecycle (Propose -> Evaluate -> Promote -> Rollback)
        prop_res = client.post(
            "/api/evolution/propose",
            headers=auth_headers,
            json={
                "target_kind": "context_rule",
                "target_path": unique_rule,
                "trigger": "manual_proposal",
                "patch_content": "# Test Evolution Rule\n\nDeterministic rule for test.",
                "rationale": "Automated verification of self-evolution pipeline in DH-04.",
            },
        )
        assert prop_res.status_code == 200
        prop_data = prop_res.json()
        assert prop_data["success"] is True
        proposal_id = prop_data["proposal"]["proposal_id"]

        # 3. Evaluate proposal
        eval_res = client.post(
            "/api/evolution/evaluate",
            headers=auth_headers,
            json={"proposal_id": proposal_id},
        )
        assert eval_res.status_code == 200
        eval_data = eval_res.json()
        assert "result" in eval_data

        # 4. Promote proposal
        prom_res = client.post(
            "/api/evolution/promote",
            headers=auth_headers,
            json={"proposal_id": proposal_id},
        )
        assert prom_res.status_code == 200
        assert prom_res.json()["success"] is True

        # 5. Rollback proposal
        roll_res = client.post(
            "/api/evolution/rollback",
            headers=auth_headers,
            json={"proposal_id": proposal_id},
        )
        assert roll_res.status_code == 200
        assert roll_res.json()["success"] is True
    finally:
        Path("core/content/anti_slop.py").unlink(missing_ok=True)
        Path(unique_rule).unlink(missing_ok=True)


# =====================================================================
# DH-07: Estúdio de Conteúdo Anti-Slop e Ateliê Visual
# =====================================================================

def test_dh07_content_presets_and_lint(client: TestClient) -> None:
    # 1. Presets
    presets_res = client.get("/api/content/presets")
    assert presets_res.status_code == 200
    presets = presets_res.json()
    assert isinstance(presets, list)
    assert len(presets) >= 3

    # 2. Content Lint (Deterministic)
    lint_res = client.post(
        "/api/content/lint",
        json={
            "text": "In today's fast-paced world, we must delve into the game-changing paradigm shift.",
        },
    )
    assert lint_res.status_code == 200
    lint_data = lint_res.json()
    assert "slop_score" in lint_data
    assert "violations" in lint_data
    assert lint_data["violations_count"] > 0


def test_dh07_content_generate(client: TestClient) -> None:
    res = client.post(
        "/api/content/generate",
        json={
            "topic": "Microagent idempotent architecture without framework lock-in",
            "content_type": "linkedin_post",
            "offline": True,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert "final_content" in data
    assert len(data["final_content"]) > 10
    assert "cleanliness_rating" in data


def test_dh07_visual_gallery_and_generation(client: TestClient) -> None:
    # 1. Gallery
    gallery_res = client.get("/api/visual/gallery")
    assert gallery_res.status_code == 200
    assert isinstance(gallery_res.json(), list)

    # 2. Visual Generation
    gen_res = client.post(
        "/api/visual/generate",
        json={
            "title": "Dark architecture flow diagram with deterministic gates",
            "asset_type": "architecture_diagram",
            "aspect_ratio": "16:9",
            "theme": "modern_minimalist_dark",
            "offline": True,
        },
    )
    assert gen_res.status_code == 200
    gen_data = gen_res.json()
    assert "asset_id" in gen_data
    assert "file_path" in gen_data

    # 3. Text Illustration
    ill_res = client.post(
        "/api/visual/illustrate",
        json={
            "text": "The memory ledger acts as a persistent cryptographic anchor for autonomous workflows.",
            "asset_type": "social_banner",
            "aspect_ratio": "16:9",
            "offline": True,
        },
    )
    assert ill_res.status_code == 200
    ill_data = ill_res.json()
    assert "asset_id" in ill_data


# =====================================================================
# DH-09: Governança Enterprise e Deploy Dokploy
# =====================================================================

def test_dh09_enterprise_governance_and_deploy_gate(client: TestClient, auth_headers: dict[str, str]) -> None:
    # 1. Audit Trail
    audit_res = client.get("/api/enterprise/audit-trail?project=darkfac", headers=auth_headers)
    assert audit_res.status_code == 200
    audit_data = audit_res.json()
    assert "events" in audit_data
    assert "count" in audit_data

    # 2. Configure Enterprise Policy
    cfg_res = client.post(
        "/api/enterprise/configure",
        headers=auth_headers,
        json={
            "project_id": "darkfac",
            "enabled": True,
            "residency_mode": "local_only",
            "max_rpo_minutes": 60,
            "max_rto_minutes": 30,
            "require_owner_signoff": True,
        },
    )
    assert cfg_res.status_code == 200
    cfg_data = cfg_res.json()
    assert cfg_data["success"] is True
    assert cfg_data["config"]["residency_mode"] == "local_only"

    # 3. Evaluate Deploy Gate (Scenario G8)
    eval_res = client.post(
        "/api/enterprise/evaluate-deploy",
        headers=auth_headers,
        json={
            "project_id": "darkfac",
            "target_environment": "production",
            "owner_approved": True,
            "actor_role": "owner",
        },
    )
    assert eval_res.status_code == 200
    eval_data = eval_res.json()
    assert "approved" in eval_data or "decision" in eval_data

    # 4. Trigger Dokploy Deploy
    deploy_res = client.post(
        "/api/cloud/deploy",
        json={"project": "darkfac"},
    )
    assert deploy_res.status_code == 200
    deploy_data = deploy_res.json()
    assert "status" in deploy_data


# =====================================================================
# DH-11: Validação sob Demanda e Harness Remoto
# =====================================================================

def test_dh11_harness_test_execution_endpoints(client: TestClient) -> None:
    # 1. Run Tests via Subagent Engine
    run_res = client.post(
        "/api/harness/run-tests",
        json={
            "target": "tests/test_hub_coverage.py",
            "scope": "file",
            "timeout_seconds": 30,
            "fail_fast": True,
        },
    )
    assert run_res.status_code == 200
    run_data = run_res.json()
    assert "verdict" in run_data
    assert "success" in run_data
    assert "concise_summary" in run_data

    # 2. Execute Test Suite via Distributed Worker
    exec_res = client.post(
        "/api/harness/execute",
        json={
            "target": "tests/test_hub_coverage.py",
            "scope": "file",
            "timeout_seconds": 30,
            "fail_fast": True,
        },
    )
    assert exec_res.status_code == 200
    exec_data = exec_res.json()
    assert "verdict" in exec_data
    assert "success" in exec_data
