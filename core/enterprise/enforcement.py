"""Single enforcement point binding the HF-24 data-protection policy to real executors (USR-174).

Inference routing (`core.line.routing.pick`), export and delivery (`core.git.autonomy`) call
`enforce_data_policy`. Contract:

* A minimum protection applies to EVERY project, including those without enterprise config:
  payloads carrying credentials/API keys are never exported or sent to a provider.
* A project with an enabled enterprise config also gets its residency mode, provider allowlist
  and strict PII check.
* Policy file present but unreadable/invalid => fail-closed with a structured error
  (`DataPolicyUnavailable`); an absent file means "no enterprise config" (current behavior).
* Pure inference routing without a payload for a project with no enabled policy is always allowed,
  so the default keeps the live fleet's behavior.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from core.enterprise.models import DataResidencyMode, EnterpriseProjectConfig
from core.enterprise.residency import API_KEY_PATTERN, DataResidencyEnforcer
from core.evolution.models import SecurityViolationError

logger = logging.getLogger(__name__)

EFFECTS = ("inference", "export", "delivery")
# Destination treated as an external (non-local, non-EU) provider on export/delivery paths.
DELIVERY_PROVIDER = "github"


class DataPolicyError(SecurityViolationError):
    """Structured refusal: `code`, `project_id`, `effect`, `provider`, `reason`."""

    def __init__(self, code: str, project_id: str, effect: str, provider: Optional[str], reason: str) -> None:
        self.code = code
        self.project_id = project_id
        self.effect = effect
        self.provider = provider
        self.reason = reason
        super().__init__(f"[{code}] project={project_id} effect={effect} provider={provider}: {reason}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.code, "project_id": self.project_id, "effect": self.effect,
            "provider": self.provider, "reason": self.reason,
        }


class DataPolicyUnavailable(DataPolicyError):
    """The policy exists but cannot be read: sensitive effects are blocked (fail-closed)."""


def _configs_file() -> Path:
    from core.paths import state_root

    return state_root() / "enterprise" / "configs.json"


def load_project_policy(project_id: str, configs_file: Optional[Path] = None) -> Optional[EnterpriseProjectConfig]:
    """Return the project's config, None when there is none; raise DataPolicyUnavailable if unreadable."""
    path = Path(configs_file) if configs_file else _configs_file()
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data:
            cfg = EnterpriseProjectConfig.model_validate(item)
            if cfg.project_id == project_id:
                return cfg
        return None
    except Exception as exc:
        raise DataPolicyUnavailable(
            "data_policy_unavailable", project_id, "policy_load", None,
            f"policy unreadable ({type(exc).__name__}); sensitive effects blocked",
        ) from exc


def enforce_data_policy(
    project_id: str,
    effect: str,
    *,
    provider: Optional[str] = None,
    payload_text: str = "",
    configs_file: Optional[Path] = None,
) -> None:
    """Raise DataPolicyError when `effect` violates the project's policy; return None otherwise."""
    if effect not in EFFECTS:
        raise DataPolicyError("data_policy_unknown_effect", project_id, effect, provider, "unknown effect")
    # Minimum protection for ALL projects: never ship credentials out.
    if payload_text and API_KEY_PATTERN.search(payload_text):
        raise DataPolicyError("data_policy_secret_in_payload", project_id, effect, provider,
                              "payload contains a credential; minimum protection blocks it")
    cfg = load_project_policy(project_id, configs_file)
    if cfg is None or not cfg.enabled:
        return
    target = (provider or "").strip().lower()
    allowed = {p.strip().lower() for p in cfg.allowed_providers}
    explicit = bool(target) and target in allowed  # owner's explicit allowlist overrides the residency provider sets
    if target:
        if (
            cfg.residency_mode != DataResidencyMode.GLOBAL_ALLOWED
            and target not in allowed
            and target not in DataResidencyEnforcer.LOCAL_ALLOWED_PROVIDERS
        ):
            raise DataPolicyError("data_policy_provider_forbidden", project_id, effect, provider,
                                  "provider not in the project's allowlist")
    try:
        DataResidencyEnforcer(cfg).validate_inference_route("local" if explicit or not target else target, payload_text)
    except SecurityViolationError as exc:
        raise DataPolicyError("data_policy_residency_violation", project_id, effect, provider, str(exc)) from exc


def provider_allowed(project_id: str, provider: str, configs_file: Optional[Path] = None) -> bool:
    """Routing helper: True when `provider` may serve inference for the project (False if policy unreadable)."""
    try:
        enforce_data_policy(project_id, "inference", provider=provider, configs_file=configs_file)
        return True
    except DataPolicyError as exc:
        logger.info("data policy refuses provider %s: %s", provider, exc.code)
        return False
