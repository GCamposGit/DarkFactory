"""Tests for Enterprise Profile On-Demand (HF-24)."""

import json
from pathlib import Path
import pytest

from core.evolution.models import SecurityViolationError
from core.enterprise.audit_chain import ImmutableAuditChain
from core.enterprise.models import (
    DataResidencyMode,
    EnterpriseProjectConfig,
    EnterpriseRole,
)
from core.enterprise.policy import EnterprisePolicyGuard
from core.enterprise.residency import DataResidencyEnforcer, PIISanitizer
from core.enterprise.sla_guard import EnterpriseSLAGuard


def test_pii_sanitizer_detects_and_masks_sensitive_data() -> None:
    sample_text = (
        "Cliente CPF: 123.456.789-00, CNPJ 12.345.678/0001-99, "
        "Email contato@empresa.com, Chave: sk-123456789012345678901234, "
        "Cartao 1234-5678-9012-3456."
    )
    assert PIISanitizer.contains_pii(sample_text)

    cleaned, count = PIISanitizer.sanitize(sample_text)
    assert count >= 5
    assert "123.456.789-00" not in cleaned
    assert "[REDACTED_CPF]" in cleaned
    assert "[REDACTED_CNPJ]" in cleaned
    assert "[REDACTED_EMAIL]" in cleaned
    assert "[REDACTED_SECRET]" in cleaned
    assert "[REDACTED_CREDIT_CARD]" in cleaned
    assert not PIISanitizer.contains_pii(cleaned)


def test_data_residency_enforcer_blocks_cloud_in_local_only() -> None:
    cfg = EnterpriseProjectConfig(
        project_id="enterprise-client",
        enabled=True,
        residency_mode=DataResidencyMode.LOCAL_ONLY,
    )
    enforcer = DataResidencyEnforcer(cfg)

    # Local is allowed
    assert enforcer.validate_inference_route("ollama_local")
    assert enforcer.validate_inference_route("qwen-fast")

    # Cloud providers are blocked fail-closed
    with pytest.raises(SecurityViolationError, match="LOCAL_ONLY residency"):
        enforcer.validate_inference_route("openrouter")

    with pytest.raises(SecurityViolationError, match="LOCAL_ONLY residency"):
        enforcer.validate_inference_route("openai")


def test_data_residency_enforcer_eu_only_scope() -> None:
    cfg = EnterpriseProjectConfig(
        project_id="enterprise-eu",
        enabled=True,
        residency_mode=DataResidencyMode.EU_ONLY,
    )
    enforcer = DataResidencyEnforcer(cfg)
    assert enforcer.validate_inference_route("hetzner_eu")
    assert enforcer.validate_inference_route("ollama_local")

    with pytest.raises(SecurityViolationError, match="EU_ONLY residency"):
        enforcer.validate_inference_route("google_us_central")


def test_immutable_audit_chain_integrity_and_tamper_detection(tmp_path: Path) -> None:
    log_file = tmp_path / "audit_trail.jsonl"
    chain = ImmutableAuditChain(root=tmp_path, log_file=log_file)

    # 1. Record 3 events
    evt1 = chain.record_event("admin", "atrium", "login", "auth_token")
    evt2 = chain.record_event("admin", "atrium", "update_config", "seo_settings", {"rpo": 60})
    evt3 = chain.record_event("owner", "atrium", "deploy_release", "release_v1")

    assert evt1.sequence == 0
    assert evt2.sequence == 1
    assert evt3.sequence == 2
    assert evt2.prev_hash == evt1.event_hash
    assert evt3.prev_hash == evt2.event_hash

    # 2. Verify intact chain
    res = chain.verify_integrity("atrium")
    assert res.is_valid
    assert res.total_events == 3

    # 3. Simulate tampering: modify line 1 payload
    lines = log_file.read_text(encoding="utf-8").splitlines()
    tampered_data = json.loads(lines[1])
    tampered_data["action"] = "malicious_tamper"
    lines[1] = json.dumps(tampered_data)
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 4. Verify tampering is caught immediately
    tamper_res = chain.verify_integrity("atrium")
    assert not tamper_res.is_valid
    assert tamper_res.tampered_event_id == evt2.event_id
    assert "Tampered content" in tamper_res.error_message


def test_enterprise_sla_guard_measures_rpo_and_rto(tmp_path: Path) -> None:
    guard = EnterpriseSLAGuard(root=tmp_path)
    cfg = EnterpriseProjectConfig(
        project_id="test-sla",
        enabled=True,
        max_rpo_minutes=60,
        max_rto_minutes=30,
    )

    # 1. Compliant run
    res_good = guard.check_sla(cfg, last_backup_age_minutes=25.0, measured_rto_minutes=12.0)
    assert res_good.is_compliant
    assert len(res_good.violations) == 0

    # 2. Non-compliant run: backup too old
    res_bad_rpo = guard.check_sla(cfg, last_backup_age_minutes=95.0, measured_rto_minutes=15.0)
    assert not res_bad_rpo.is_compliant
    assert any("RPO Violation" in v for v in res_bad_rpo.violations)

    # 3. Non-compliant run: recovery latency too high
    res_bad_rto = guard.check_sla(cfg, last_backup_age_minutes=10.0, measured_rto_minutes=45.0)
    assert not res_bad_rto.is_compliant
    assert any("RTO Violation" in v for v in res_bad_rto.violations)


def test_enterprise_policy_guard_scenario_g8(tmp_path: Path) -> None:
    configs_file = tmp_path / "configs.json"
    guard = EnterprisePolicyGuard(root=tmp_path, configs_file=configs_file)

    cfg = EnterpriseProjectConfig(
        project_id="enterprise-prod-client",
        enabled=True,
        require_owner_signoff=True,
        residency_mode=DataResidencyMode.LOCAL_ONLY,
    )
    guard.set_config(cfg)

    # 1. Reject without owner signoff
    dec1 = guard.evaluate_production_release(
        project_id="enterprise-prod-client",
        target_environment="production",
        owner_approved=False,
        actor_role=EnterpriseRole.OPERATOR,
        simulated_backup_age_minutes=10.0,
        simulated_rto_minutes=5.0,
    )
    assert not dec1.approved
    assert not dec1.owner_signoff_verified
    assert any("Owner Signoff Violation" in r for r in dec1.rejection_reasons)

    # 2. Approve with owner signoff, valid chain, and SLA compliance
    dec2 = guard.evaluate_production_release(
        project_id="enterprise-prod-client",
        target_environment="production",
        owner_approved=True,
        actor_role=EnterpriseRole.OWNER,
        simulated_backup_age_minutes=15.0,
        simulated_rto_minutes=4.0,
    )
    assert dec2.approved
    assert dec2.owner_signoff_verified
    assert dec2.audit_chain_verified
    assert dec2.residency_verified
    assert dec2.sla_verified
    assert len(dec2.rejection_reasons) == 0


def test_enterprise_cli_status_and_configure(capsys: pytest.CaptureFixture[str]) -> None:
    from core.enterprise.cli import main

    # Status of unconfigured project
    code_status = main(["status", "--project", "darkfac"])
    assert code_status == 0
    captured = capsys.readouterr()
    assert "Enterprise Profile: darkfac" in captured.out

    # Configure enterprise
    code_cfg = main(["configure", "--project", "test-prj", "--enable", "--residency", "local_only", "--rpo", "60", "--rto", "30"])
    assert code_cfg == 0

    # Verify audit
    code_audit = main(["verify-audit", "--project", "test-prj"])
    assert code_audit == 0
