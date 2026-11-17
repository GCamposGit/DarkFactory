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

from pydantic import BaseModel, ConfigDict, Field

from core.enterprise.models import DataResidencyMode, EnterpriseProjectConfig
from core.enterprise.residency import API_KEY_PATTERN, DataResidencyEnforcer
from core.evolution.models import SecurityViolationError
from core.paths import project_root

logger = logging.getLogger(__name__)

EFFECTS = ("inference", "export", "delivery")
# Destination treated as an external (non-local, non-EU) provider on export/delivery paths.
DELIVERY_PROVIDER = "github"
SHARED_DATA_POLICY_FILE = Path(".factory") / "config" / "data_policies.json"


class DataPolicyDiagnostic(BaseModel):
    """Structured explanation when a host policy differs from the shared reference."""

    model_config = ConfigDict(extra="forbid")

    code: str
    project_id: str
    shared_source_path: Path
    local_source_path: Path
    effective_source_path: Path


class DataPolicyResolution(BaseModel):
    """Effective project policy together with its provenance and source diagnostics."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    project_id: str
    policy: Optional[EnterpriseProjectConfig] = None
    source_path: Optional[Path] = None
    shared_source_path: Optional[Path] = None
    local_source_path: Path
    diagnostics: list[DataPolicyDiagnostic] = Field(default_factory=list)

    @property
    def effective_policy(self) -> Optional[EnterpriseProjectConfig]:
        """Descriptive alias for callers that prefer the full field name."""
        return self.policy


class DataPolicyError(SecurityViolationError):
    """Structured refusal: `code`, `project_id`, `effect`, `provider`, `reason`."""

    def __init__(
        self,
        code: str,
        project_id: str,
        effect: str,
        provider: Optional[str],
        reason: str,
        source_path: Optional[Path] = None,
    ) -> None:
        self.code = code
        self.project_id = project_id
        self.effect = effect
        self.provider = provider
        self.reason = reason
        self.source_path = source_path
        super().__init__(f"[{code}] project={project_id} effect={effect} provider={provider}: {reason}")

    def to_dict(self) -> dict[str, Any]:
        result = {
            "error": self.code, "project_id": self.project_id, "effect": self.effect,
            "provider": self.provider, "reason": self.reason,
        }
        if self.source_path is not None:
            result["source_path"] = str(self.source_path)
        return result


class DataPolicyUnavailable(DataPolicyError):
    """The policy exists but cannot be read: sensitive effects are blocked (fail-closed)."""


def _configs_file(root: Optional[Path] = None) -> Path:
    """Resolve the policy file under an injected state root or the configured state root."""
    if root is not None:
        return (Path(root).expanduser().resolve() / "enterprise" / "configs.json").resolve()

    from core.paths import state_root as configured_state_root

    return (configured_state_root() / "enterprise" / "configs.json").resolve()


def _shared_configs_file() -> Path:
    """Return the versioned project policy reference, separate from mutable host state."""
    return (project_root() / SHARED_DATA_POLICY_FILE).expanduser().resolve()


def _read_project_policy(project_id: str, path: Path) -> Optional[EnterpriseProjectConfig]:
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
            f"policy unreadable at {path} ({type(exc).__name__}); sensitive effects blocked",
            source_path=path,
        ) from exc


def _policy_semantics(config: Optional[EnterpriseProjectConfig]) -> Optional[tuple[Any, ...]]:
    """Compare only active data controls, ignoring defaults, metadata, and set ordering."""
    if config is None or not config.enabled:
        return None
    return (
        config.residency_mode.value,
        config.pii_sanitization_strict,
        tuple(sorted({provider.strip().lower() for provider in config.allowed_providers})),
    )


def resolve_project_policy(
    project_id: str,
    configs_file: Optional[Path] = None,
    *,
    root: Optional[Path] = None,
    shared_configs_file: Optional[Path] = None,
) -> DataPolicyResolution:
    """Resolve an effective policy and report any behavior-changing host override.

    ``.factory/config/data_policies.json`` is the shared, versioned reference. The mutable
    ``.factory/enterprise/configs.json`` state file remains a host override for compatibility.
    A differing local entry keeps its historic effective behavior but emits a structured warning
    so callers can identify the machine-specific source.
    """
    local_path = (
        Path(configs_file).expanduser()
        if configs_file is not None
        else (_configs_file() if root is None else _configs_file(root))
    ).resolve()
    # An explicit legacy file remains an isolated source unless the caller supplies a reference.
    shared_path = (
        Path(shared_configs_file).expanduser().resolve()
        if shared_configs_file is not None
        else (_shared_configs_file() if configs_file is None else None)
    )

    local_policy = _read_project_policy(project_id, local_path)
    shared_policy = (
        _read_project_policy(project_id, shared_path)
        if shared_path is not None and shared_path != local_path
        else (local_policy if shared_path == local_path else None)
    )
    diagnostics: list[DataPolicyDiagnostic] = []

    local_semantics = _policy_semantics(local_policy)
    shared_semantics = _policy_semantics(shared_policy)
    divergent = local_semantics != shared_semantics
    if local_policy is not None and divergent and shared_path is not None:
        diagnostic = DataPolicyDiagnostic(
            code="data_policy_host_divergence",
            project_id=project_id,
            shared_source_path=shared_path,
            local_source_path=local_path,
            effective_source_path=local_path,
        )
        diagnostics.append(diagnostic)
        logger.warning(
            "Data policy differs from the shared reference for project %s",
            project_id,
            extra={"data_policy_diagnostic": diagnostic.model_dump(mode="json")},
        )

    # Prefer the shared definition when the host copy is behaviorally equivalent. If there is
    # no shared entry, a local config remains effective for compatibility with existing hosts.
    if local_policy is not None and (shared_policy is None or divergent):
        effective_policy, source_path = local_policy, local_path
    else:
        effective_policy, source_path = shared_policy, shared_path

    return DataPolicyResolution(
        project_id=project_id,
        policy=effective_policy,
        source_path=source_path if effective_policy is not None else None,
        shared_source_path=shared_path,
        local_source_path=local_path,
        diagnostics=diagnostics,
    )


def load_project_policy(
    project_id: str,
    configs_file: Optional[Path] = None,
    *,
    root: Optional[Path] = None,
    shared_configs_file: Optional[Path] = None,
) -> Optional[EnterpriseProjectConfig]:
    """Load project policy from a state root or explicit file, independent of the current directory."""
    return resolve_project_policy(
        project_id, configs_file, root=root, shared_configs_file=shared_configs_file,
    ).policy


def enforce_data_policy(
    project_id: str,
    effect: str,
    *,
    provider: Optional[str] = None,
    payload_text: str = "",
    configs_file: Optional[Path] = None,
    root: Optional[Path] = None,
    shared_configs_file: Optional[Path] = None,
) -> None:
    """Raise DataPolicyError when `effect` violates the project's policy; return None otherwise."""
    if effect not in EFFECTS:
        raise DataPolicyError("data_policy_unknown_effect", project_id, effect, provider, "unknown effect")
    # Minimum protection for ALL projects: never ship credentials out.
    if payload_text and API_KEY_PATTERN.search(payload_text):
        raise DataPolicyError("data_policy_secret_in_payload", project_id, effect, provider,
                              "payload contains a credential; minimum protection blocks it")
    cfg = load_project_policy(
        project_id, configs_file, root=root, shared_configs_file=shared_configs_file,
    )
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


def provider_allowed(
    project_id: str,
    provider: str,
    configs_file: Optional[Path] = None,
    *,
    root: Optional[Path] = None,
    shared_configs_file: Optional[Path] = None,
) -> bool:
    """Routing helper: True when `provider` may serve inference for the project (False if policy unreadable)."""
    return provider_policy_rejection(
        project_id, provider, configs_file, root=root, shared_configs_file=shared_configs_file,
    ) is None


def provider_policy_rejection(
    project_id: str,
    provider: str,
    configs_file: Optional[Path] = None,
    *,
    root: Optional[Path] = None,
    shared_configs_file: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """Return a sanitized policy refusal for routing, or None when the provider is allowed.

    The diagnostic deliberately omits raw exception text and policy metadata. It names only the
    effective policy file, project, provider, and controls needed to explain the refusal.
    """
    try:
        resolution = resolve_project_policy(
            project_id, configs_file, root=root, shared_configs_file=shared_configs_file,
        )
    except DataPolicyUnavailable as exc:
        return {
            "project_id": project_id,
            "provider": provider,
            "source_file": str(exc.source_path or _configs_file(root)),
            "reason_code": exc.code,
            "restriction": {"policy_status": "unavailable"},
        }

    cfg = resolution.policy
    if cfg is None or not cfg.enabled:
        return None

    try:
        enforce_data_policy(
            project_id, "inference", provider=provider, configs_file=resolution.source_path,
        )
    except DataPolicyError as exc:
        logger.info("data policy refuses provider %s: %s", provider, exc.code)
        refusal = {
            "project_id": project_id,
            "provider": provider,
            "source_file": str(resolution.source_path or resolution.local_source_path),
            "reason_code": exc.code,
            "restriction": {
                "residency_mode": cfg.residency_mode.value,
                "allowed_providers": sorted({item.strip().lower() for item in cfg.allowed_providers}),
            },
        }
        if resolution.diagnostics:
            refusal["host_diagnostics"] = [
                diagnostic.model_dump(mode="json") for diagnostic in resolution.diagnostics
            ]
        return refusal
    return None
