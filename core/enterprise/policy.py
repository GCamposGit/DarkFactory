"""Enterprise Policy Guard and Production Release Gate (HF-24).

Implements Scenario G8 of the hybrid autonomy requirements: enforcing mandatory
Owner signoff, cryptographic audit chain integrity, data residency, and SLA compliance
before any commercial paying project can be promoted to production.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from core.paths import project_root
from core.enterprise.models import (
    DataResidencyMode,
    EnterpriseDeployDecision,
    EnterpriseProjectConfig,
    EnterpriseRole,
)
from core.enterprise.audit_chain import ImmutableAuditChain
from core.enterprise.residency import DataResidencyEnforcer
from core.enterprise.sla_guard import EnterpriseSLAGuard

logger = logging.getLogger(__name__)

DEFAULT_CONFIGS_FILE = Path(".factory") / "enterprise" / "configs.json"


class EnterprisePolicyGuard:
    """Central orchestrator for enterprise security, residency, and release gates."""

    def __init__(self, root: Optional[Path] = None, configs_file: Optional[Path] = None) -> None:
        self.root = Path(root) if root else project_root()
        self.configs_file = Path(configs_file) if configs_file else (self.root / DEFAULT_CONFIGS_FILE)
        self.audit_chain = ImmutableAuditChain(self.root)
        self.sla_guard = EnterpriseSLAGuard(self.root)
        self._configs: Dict[str, EnterpriseProjectConfig] = {}
        self._load()

    def _load(self) -> None:
        """Load enterprise project configs from disk."""
        self.configs_file.parent.mkdir(parents=True, exist_ok=True)
        if self.configs_file.is_file():
            try:
                data = json.loads(self.configs_file.read_text(encoding="utf-8"))
                for item in data:
                    cfg = EnterpriseProjectConfig.model_validate(item)
                    self._configs[cfg.project_id] = cfg
            except Exception as exc:
                logger.error(f"Failed to load enterprise configs from {self.configs_file}: {exc}")

    def _save(self) -> None:
        """Persist enterprise project configs to disk."""
        self.configs_file.parent.mkdir(parents=True, exist_ok=True)
        payload = [cfg.model_dump(mode="json") for cfg in self._configs.values()]
        self.configs_file.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def get_config(self, project_id: str) -> EnterpriseProjectConfig:
        """Return the enterprise config for a project, or a disabled default if not set."""
        if project_id in self._configs:
            return self._configs[project_id]
        return EnterpriseProjectConfig(project_id=project_id, enabled=False)

    def set_config(self, config: EnterpriseProjectConfig) -> None:
        """Set or update enterprise configuration for a project."""
        self._configs[config.project_id] = config
        self._save()
        self.audit_chain.record_event(
            actor_id="system",
            project_id=config.project_id,
            action="enterprise_config_updated",
            resource=f"project:{config.project_id}",
            payload=config.model_dump(mode="json"),
        )

    def evaluate_production_release(
        self,
        project_id: str,
        target_environment: str = "production",
        *,
        owner_approved: bool = False,
        actor_role: EnterpriseRole | str = EnterpriseRole.OPERATOR,
        simulated_backup_age_minutes: Optional[float] = None,
        simulated_rto_minutes: Optional[float] = None,
    ) -> EnterpriseDeployDecision:
        """Evaluate Scenario G8 enterprise production deployment constraints."""
        cfg = self.get_config(project_id)
        norm_role = EnterpriseRole(actor_role) if isinstance(actor_role, str) else actor_role

        # If enterprise mode is not enabled for this project, standard automated gates govern
        if not cfg.enabled:
            return EnterpriseDeployDecision(
                project_id=project_id,
                target_environment=target_environment,
                approved=True,
                owner_signoff_verified=True,
                audit_chain_verified=True,
                residency_verified=True,
                sla_verified=True,
                rejection_reasons=[],
            )

        rejections: List[str] = []

        # 1. Owner signoff check for production
        owner_ok = True
        if target_environment == "production" and cfg.require_owner_signoff:
            if not owner_approved or norm_role != EnterpriseRole.OWNER:
                owner_ok = False
                rejections.append(
                    "Owner Signoff Violation (Scenario G8): Commercial enterprise production release "
                    "requires explicit authenticated Owner approval."
                )

        # 2. Cryptographic audit chain integrity verification
        audit_res = self.audit_chain.verify_integrity(project_id)
        audit_ok = audit_res.is_valid
        if not audit_ok:
            rejections.append(
                f"Audit Chain Integrity Failure: {audit_res.error_message} "
                f"(tampered event: {audit_res.tampered_event_id})"
            )

        # 3. Data residency verification
        residency_enforcer = DataResidencyEnforcer(cfg)
        residency_ok = True
        try:
            # Verify that default configured providers are compliant
            for prov in cfg.allowed_providers:
                residency_enforcer.validate_inference_route(prov)
        except Exception as exc:
            residency_ok = False
            rejections.append(f"Residency Policy Violation: {exc}")

        # 4. Disaster recovery SLA check
        sla_res = self.sla_guard.check_sla(
            cfg,
            last_backup_age_minutes=simulated_backup_age_minutes,
            measured_rto_minutes=simulated_rto_minutes,
        )
        sla_ok = sla_res.is_compliant
        if not sla_ok:
            rejections.extend(sla_res.violations)

        approved = len(rejections) == 0

        decision = EnterpriseDeployDecision(
            project_id=project_id,
            target_environment=target_environment,
            approved=approved,
            owner_signoff_verified=owner_ok,
            audit_chain_verified=audit_ok,
            residency_verified=residency_ok,
            sla_verified=sla_ok,
            rejection_reasons=rejections,
        )

        # Audit the decision
        self.audit_chain.record_event(
            actor_id=norm_role.value,
            project_id=project_id,
            action="evaluate_enterprise_deploy",
            resource=f"deploy:{target_environment}",
            payload=decision.model_dump(mode="json"),
        )

        return decision
